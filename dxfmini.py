"""Minimal read-only ASCII DXF geometry reader (LINE/LWPOLYLINE/POLYLINE/ARC/CIRCLE/INSERT) used only to
rasterize a floor for PDF-to-DXF registration when ezdxf is unavailable."""
import math
def _pairs(path):
    with open(path,'r',encoding='latin-1',errors='replace') as f:
        it=iter(f.read().split('\n'))
    for c in it:
        v=next(it,None)
        if v is None: break
        try: yield int(c.strip()), v.strip()
        except ValueError: continue
def read_dxf(path):
    ents=[];blocks={};cur=None;sec=None;e=None;poly=None;pend=None
    def push(ent):
        if ent is None: return
        (blocks[cur]['e'] if cur else ents).append(ent)
    P=list(_pairs(path));i=0;n=len(P)
    while i<n:
        c,v=P[i];i+=1
        if c==0:
            if e is not None and e['t'] not in('POLYLINE','VERTEX','SEQEND'): push(e)
            if v=='SECTION': e=None;continue
            if v=='ENDSEC': sec=None;cur=None;e=None;continue
            if v=='BLOCK': cur='?';blocks['?']={'e':[],'base':(0,0)};e={'t':'BLOCKHDR'};continue
            if v=='ENDBLK': cur=None;e=None;continue
            if v=='POLYLINE': poly={'t':'LWPOLYLINE','pts':[],'closed':False,'layer':''};e=poly;continue
            if v=='VERTEX': e={'t':'VERTEX'};continue
            if v=='SEQEND':
                if poly: push(poly);poly=None
                e=None;continue
            e={'t':v,'pts':[],'layer':''} if v in('LINE','LWPOLYLINE','ARC','CIRCLE','INSERT') else {'t':'SKIP'}
            continue
        if e is None: 
            if c==2 and sec is None: sec=v
            continue
        t=e['t']
        if t=='BLOCKHDR':
            if c==2:
                blocks[v]=blocks.pop('?');cur=v
            elif c in(10,20) and cur:
                b=blocks[cur]['base'];blocks[cur]['base']=(float(v),b[1]) if c==10 else (b[0],float(v))
            continue
        if t=='VERTEX':
            if c==10: poly['pts'].append([float(v),0.0])
            elif c==20 and poly['pts']: poly['pts'][-1][1]=float(v)
            continue
        if c==8: e['layer']=v
        if t=='LINE':
            if c in(10,20,11,21): e[c]=float(v)
        elif t=='LWPOLYLINE':
            if c==10: e['pts'].append([float(v),0.0])
            elif c==20 and e['pts']: e['pts'][-1][1]=float(v)
            elif c==70: e['closed']=bool(int(v)&1)
        elif t in('ARC','CIRCLE'):
            if c in(10,20,40,50,51): e[c]=float(v)
        elif t=='INSERT':
            if c==2: e['name']=v
            elif c in(10,20,41,42,50): e[c]=float(v)
    return ents,blocks
def flatten(ents,blocks,depth=0,M=(1,0,0,1,0,0),out=None,maxdepth=8):
    """returns list of polylines (Nx2 lists) in world coords; M=(a,b,c,d,tx,ty): x'=a*x+c*y+tx"""
    if out is None: out=[]
    a,b,c,d,tx,ty=M
    T=lambda x,y:(a*x+c*y+tx,b*x+d*y+ty)
    for e in ents:
        t=e['t']
        if t=='LINE' and 10 in e and 11 in e:
            out.append([T(e[10],e.get(20,0)),T(e[11],e.get(21,0))])
        elif t=='LWPOLYLINE' and len(e['pts'])>1:
            p=[T(x,y) for x,y in e['pts']]
            if e.get('closed'): p.append(p[0])
            out.append(p)
        elif t=='CIRCLE' and 10 in e and 40 in e:
            out.append([T(e[10]+e[40]*math.cos(k*math.pi/12),e[20]+e[40]*math.sin(k*math.pi/12)) for k in range(25)])
        elif t=='ARC' and 10 in e and 40 in e:
            s=math.radians(e.get(50,0));en=math.radians(e.get(51,360))
            if en<=s: en+=2*math.pi
            m=12;out.append([T(e[10]+e[40]*math.cos(s+(en-s)*k/m),e[20]+e[40]*math.sin(s+(en-s)*k/m)) for k in range(m+1)])
        elif t=='INSERT' and depth<maxdepth and e.get('name') in blocks:
            bl=blocks[e['name']];sx=e.get(41,1.0);sy=e.get(42,sx);r=math.radians(e.get(50,0))
            cr,sr=math.cos(r),math.sin(r);bx,by=bl['base']
            # local->parent: p=R*S*(q-base)+ins ; then parent transform M
            la,lb,lc,ld=cr*sx,sr*sx,-sr*sy,cr*sy
            ltx=e.get(10,0)-(la*bx+lc*by);lty=e.get(20,0)-(lb*bx+ld*by)
            N=(a*la+c*lb,b*la+d*lb,a*lc+c*ld,b*lc+d*ld,a*ltx+c*lty+tx,b*ltx+d*lty+ty)
            flatten(bl['e'],blocks,depth+1,N,out,maxdepth)
    return out
