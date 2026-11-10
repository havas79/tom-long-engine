"""Faithful model of ENGINE.EXE 037B path finder, incl. the vis-matrix/path-array overlap."""
MASK=[0x80,0x40,0x20,0x10,8,4,2,1]
MAXLEN=720
def tdiv(a,b):
    q=abs(a)//abs(b); return q if (a>=0)==(b>0) else -q
def s16(v): v&=0xffff; return v-0x10000 if v&0x8000 else v
class PathFinder:
    def __init__(s,bpr,rows,bm,mem_oob=None):
        s.bpr,s.rows,s.bm=bpr,rows,bm
        s.X=[0]*MAXLEN; s.Y=[0]*MAXLEN; s.D=[0]*MAXLEN; s.n=0
        s.lgx=0; s.lgy=0               # DGROUP 32E8/32EA (BSS, starts 0)
        s.way=[]; s.vis0=[0]*10        # only row 0 of the 10x10 matrix survives (bytes 0..9)
    # --- byte k of the 100-byte visibility matrix at DGROUP 32DE (overlaps 32E8.. = lastgood, X[]) ---
    def visbyte(s,k):
        if k<10: return s.vis0[k]
        k-=10
        words=[s.lgx,s.lgy]+s.X
        w=words[k//2]&0xffff
        return (w>>8) if k&1 else (w&0xff)
    def ok(s,x,y):
        i=y*s.bpr+tdiv(x,8)
        if i<0 or i>=len(s.bm): return False
        return bool(s.bm[i]&MASK[x&7])
    def los(s,x0,y0,x1,y1):
        si,di=x0,y0; sx=sy=1; end=x1; dx=x1-x0; dy=y1-y0; steep=False
        if dx<0: sx=-1; dx=-dx
        if dy<0: sy=-1; dy=-dy
        if dx<dy: si,di=di,si; dx,dy=dy,dx; sx,sy=sy,sx; end=y1; steep=True
        e1=dy>>1; e2=(dx>>1)-e1; c=e1-dx
        P=(lambda: s.ok(di,si)) if steep else (lambda: s.ok(si,di))
        if not P(): return -1
        while si!=end:
            if steep: s.lgx,s.lgy=di,si
            else: s.lgx,s.lgy=si,di
            si+=sx
            if c>0: di+=sy; c-=e2
            else: c+=e1
            if not P(): return -1
        return 0
    def put(s,x,y,d):
        s.X[s.n]=x; s.Y[s.n]=y; s.D[s.n]=d; s.n+=1
        return s.n<MAXLEN
    def seg(s,x0,y0,x1,y1):
        sx=sy=1; end=x1; dx=x1-x0; dy=y1-y0
        if dx<0: sx=-1; dx=-dx
        if dy<0: sy=-1; dy=-dy
        if dx<dy: d=2 if sy==-1 else 3
        else: d=1 if sx==-1 else 0
        maj,mn,steep=x0,y0,False
        if dx<dy: maj,mn=y0,x0; dx,dy=dy,dx; sx,sy=sy,sx; end=y1; steep=True
        e1=dy>>1; e2=(dx>>1)-e1; c=e1-dx
        xy=lambda: (mn,maj) if steep else (maj,mn)
        if not s.put(*xy(),d): return
        while maj!=end:
            maj+=sx
            if c>0: mn+=sy; c-=e2
            else: c+=e1
            if not s.put(*xy(),d): return
        s.put(x1,y1,d)
    def snap(s,x,y):           # 037B:0549, faithful incl. its addressing bug
        for r in (1,2,3,4):
            for di in range(-r,r+1):
                for si in range(-r,r+1):
                    i=si*s.bpr+y+x+tdiv(di,8)
                    if 0<=i<len(s.bm) and s.bm[i]&MASK[(x+di)&7]: return x+di,y+si
        return x,y
    def add_way(s,x,y):        # 037B:0E5D (ROM op 12h) + 0DC9
        if len(s.way)>=10 or not s.ok(x,y): return
        s.way.append((x,y)); s.vis0=[0]*10
        n=len(s.way)
        if n==1: s.vis0[0]=1; return
        for i in range(n):
            for j in range(n):
                if s.los(*s.way[i],*s.way[j])==0:
                    k=i*10+j
                    if k<10: s.vis0[k]=1
                    # writes to k>=10 land in lastgood/X[] and are overwritten later
    def find(s,x0,y0,x1,y1,sx,sy,W):
        """037B:071C. returns reason; path in X/Y/D[0:n]; n may be -1 (caller sets 0)."""
        s.n=0; ox,oy=x0,y0
        if (x0,y0)==(x1,y1): return 'same'
        if not s.ok(x0,y0): x0,y0=s.snap(x0,y0)
        if not s.ok(x1,y1):
            s.lgx=-1; s.los(x0,y0,x1,y1)
            if s.lgx==-1: return 'start-blocked'
            s.seg(x0,y0,s.lgx,s.lgy); return 'target-unwalkable'
        s.lgx=-1; found=False
        if s.los(x0,y0,x1,y1)==0: s.seg(x0,y0,x1,y1); return 'direct'
        stx,sty=ox,oy; px=py=None
        if s.lgx!=-1:
            found=True; px,py=s.lgx,s.lgy
            if abs(x0-s.lgx)<2 and abs(y0-s.lgy)<2:
                best2=0
                for dx in range(-5,6):
                    for dy in range(-5,6):
                        nx,ny=x0+dx,y0+dy
                        if s.los(x0,y0,nx,ny)!=0: continue
                        s.lgx=-1
                        if ny<0 or s.rows<ny: continue
                        if s.los(nx,ny,x1,y1)==0: s.seg(nx,ny,x1,y1); return 'neighbour'
                        if s.lgx==-1: continue
                        d=(x0-s.lgx)**2+(y0-s.lgy)**2
                        if d>best2: best2=d; found=True; stx,sty=nx,ny; px,py=s.lgx,s.lgy
        best=10000000; via=None
        gx=sx
        while gx<sx+320:
            if gx>W: break
            gy=sy
            while gy<sy+220:
                if s.rows<gy: break
                if s.los(x0,y0,gx,gy)==0 and s.los(gx,gy,x1,y1)==0:
                    d=(y1-gy)**2+(gy-y0)**2+(gx-x0)**2+(x1-gx)**2
                    if d<best: best=d; via=(gx,gy)
                gy+=8
            gx+=8
        if best<10000000: s.seg(x0,y0,*via); s.seg(*via,x1,y1); return 'grid'
        for (wx,wy) in s.way:
            if s.los(x0,y0,wx,wy)!=0: continue
            if s.los(wx,wy,x1,y1)!=0: continue
            d=(y1-wy)**2+(wy-y0)**2+(wx-x0)**2+(x1-wx)**2
            if d<best: best=d; via=(wx,wy)
        if best<10000000: s.seg(x0,y0,*via); s.seg(*via,x1,y1); return 'waypoint'
        if len(s.way)>1:
            A=[];B=[]
            for i,(wx,wy) in enumerate(s.way):
                if s.los(x0,y0,wx,wy)==0: A.append(i)
                if s.los(x1,y1,wx,wy)==0: B.append(i)
            if len(A)==1 and len(B)==1 and s.visbyte(A[0]*10+B[0]):
                a,b=s.way[A[0]],s.way[B[0]]
                s.seg(x0,y0,*a); s.seg(*a,*b); s.seg(*b,x1,y1); return 'two-waypoints'
        if found:
            s.seg(stx,sty,px,py); return 'partial'
        s.n=-1; return 'fail'
    def path(s): return [(s.X[i],s.Y[i],s.D[i]) for i in range(max(s.n,0))]
