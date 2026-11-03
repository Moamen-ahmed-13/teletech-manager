"""End-to-end As-Built relocation on a DXF without ezdxf:
   PDF pages -> markup colors -> registration -> endpoint match -> NEW DXF (+ change list for server.py).
   usage: python asbuilt_apply.py --dxf X.dxf --pdf p1.pdf p2.pdf --out out_dir
"""
import sys,os,json,hashlib,math,argparse,pickle
import numpy as np,cv2
HERE=os.path.dirname(os.path.abspath(__file__));sys.path.insert(0,HERE)
import asbuilt_registration as R,asbuilt_arrow as AR,dxfedit,dxfmini
ENDPOINT_BLOCKS={'D-V','D-W','DD','D-V BOX','ES-cctv'}
MATCH_MAX=900.0;AMBIG_RATIO=1.4
HR={'orange':(5,22),'yellow':(23,38),'green':(40,90),'pink':(160,179)}
sha=lambda p:hashlib.sha256(open(p,'rb').read()).hexdigest()
def detect_markups(im):
    hsv=cv2.cvtColor(im,cv2.COLOR_BGR2HSV);H,W=im.shape[:2];out=[]
    for c,(h0,h1) in HR.items():
        m=((hsv[:,:,0]>=h0)&(hsv[:,:,0]<=h1)&(hsv[:,:,1]>=95)&(hsv[:,:,2]>=150)).astype(np.uint8)
        if c=='pink': m|=((hsv[:,:,0]<=3)&(hsv[:,:,1]>=95)&(hsv[:,:,2]>=150)).astype(np.uint8)
        m=cv2.morphologyEx(m,cv2.MORPH_CLOSE,np.ones((9,9),np.uint8))
        n,l,st,ce=cv2.connectedComponentsWithStats(m,8)
        for k in range(1,n):
            x,y,w,h,a=[int(v) for v in st[k]]
            if not 120<=a<=9000: continue
            legend=(x>W*0.70 and y<H*0.20 and w/max(h,1)>=2.0)   # highlighter swatches of the handwritten legend
            out.append({'color':c,'bbox':[x,y,w,h],'center':[float(ce[k][0]),float(ce[k][1])],'area':a,'legend':legend})
    return out
def cad_raster(pl,bbox,width=3000):
    from render import render
    img,s=render(pl,bbox,width,1);return img,np.array([[1/s,0,bbox[0]],[0,-1/s,bbox[3]]])
def run(dxf_path,pdfs,out_dir,cad_cache=None):
    import pypdfium2 as pdfium
    os.makedirs(out_dir,exist_ok=True);h_before=sha(dxf_path)
    D=dxfedit.Dxf(dxf_path);I=D.instances()
    ends=[i for i in I if i['name'] in ENDPOINT_BLOCKS]
    ents,blocks=dxfmini.read_dxf(dxf_path);pl=dxfmini.flatten(ents,blocks)
    xs=np.concatenate([np.array(p)[:,0] for p in pl]);ys=np.concatenate([np.array(p)[:,1] for p in pl])
    bbox=(float(np.percentile(xs,1)),float(np.percentile(ys,1)),float(np.percentile(xs,99)),float(np.percentile(ys,99)))
    cad,px2w=cad_raster(pl,bbox)
    ed=dxfedit.Editor(D);report={'pages':[],'changes':[],'review':[],'applied':[]};used=set()
    for pno,pdf in enumerate(pdfs,1):
        im=pdfium.PdfDocument(pdf)[0].render(scale=1).to_numpy()[:,:,:3].copy()
        reg=R.register_page_to_cad(im,cad,px2w);pg={'page':pno,'pdf':os.path.basename(pdf),'registration_ok':reg['ok'],'reason':reg['reason'],'diag':reg['diagnostics'],'markups':[]}
        report['pages'].append(pg)
        if not reg['ok']:
            report['review'].append({'page':pno,'state':'REGISTRATION_FAILED','reason':reg['reason']});continue
        marks=[m for m in detect_markups(im) if not m['legend']]
        orange=[m for m in marks if m['color']=='orange'];arrows=AR.detect_blue_arrow_tips(im,orange)
        oidx={id(m):i+1 for i,m in enumerate(orange)}
        for m in marks:
            w=R.page_px_to_cad(reg,*m['center']);rec={'color':m['color'],'page_px':[round(v) for v in m['center']],'cad_xy':[round(w[0],1),round(w[1],1)]}
            pg['markups'].append(rec)
            if m['color']=='green': rec['action']='NO_CHANGE (Patch Panel unchanged)';continue
            cand=sorted(((math.hypot(e['world'][0]-w[0],e['world'][1]-w[1]),k) for k,e in enumerate(ends)))
            d1,k1=cand[0];d2=cand[1][0] if len(cand)>1 else 1e9;e=ends[k1]
            rec['match']={'block':e['name'],'depth':e['depth'],'world':[round(v,1) for v in e['world']],'distance':round(d1,1),'next_distance':round(d2,1)}
            def review(state,why): rec['state']=state;rec['reason']=why;report['review'].append({'page':pno,'color':m['color'],'cad_xy':rec['cad_xy'],'state':state,'reason':why})
            if d1>MATCH_MAX: review('NOT_MATCHED',f'nearest endpoint is {d1:.0f} units away (>{MATCH_MAX:.0f})');continue
            if d2<AMBIG_RATIO*d1: review('AMBIGUOUS',f'two endpoints at {d1:.0f} and {d2:.0f} units');continue
            if k1 in used: review('DUPLICATE','endpoint already targeted by another markup');continue
            if m['color'] in('pink','yellow'):
                used.add(k1);ed.delete(e);rec['state']='CONFIRMED'
                report['changes'].append({'op':'SITE_REMOVE','page':pno,'color':m['color'],'patch_action':'SHIFT' if m['color']=='pink' else 'RESERVE','block':e['name'],'world':[round(v,1) for v in e['world']],'dxf_handle_path':[p['start'] for p in e['path']]})
                report['applied'].append({'color':m['color'],'op':'DELETE','block':e['name'],'depth':e['depth'],'world':[round(v,1) for v in e['world']]})
            elif m['color']=='orange':
                a=arrows.get(oidx[id(m)],{});rec['arrow']=a
                if a.get('status')!='CONFIRMED': review('NOT_DETECTED' if a.get('status')=='NOT_FOUND' else a.get('status','NOT_DETECTED'),a.get('reason','arrow not confirmed'));continue
                dest=R.page_px_to_cad(reg,*a['tip_px']);rec['destination_cad']=[round(dest[0],1),round(dest[1],1)]
                used.add(k1);ed.move(e,dest);rec['state']='CONFIRMED'
                report['changes'].append({'op':'MOVE_ENDPOINT','page':pno,'color':'orange','patch_action':'KEEP','block':e['name'],'from':[round(v,1) for v in e['world']],'x':dest[0],'y':dest[1]})
                report['applied'].append({'color':'orange','op':'MOVE','block':e['name'],'depth':e['depth'],'from':[round(v,1) for v in e['world']],'to':[round(dest[0],1),round(dest[1],1)]})
    out=os.path.join(out_dir,os.path.splitext(os.path.basename(dxf_path))[0]+'_AsBuilt.dxf');ed.write(out)
    report['original_sha256']=h_before;report['original_unchanged']=(sha(dxf_path)==h_before);report['output']=out
    json.dump(report,open(os.path.join(out_dir,'asbuilt_report.json'),'w'),ensure_ascii=False,indent=1)
    return report,ed
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--dxf',required=True);ap.add_argument('--pdf',nargs='+',required=True);ap.add_argument('--out',required=True)
    a=ap.parse_args();r,_=run(a.dxf,a.pdf,a.out);print(json.dumps({k:r[k] for k in('applied','review','original_unchanged','output')},ensure_ascii=False,indent=1))
