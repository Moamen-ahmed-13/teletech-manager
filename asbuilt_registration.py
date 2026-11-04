"""PDF-to-DXF registration for TELETECH As-Built analysis.

register_page_to_cad(page_bgr, cad_gray, cad_px_to_world) -> dict
  Finds a robust transform between a scanned/photographed As-Built page and a rasterized DXF floor.
  Returns {'ok':bool,'reason':str,'page_px_to_cad':2x3 (page pixel -> CAD world),'diagnostics':{...}}.
  Never returns ok=True unless inlier count, ratio, spatial coverage, held-out error and
  geometry sanity checks all pass; callers must route ok=False pages to manual review.
"""
import math
import numpy as np
import cv2

MIN_INLIERS=150; MIN_INLIER_RATIO=0.15; MIN_SEED=30; MIN_COVERAGE=0.20
MAX_HELDOUT_ERR_FRAC=0.012   # median held-out error, fraction of page diagonal
MAX_HELDOUT_P90_FRAC=0.035

def _color_mask(bgr):
    hsv=cv2.cvtColor(bgr,cv2.COLOR_BGR2HSV)
    return (hsv[:,:,1]>70)&(hsv[:,:,2]>60)

def preprocess_page(bgr, target_long=2200):
    """Photo/scan -> black line-art on white. Removes colored ink and uneven lighting. Returns (img, scale)."""
    h,w=bgr.shape[:2];sc=target_long/max(h,w)
    if abs(sc-1)>0.02: bgr=cv2.resize(bgr,(int(w*sc),int(h*sc)),interpolation=cv2.INTER_AREA)
    mask=cv2.dilate(_color_mask(bgr).astype(np.uint8),np.ones((5,5),np.uint8))>0
    g=cv2.cvtColor(bgr,cv2.COLOR_BGR2GRAY)
    g[mask]=255
    bg=cv2.medianBlur(cv2.resize(g,(g.shape[1]//8,g.shape[0]//8)),21)
    bg=cv2.resize(bg,(g.shape[1],g.shape[0]))
    norm=np.clip(g.astype(np.float32)/np.maximum(bg,1)*255,0,255).astype(np.uint8)
    bw=cv2.adaptiveThreshold(norm,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY,31,12)
    bw=cv2.morphologyEx(bw,cv2.MORPH_OPEN,np.ones((2,2),np.uint8)) if False else bw
    return bw,sc

def _feat(img, nfeat=12000):
    img=cv2.GaussianBlur(img,(0,0),1.6)
    sift=cv2.SIFT_create(nfeatures=nfeat,contrastThreshold=0.02)
    k,d=sift.detectAndCompute(img,None)
    return k,d

def _coverage(pts,w,h):
    if len(pts)<3: return 0.0
    hull=cv2.convexHull(pts.astype(np.float32))
    return float(cv2.contourArea(hull))/max(1.0,float(w*h))

def _fit(src,dst,thr):
    """similarity first (rotation/scale/translation), then full affine on its inliers if it stays consistent."""
    M,inl=cv2.estimateAffinePartial2D(src,dst,method=cv2.RANSAC,ransacReprojThreshold=thr,maxIters=8000,confidence=0.9995,refineIters=20)
    if M is None: return None,None,'similarity'
    inl=inl.ravel().astype(bool)
    A,inl2=cv2.estimateAffine2D(src[inl],dst[inl],method=cv2.RANSAC,ransacReprojThreshold=thr*0.8,maxIters=5000,confidence=0.999,refineIters=20)
    if A is not None and inl2.sum()>=0.85*inl.sum():
        # accept affine only when it does not shear/scale anisotropically beyond scan distortion
        s1,s2=np.linalg.svd(A[:,:2],compute_uv=False)
        if s1/s2<1.12:
            full=np.zeros(len(src),bool);idx=np.where(inl)[0];full[idx[inl2.ravel().astype(bool)]]=True
            return A,full,'affine'
    return M,inl,'similarity'

def register_page_to_cad(page_bgr, cad_gray, cad_px_to_world, mirror_ok=False):
    """cad_px_to_world: 2x3 affine mapping CAD-raster pixel -> DXF world coords (includes the Y flip)."""
    diag={}
    page,psc=preprocess_page(page_bgr)
    ph,pw=page.shape[:2];ch,cw=cad_gray.shape[:2]
    # work on inverted-free line art (black on white) for both
    kp,dp=_feat(page);kc,dc=_feat(cad_gray)
    diag.update(page_keypoints=len(kp),cad_keypoints=len(kc))
    if dp is None or dc is None or len(kp)<80 or len(kc)<80:
        return {'ok':False,'reason':'Too few reliable features on page or CAD render.','diagnostics':diag}
    bf=cv2.BFMatcher(cv2.NORM_L2)
    m=bf.knnMatch(dp,dc,k=2)
    good=[a for a,b in (x for x in m if len(x)==2) if a.distance<0.80*b.distance]
    diag['good_matches']=len(good)
    if len(good)<MIN_SEED:
        return {'ok':False,'reason':f'Only {len(good)} candidate feature matches (need {MIN_SEED}).','diagnostics':diag}
    src=np.float32([kp[g.queryIdx].pt for g in good]);dst=np.float32([kc[g.trainIdx].pt for g in good])
    diagp=math.hypot(pw,ph)
    kp_xy=np.float32([k.pt for k in kp]);kc_xy=np.float32([k.pt for k in kc])
    A,inl,kind=_fit(src,dst,thr=max(4.0,0.004*diagp))
    if A is None: return {'ok':False,'reason':'RANSAC found no consistent transform.','diagnostics':diag}
    # guided matching: first transform is a prior; accept nearest descriptor inside a tight spatial gate
    from scipy.spatial import cKDTree
    tree=cKDTree(kc_xy)
    for _ in range(3):
        pred=kp_xy@A[:,:2].T+A[:,2]
        sc_a=math.sqrt(abs(A[0,0]*A[1,1]-A[0,1]*A[1,0]))
        gate=max(8.0,0.010*diagp*sc_a)
        gs,gd=[],[]
        for i,cand in enumerate(tree.query_ball_point(pred,gate)):
            if not cand: continue
            dist=np.linalg.norm(dc[cand]-dp[i],axis=1);o=np.argsort(dist)
            if len(cand)>1 and dist[o[0]]>0.85*dist[o[1]]: continue
            gs.append(kp_xy[i]);gd.append(kc_xy[cand[o[0]]])
        if len(gs)<MIN_SEED: break
        gs=np.float32(gs);gd=np.float32(gd)
        A2,i2,k2=_fit(gs,gd,thr=max(3.0,0.003*diagp))
        if A2 is None or i2.sum()<=inl.sum()*1.05: break
        A,inl,kind,src,dst=A2,i2,k2,gs,gd;good=gs
    n_in=int(inl.sum());ratio=n_in/max(1,len(good))
    det=A[0,0]*A[1,1]-A[0,1]*A[1,0]
    diag.update(model=kind,inliers=n_in,inlier_ratio=round(ratio,3),det=round(float(det),4),
                scale=round(float(math.sqrt(abs(det))),4),rotation_deg=round(math.degrees(math.atan2(A[1,0],A[0,0])),2))
    # held-out validation: refit on random half of inliers, measure on the other half
    rng=np.random.default_rng(7);idx=np.where(inl)[0];rng.shuffle(idx);half=len(idx)//2
    s_in,d_in=src[idx],dst[idx]
    Mh,_=cv2.estimateAffinePartial2D(s_in[:half],d_in[:half],method=cv2.LMEDS)
    if Mh is None: return {'ok':False,'reason':'Held-out validation fit failed.','diagnostics':diag}
    pr=(s_in[half:]@Mh[:,:2].T+Mh[:,2]);err=np.linalg.norm(pr-d_in[half:],axis=1)*(1.0)
    # express error as fraction of the CAD-raster extent that the page covers (page diag mapped through A)
    scale=math.sqrt(abs(det));cov_diag=diagp*scale
    diag.update(heldout_median_err_px=round(float(np.median(err)),2),heldout_p90_err_px=round(float(np.percentile(err,90)),2),
                heldout_median_frac=round(float(np.median(err))/cov_diag,5))
    allp=(src[inl]@A[:,:2].T+A[:,2]);res=np.linalg.norm(allp-dst[inl],axis=1)
    diag['reproj_median_px']=round(float(np.median(res)),2)
    cov=_coverage(src[inl],pw,ph);diag['coverage']=round(cov,3)
    # coverage in each page third (vertical) so a cluster in one corner cannot pass
    ys=src[inl][:,1];thirds=[int(((ys>=ph*i/3)&(ys<ph*(i+1)/3)).sum()) for i in range(3)];diag['inliers_by_third']=thirds
    # Local support grid: a global transform can be excellent while a markup sits
    # in a poorly-supported corner. Keep a compact 5x5 map for per-markup gating.
    pts=src[inl]
    grid=np.zeros((5,5),dtype=int)
    gx=np.clip((pts[:,0]/max(pw,1)*5).astype(int),0,4)
    gy=np.clip((pts[:,1]/max(ph,1)*5).astype(int),0,4)
    for ix,iy in zip(gx,gy): grid[iy,ix]+=1
    diag['inlier_grid_5x5']=grid.tolist()
    reasons=[]
    if n_in<MIN_INLIERS: reasons.append(f'inliers {n_in}<{MIN_INLIERS}')
    if ratio<MIN_INLIER_RATIO: reasons.append(f'inlier ratio {ratio:.2f}<{MIN_INLIER_RATIO}')
    if cov<MIN_COVERAGE: reasons.append(f'coverage {cov:.2f}<{MIN_COVERAGE}')
    if sum(1 for t in thirds if t>=5)<2: reasons.append('inliers concentrated in one region')
    if diag['heldout_median_frac']>MAX_HELDOUT_ERR_FRAC: reasons.append('held-out error too high')
    if float(np.percentile(err,90))/cov_diag>MAX_HELDOUT_P90_FRAC: reasons.append('held-out p90 error too high')
    if det<=0 and not mirror_ok: reasons.append('transform mirrors the page (reflection)')
    if reasons: return {'ok':False,'reason':'Registration rejected: '+'; '.join(reasons),'diagnostics':diag}
    # page px (original resolution) -> CAD raster px -> world
    S=np.array([[1/psc,0,0],[0,1/psc,0],[0,0,1]]);A3=np.vstack([A,[0,0,1]])
    W=np.vstack([cad_px_to_world,[0,0,1]])
    page_px_to_world=(W@A3@np.linalg.inv(S))[:2]
    diag['page_work_size']=[pw,ph]
    return {'ok':True,'reason':'','page_px_to_cad':page_px_to_world.tolist(),'work_scale':psc,'work_to_cad_px':A.tolist(),'diagnostics':diag}

def page_px_to_cad(result,x,y):
    M=np.array(result['page_px_to_cad']);return [float(M[0,0]*x+M[0,1]*y+M[0,2]),float(M[1,0]*x+M[1,1]*y+M[1,2])]

def to_page_to_cad_affine(result,page_w,page_h):
    """Convert to the existing catalog format [a,b,c,d,tx,ty] over u,v in 0..1000 (CAD_X=a*u+b*v+tx, CAD_Y=c*u+d*v+ty)."""
    M=np.array(result['page_px_to_cad']);sx=page_w/1000.0;sy=page_h/1000.0
    return [float(M[0,0]*sx),float(M[0,1]*sy),float(M[1,0]*sx),float(M[1,1]*sy),float(M[0,2]),float(M[1,2])]
