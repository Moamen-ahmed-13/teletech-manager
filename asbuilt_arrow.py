"""Blue-ballpoint relocation-arrow detector for Orange As-Built markups.

detect_blue_arrow_tips(image_path_or_bgr, orange_regions) -> {orange_index(1-based): result}
result keys: status (CONFIRMED|AMBIGUOUS|NOT_FOUND|NOT_CONFIRMED), confidence, tip_px, tip_norm,
tail_px, arrowhead_detected, head_score, stroke_bbox, stroke_length_px, orange_to_stroke_gap_px,
candidates (diagnostics), reason.  Only CONFIRMED may be used to move CAD geometry.
"""
import math
import numpy as np
import cv2
from skimage.morphology import skeletonize

def _blue_mask(bgr):
    hsv=cv2.cvtColor(bgr,cv2.COLOR_BGR2HSV)
    h,s,v=hsv[:,:,0],hsv[:,:,1],hsv[:,:,2]
    # ballpoint blue ink: hue ~100-135, moderately saturated, not paper-white; includes dark navy ink
    return ((h>=85)&(h<=140)&(s>=45)&(v>=30)&(v<=245)).astype(np.uint8)


def _local_groups(bgr,ox,oy,ow,oh,pm,others):
    """Relaxed local blue mask around one orange marker; fragments (broken ballpoint strokes) are chained
    into groups when their pixel gap is small. Returns [(xs,ys,gap_to_orange)] in full-image coords."""
    H,W=bgr.shape[:2];pad=int(pm*0.45)
    x0=max(0,int(ox-pad));y0=max(0,int(oy-pad));x1=min(W,int(ox+ow+pad));y1=min(H,int(oy+oh+pad))
    c=bgr[y0:y1,x0:x1];hsv=cv2.cvtColor(c,cv2.COLOR_BGR2HSV)
    v=hsv[:,:,2];paper=cv2.medianBlur(v,31)
    b,g,r=[c[:,:,i].astype(np.int16) for i in range(3)]
    blueness=b-(r+g)//2
    darker=(v.astype(np.int16)<paper.astype(np.int16)-18)
    # Weak mask keeps the thin/faint parts of a ballpoint stroke; strong mask is real blue ink.
    # Grey/purple-tinted scan noise (black wall lines, hatch) passes the weak mask but never the strong one,
    # so only weak components that contain strong ink are kept.
    weak=((hsv[:,:,0]>=82)&(hsv[:,:,0]<=145)&(hsv[:,:,1]>=12)&(blueness>=4)&darker).astype(np.uint8)
    strong=((hsv[:,:,0]>=92)&(hsv[:,:,0]<=142)&(hsv[:,:,1]>=25)&(blueness>=10)&darker).astype(np.uint8)
    wn,wlab=cv2.connectedComponents(weak,connectivity=8)
    keep=np.zeros(wn,bool)
    if wn>1:
        keep[np.unique(wlab[strong>0])]=True
        keep[0]=False
    m=keep[wlab].astype(np.uint8)
    for bb in [(ox,oy,ow,oh)]+others:
        bx,by,bw,bh=[int(v_) for v_ in bb];m[max(0,by-3-y0):max(0,by+bh+3-y0),max(0,bx-3-x0):max(0,bx+bw+3-x0)]=0
    n,lab,st,_=cv2.connectedComponentsWithStats(m,8)
    comps=[]
    for k in range(1,n):
        if st[k,4]<5: continue
        ys,xs=np.where(lab==k);comps.append((xs+x0,ys+y0))
    if not comps: return []
    link=max(30.0,pm*0.04);par=list(range(len(comps)))
    def f(i):
        while par[i]!=i: par[i]=par[par[i]];i=par[i]
        return i
    bridges=[]
    for i in range(len(comps)):
        for j in range(i+1,len(comps)):
            a=np.stack(comps[i],1).astype(np.float32);b_=np.stack(comps[j],1).astype(np.float32)
            if abs(a[:,0].mean()-b_[:,0].mean())>pm*0.42 or abs(a[:,1].mean()-b_[:,1].mean())>pm*0.42: continue
            d=np.linalg.norm(a[:,None,:]-b_[None,:,:],axis=2) if len(a)*len(b_)<250000 else None
            if d is None: continue
            ia,ib=np.unravel_index(np.argmin(d),d.shape)
            if d[ia,ib]<=link:
                par[f(i)]=f(j);bridges.append((tuple(a[ia]),tuple(b_[ib])))
    groups={}
    for i,cp in enumerate(comps): groups.setdefault(f(i),[]).append(i)
    out=[]
    for root,idxs in groups.items():
        xs=np.concatenate([comps[i][0] for i in idxs]);ys=np.concatenate([comps[i][1] for i in idxs])
        # draw bridge lines so the stroke is one connected skeleton
        bx=[];by=[]
        for p,q in bridges:
            if any(np.any((comps[i][0]==p[0])&(comps[i][1]==p[1])) for i in idxs):
                nn=int(max(abs(p[0]-q[0]),abs(p[1]-q[1])))+1
                bx+=list(np.linspace(p[0],q[0],nn).round().astype(int));by+=list(np.linspace(p[1],q[1],nn).round().astype(int))
        if bx: xs=np.concatenate([xs,np.array(bx)]);ys=np.concatenate([ys,np.array(by)])
        ddx=np.maximum(np.maximum(ox-xs,0),xs-(ox+ow));ddy=np.maximum(np.maximum(oy-ys,0),ys-(oy+oh))
        out.append((xs.astype(int),ys.astype(int),float(np.min(np.hypot(ddx,ddy)))))
    return out

def _endpoints(sk):
    k=cv2.filter2D(sk.astype(np.uint8),-1,np.ones((3,3),np.float32),borderType=cv2.BORDER_CONSTANT)
    ys,xs=np.where((sk>0)&(k==2))
    return list(zip(xs.tolist(),ys.tolist()))

def detect_blue_arrow_tips(image, orange_regions):
    bgr=image if isinstance(image,np.ndarray) else cv2.imread(str(image),cv2.IMREAD_COLOR)
    if bgr is None: return {}
    H,W=bgr.shape[:2];pm=float(min(H,W))
    blue=_blue_mask(bgr)
    # remove the orange marker's own halo (hue overlap on some scans) by excluding pixels inside orange bboxes
    regs=[r.get('bbox') for r in (orange_regions or [])]
    for bb in regs:
        if bb:
            x,y,w,h=[int(v) for v in bb[:4]];blue[max(0,y):y+h,max(0,x):x+w]=0
    gap_px=max(4,int(pm*0.0025))|1
    bridged=cv2.morphologyEx(blue,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(gap_px*2+1,gap_px*2+1)))
    n,lab,st,_=cv2.connectedComponentsWithStats(bridged,8)
    centers=[]
    for bb in regs:
        x,y,w,h=[float(v) for v in (bb or [0,0,0,0])[:4]];centers.append((x+w/2,y+h/2,max(w,h)/2))
    out={}
    for oi,bb in enumerate(regs,1):
        if not bb: out[oi]={'status':'NOT_FOUND','confidence':0.0,'reason':'Orange region has no bbox.'};continue
        ox,oy,ow,oh=[float(v) for v in bb[:4]];ocx,ocy=ox+ow/2,oy+oh/2
        max_gap=max(35.0,pm*0.018);max_len=pm*0.48;min_len=max(20.0,pm*0.007)
        cands=[]
        others=[bj for oj,bj in enumerate(regs,1) if oj!=oi and bj]
        for gi,(xs,ys,gap) in enumerate(_local_groups(bgr,ox,oy,ow,oh,pm,others)):
            if gap>max_gap or len(xs)<12: continue
            x,y=int(xs.min()),int(ys.min());w=int(xs.max()-x+1);h=int(ys.max()-y+1)
            if max(w,h)<min_len*0.7: continue
            stolen=False
            for bj in others:
                ax,ay,aw,ah=[float(v) for v in bj[:4]]
                g2=float(np.min(np.hypot(np.maximum(np.maximum(ax-xs,0),xs-(ax+aw)),np.maximum(np.maximum(ay-ys,0),ys-(ay+ah)))))
                if g2+3<gap: stolen=True;break
            if stolen: continue
            cands.append((gi,x,y,w,h,gap,xs,ys))
        if not cands:
            out[oi]={'status':'NOT_FOUND','confidence':0.0,'arrowhead_detected':False,'reason':'No blue-ink stroke starts near this orange markup.'};continue
        scored=[]
        for k,x,y,w,h,gap,xs,ys in cands:
            m=np.zeros((h+2*6,w+2*6),np.uint8);m[ys-y+6,xs-x+6]=1
            m=cv2.morphologyEx(m,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
            sk=skeletonize(m>0).astype(np.uint8)
            eps=_endpoints(sk)
            raw_ink=blue[max(0,y-6):y+h+6,max(0,x-6):x+w+6]
            if len(eps)<2: continue
            # tail = skeleton end nearest the orange; head candidates = the other ends
            def dist_o(p): return math.hypot(p[0]+x-6-ocx,p[1]+y-6-ocy)
            eps.sort(key=dist_o);tail=eps[0];heads=eps[1:]
            skp=np.argwhere(sk>0);L=float(len(skp))
            dt=cv2.distanceTransform(m,cv2.DIST_L2,3)
            shaft_w=float(np.median(dt[sk>0]))+1e-6
            hr=max(8,int(pm*0.011))
            best=None
            for hp in heads:
                hx,hy=hp
                # head score: local ink mass around the endpoint vs shaft ink mass over the same length
                yy,xx=np.ogrid[:m.shape[0],:m.shape[1]]
                disk=((xx-hx)**2+(yy-hy)**2)<=hr*hr
                head_mass=float(m[disk].sum());shaft_mass=float(2*hr*2*shaft_w)
                ratio=head_mass/max(shaft_mass,1.0)
                # direction: from skeleton points ~2*hr behind the endpoint toward the endpoint
                d=np.hypot(skp[:,1]-hx,skp[:,0]-hy);beh=skp[(d>hr*0.8)&(d<hr*2.6)]
                if len(beh)<3: continue
                bx=float(beh[:,1].mean());by=float(beh[:,0].mean())
                vx,vy=hx-bx,hy-by;nv=math.hypot(vx,vy)
                if nv<1: continue
                vx/=nv;vy/=nv
                # tip = farthest ink pixel of the head blob along the travel direction
                hy_,hx_=np.where(m*disk.astype(np.uint8)>0)
                proj=(hx_-hx)*vx+(hy_-hy)*vy
                j=int(np.argmax(proj));tipx=float(hx_[j]);tipy=float(hy_[j])
                # triangular head spreads laterally: width across the axis near the tip
                lat=np.abs((hx_-hx)*(-vy)+(hy_-hy)*vx);spread=float(np.percentile(lat,90)*2)
                # Ballpoint arrows are often drawn as an open V: the two thin head strokes
                # add lateral spread but almost no filled ink mass. The old mass-only
                # threshold rejected exactly this common hand-drawn shape.
                open_v_head = (ratio >= 0.85 and spread >= max(7.0, 5.0*shaft_w))
                filled_head = (ratio >= 1.35 and spread >= 1.8*shaft_w)
                head = bool(filled_head or open_v_head)
                # Keep mass as supporting evidence, but let a clear wide V geometry
                # contribute strongly even when the head is not filled in.
                score=min(1.0,ratio/1.8)*0.25+min(1.0,spread/(4.5*shaft_w))*0.75
                if best is None or score>best[0]: best=(score,tipx+x-6,tipy+y-6,head,ratio,spread/shaft_w,(vx,vy))
            if best is None: continue
            score,tx,ty,head,ratio,sp,vec=best
            length=math.hypot(tx-(tail[0]+x-6),ty-(tail[1]+y-6))
            if length<min_len or length>max_len: continue
            # direction must lead away from the orange marker
            away=(tx-ocx)*vec[0]+(ty-ocy)*vec[1]
            conf=0.35*score+0.25*min(1.0,length/(pm*0.05))+0.20*max(0.0,1-gap/max_gap)+0.20*(1.0 if head else 0.0)
            if away<=0: conf*=0.4
            scored.append(dict(conf=conf,tip=(tx,ty),tail=(tail[0]+x-6,tail[1]+y-6),head=bool(head),length=length,gap=gap,
                               bbox=[x,y,w,h],head_ratio=round(ratio,2),head_spread=round(sp,2),away=bool(away>0)))
        if not scored:
            out[oi]={'status':'NOT_FOUND','confidence':0.0,'arrowhead_detected':False,'reason':'Blue strokes near orange have no usable stroke end.'};continue
        scored.sort(key=lambda z:-z['conf']);b=scored[0]
        amb=len(scored)>1 and scored[1]['head'] and (b['conf']-scored[1]['conf'])<0.12
        ok=(b['head'] and b['away'] and b['conf']>=0.70 and b['gap']<=max_gap and not amb)
        res={'status':'CONFIRMED' if ok else ('AMBIGUOUS' if amb else 'NOT_CONFIRMED'),'confidence':round(float(b['conf']),2),
             'detection_confidence':round(float(b['conf']),2),'arrowhead_confidence':round(float(b['head_ratio']/(b['head_ratio']+1.0)),2),
             'arrowhead_detected':b['head'],'tip_px':[int(round(b['tip'][0])),int(round(b['tip'][1]))],
             'tip_norm':[int(round(b['tip'][0]/W*1000)),int(round(b['tip'][1]/H*1000))],
             'tail_px':[int(b['tail'][0]),int(b['tail'][1])],'stroke_bbox':b['bbox'],'stroke_length_px':round(b['length'],1),
             'orange_to_stroke_gap_px':round(b['gap'],1),'head_ratio':b['head_ratio'],'head_spread':b['head_spread'],
             'origin':'orange_markup','candidates':len(scored),'max_allowed_gap_px':round(float(max_gap),1),
             'geometry_valid':bool(b['head'] and b['away'] and b['gap']<=max_gap)}
        res['reason']=('Arrowhead found at stroke end; destination is the head tip.' if ok else
                       ('Two similar arrow candidates near this orange markup.' if amb else
                        'Stroke found but geometry, arrowhead, direction, or orange-to-stroke gap failed conservative confirmation.'))
        out[oi]=res
    return out
