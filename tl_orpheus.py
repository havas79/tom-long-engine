#!/usr/bin/env python3
"""Re-implementation of the ORPHEUS Player386 music driver (AdLib mode) used by Tom Long, written from the spec in
spec/03_music.md and checked against the original driver (it produces the same OPL2 register writes).

Voice/channel state is kept in byte arrays with the same offsets as the original data segment so the two
can be compared field by field, but every access goes through a named accessor.
"""
import struct, sys, os

OPS = [0, 1, 2, 8, 9, 10, 16, 17, 18]          # operator offset of OPL channel i (modulator; carrier = +3)
U32 = 0xffffffff

def s8(x):  return x - 256 if x & 0x80 else x
def s16(x): x &= 0xffff; return x - 0x10000 if x & 0x8000 else x
def s32(x): x &= U32; return x - (1 << 32) if x & 0x80000000 else x

class Rec:
    """byte-array record with named little-endian fields"""
    F = {}
    def __init__(self, raw): object.__setattr__(self, 'b', bytearray(raw))
    def __getattr__(self, n):
        o, sz = self.F[n]; return int.from_bytes(self.b[o:o + sz], 'little')
    def __setattr__(self, n, v):
        o, sz = self.F[n]; self.b[o:o + sz] = (v & ((1 << (8 * sz)) - 1)).to_bytes(sz, 'little')

class Voice(Rec):
    F = dict(pc=(0, 2), prog=(2, 1), prog0=(3, 1), retpc=(4, 2), retprog=(6, 1), ch=(7, 1), chan=(8, 2),
             note0=(0xa, 1), vel=(0xb, 1), note=(0xc, 1), note2=(0xd, 1), pnote=(0xe, 1),
             wflags=(0xf, 2), wcnt=(0x11, 2), lflags=(0x13, 2), lcnt=(0x15, 1), dur=(0x16, 2),
             car=(0x18, 2), mod=(0x1a, 2), pan=(0x1c, 1), csstep=(0x1d, 2), cstarget=(0x1f, 2), cscnt=(0x21, 1),
             pcnt=(0x22, 1), pstep=(0x23, 4), ptarget=(0x27, 4), vib=(0x2b, 1), vibdepth=(0x2c, 1), vibdelta=(0x2d, 4),
             mlon=(0x38, 1), mlrc=(0x39, 1), mlrate=(0x3a, 1), mlhc=(0x3b, 1), mlhalf=(0x3c, 1), mlstep=(0x3d, 2),
             lper=(0x3f, 2), lstep=(0x41, 2), lcnt2=(0x43, 2), lfo=(0x45, 2), keyon=(0x47, 1), freq=(0x48, 4),
             sampend=(0x55, 1), freqout=(0x63, 4), cartl=(0x67, 1), modtl=(0x68, 1), panout=(0x69, 1),
             m20=(0x74, 1), mksl=(0x75, 1), m60=(0x76, 1), m80=(0x77, 1), me0=(0x78, 1),
             c20=(0x79, 1), cksl=(0x7a, 1), c60=(0x7b, 1), c80=(0x7c, 1), ce0=(0x7d, 1), c0=(0x7e, 1),
             fixlo=(0x7f, 1), fixed=(0x80, 1), fx=(0x81, 1), prio=(0x82, 4), lock=(0x86, 1), idx=(0x88, 1),
             caps=(0x89, 1), op=(0x8a, 1))
class Shadow(Rec):
    F = dict(c20=(0, 1), cTL=(1, 1), cksl=(2, 1), c60=(3, 1), c80=(4, 1), ce0=(5, 1), m20=(6, 1), mTL=(7, 1), mksl=(8, 1),
             m60=(9, 1), m80=(0xa, 1), me0=(0xb, 1), a0=(0xc, 1), b0=(0xd, 1), key=(0xe, 1), c0=(0xf, 1), freq=(0x10, 4))
class Chan(Rec):
    F = dict(prog=(0, 1), last=(1, 1), bend=(2, 2), vol=(4, 1), mod=(5, 1), pan=(6, 1), sus=(7, 1), prev=(8, 1))
class Slot(Rec):
    F = dict(ch=(0, 1), veladd=(1, 1), transp=(2, 1), ptr=(3, 4), cnt=(7, 2), wait=(9, 2))


class Orpheus:
    def __init__(self, dsimage, gm, fx, rand_table, write):
        """dsimage: initial bytes of the driver data segment (for initial values); write(reg,val): OPL output"""
        ds = dsimage
        self.write = write
        self.voices = [Voice(ds[0x32c + 0x90 * i:0x32c + 0x90 * (i + 1)]) for i in range(9)]
        self.shadow = [Shadow(ds[0x14c + 20 * i:0x14c + 20 * (i + 1)]) for i in range(9)]
        self.chans = [Chan(ds[0x20c + 9 * i:0x20c + 9 * (i + 1)]) for i in range(32)]
        self.slots = [Slot(ds[0x95 + 11 * i:0x95 + 11 * (i + 1)]) for i in range(16)]
        self.bd, self.bd_sh = ds[0x152d], ds[0x201]
        self.master = ds[0x204]; self.mute = 0
        self.gm, self.fx = gm, fx
        self.freqtab = struct.unpack_from('<129I', ds, 0x1544)
        self.rand_table = rand_table; self.seed = 0; self.pit1 = 0x12
        self.song = None; self.playing = 0; self.tempo = 0; self.inc = 0; self.acc = 0
        # init (0B8B:000C, AdLib forced) ------------------------------------------
        for i, v in enumerate(self.voices):
            v.op = OPS[i]; v.caps = 2
        for r, val in ((1, 0x20), (4, 0x60), (4, 0x80), (8, 0), (0xbd, 0)):
            write(r, val)
        self.reset()

    # ---------------------------------------------------------------- helpers
    def rand(self, n):
        self.pit1 = (self.pit1 - 1) % 0x13          # the original adds a PIT channel-1 read; any noise is fine
        self.seed = (self.seed + self.pit1) & 0xffff
        return (self.randword(self.seed & 0xff) * n) >> 16

    def randword(self, i):
        # NOTE: the original indexes its 256-byte noise table by BYTE offset and reads a 16-bit word there
        return self.rand_table[i] | (self.rand_table[i + 1] << 8)

    def bank_instr(self, bank, prog2):
        it = struct.unpack_from('<H', bank, 4)[0]
        return struct.unpack_from('<H', bank, it + prog2)[0]

    def note_freq(self, n, fine):
        idx = n * 4
        if idx & 0xfe00: n = 0
        f = self.freqtab[n]
        if fine:
            f = (f + ((fine * (self.freqtab[n + 1] - f)) >> 7)) & U32
        return f

    # ---------------------------------------------------------------- API
    def reset(self):                                   # 0B8B:002A
        self.playing = 0
        for i, v in enumerate(self.voices):
            v.pc = 0xffff; v.prog = 0; v.ch = 0; v.chan = 0; v.note0 = v.vel = v.note = v.note2 = 0
            v.prio = 0; v.lock = 0; v.fx = 0; v.lflags = 0; v.wflags = 0; v.dur = 0
            v.cscnt = 0; v.pcnt = 0; v.vib = 0; v.sampend = 0; v.mlon = 0; v.lper = 0
            v.pan = 0x40; v.car = 0; v.mod = 0; v.keyon = 0; v.retpc = 0xffff; v.idx = i
        for c in self.chans:
            c.bend = 0; c.vol = 0x7f; c.mod = 0; c.pan = 0x40; c.sus = 0

    def set_tempo(self, t):                            # 0B8B:41BA
        self.tempo = t
        self.inc = ((192 * t) // 120 * 65536 // 1024) & 0xffff
        self.acc = 0

    def load_song(self, data):                         # 0B8B:001B
        self.song = data; self.set_tempo(120)
        self.playing = 0
        self.pat_tab, self.sub_tab = struct.unpack_from('<HH', data, 4)

    def _scan_fe(self, p):
        while self.song[p] != 0xfe: p += 4
        s = self.song
        return (s[p + 1] << 16) | (s[p + 2] << 8) | s[p + 3], p + 4

    def start(self, sub, loop):                        # 0B8B:0034
        self.reset()
        self.loopflag = loop
        self.o_start, self.o_loop, self.o_end = struct.unpack_from('<3H', self.song, self.sub_tab + 4 * sub)
        for s in self.slots: s.wait = 0xffff
        _, self.o_end = self._scan_fe(self.o_end)      # end marker FE consumed: end pointer = after it
        t, self.o_ptr = self._scan_fe(self.o_start)
        self.o_now = self.o_target = t
        self.stopped = 0; self.playing = 1

    # ---------------------------------------------------------------- MIDI-like event (0B8B:2A4B / 0D36)
    def event(self, status, d1, d2, cl=0xff, fxpath=False):
        typ, ch = status & 0xf0, status & 0x0f
        if fxpath: ch += 0x10
        elif (self.mute >> ch) & 1: return None
        bank = self.fx if fxpath else self.gm
        c = self.chans[ch]
        note = d1
        if typ == 0x90 and d2:
            prog2 = (c.prog if cl == 0xff else cl) * 2
            while True:
                off = self.bank_instr(bank, prog2)
                if bank[off] != 0x20: break
                k = note * 4 - bank[off + 3] * 4
                if k < 0: k = 0
                note = (note + bank[off + k + 2]) & 0xff
                prog2 = bank[off + k + 1] * 2
            pt = struct.unpack_from('<H', bank, 6)[0] + prog2
            prio, vtype = bank[pt], bank[pt + 1]
            prio32 = (prio << 16) | 0xffff
            v = None
            if vtype == 1:
                for w in self.voices:
                    if w.prog0 == prog2 and w.ch >= 0x80: v = w; break
            if v is None:
                best = U32
                for w in self.voices:
                    if w.prio < best and w.lock != 1 and (w.caps >> vtype) & 1:
                        best = w.prio; v = w
            if v is None: return None
            v.fx = 1 if fxpath else 0
            v.prio = prio32; v.prog = v.prog0 = prog2; v.pc = 0; v.retpc = 0xffff
            v.vel = d2; v.note0 = d1; v.note = v.note2 = note; v.ch = ch; v.chan = ch
            v.pan = 0x40; v.wflags = 0; v.lflags = 0; v.dur = 0
            if not fxpath:
                v.cscnt = 0; v.pcnt = 0; v.vib = 0; v.lper = 0; v.mlon = 0
            c.prev = c.last; c.last = d1
            return v
        if typ in (0x80, 0x90):
            for w in self.voices:
                if w.ch == ch and w.note0 == d1:
                    w.prio &= 0xffff7fff; w.ch |= 0x80; break
            return None
        if typ == 0xc0:
            p = d1
            if ch == 9 and not fxpath:
                dm = struct.unpack_from('<H', bank, 0x10)[0]
                p = bank[dm + p] if dm + p < len(bank) else 0
            c.prog = p; return None
        if typ == 0xe0:
            c.bend = (s8(d2) * s8(d2) - 0x1000); return None
        if typ == 0xb0:
            if d1 == 1: c.mod = d2
            elif d1 == 7: c.vol = d2
            elif d1 == 10: c.pan = d2
            elif d1 == 0x40: c.sus = d2
            elif d1 == 0x79: c.mod = 0; c.vol = 0x7f; c.pan = 0x40; c.sus = 0
            elif not fxpath and d1 == 0x46: return self.event(0xc0 | ch, d2, 0)
            elif not fxpath and d1 == 0x47: c.bend = (s8(d2) * s8(d2) - 0x1000)
        return None

    # ---------------------------------------------------------------- tick
    def rtc(self):
        """one 1024 Hz RTC interrupt"""
        old = self.acc
        self.acc = (self.acc + self.inc) & 0xffff
        if not (s16(old) >= 0 and s16(self.acc) < 0): return  # signed overflow == tick
        self.tick()

    def tick(self):
        for v in self.voices: self.voice_tick(v)
        self.opl_out()
        if self.playing: self.sequencer()

    def release(self, v):
        v.prio &= 0xffff7fff; v.ch |= 0x80

    def voice_tick(self, v):
        bank = self.fx if v.fx else self.gm
        if v.prio: v.prio -= 1
        if v.pcnt:
            v.pcnt -= 1
            if v.pcnt == 0: v.freq = v.ptarget
            else: v.freq += v.pstep
        if v.lper:
            bx = v.lstep
            v.lcnt2 -= 1
            if v.lcnt2 == 0: v.lcnt2 = v.lper; v.lstep = -v.lstep
            v.lfo += bx
        if v.mlon:
            v.mlrc -= 1
            if v.mlrc == 0:
                v.mlrc = v.mlrate; v.mlhc -= 1
                if v.mlhc == 0: v.mlhc = v.mlhalf; v.mlstep = -v.mlstep
                v.mod += v.mlstep
        if v.cscnt:
            v.cscnt -= 1
            if v.cscnt == 0: v.car = v.cstarget
            else: v.car += v.csstep
        if v.pc != 0xffff:
            run = True
            if v.dur:
                v.dur -= 1
                if v.dur == 0: self.release(v)
            if v.wflags:
                f = v.wflags; done = False
                if f & 0x100:
                    if v.wcnt == 0: done = True
                    else: v.wcnt -= 1
                if not done and f & 1 and v.ch >= 0x80: done = True
                if not done and f & 2 and v.sampend == 1: done = True
                if done: v.wflags = 0
                else: run = False
            if run: self.interpret(v, bank)
        self.output(v)

    def interpret(self, v, bank):
        while True:
            base = self.bank_instr(bank, v.prog)
            p = base + v.pc
            op, a, b, c = bank[p], bank[p + 1], bank[p + 2], bank[p + 3]
            nxt = True
            if op == 0x00:
                if v.keyon != 1: v.prio = 0; v.lock = 0
                v.pc = 0xffff; return
            elif op == 0x01:
                if a == 1 and v.retpc != 0xffff:
                    v.pc = v.retpc + 4; v.prog = v.retprog; continue
                if v.keyon == 0: v.prio = 0; v.lock = 0
                v.pc = 0xffff; return
            elif op in (0x02, 0x03):
                if c >= (v.note2 if op == 2 else v.vel): op = 0x06
                else: v.pc += 4; continue
            if op == 0x04:
                n = (b << 8) | c
                if n:
                    n -= 1; v.wcnt = n; v.wflags = 0x100 | a
                    if a & 4: v.wcnt = self.rand(n)
                    v.pc += 4; return
            elif op == 0x05:
                if v.lflags == 0:
                    ax = ((1 if a else 0) << 8) | c
                    if ax == 0: v.lflags = 0; v.pc += 4; continue
                    v.lflags = ax
                    cnt = a
                    if ax & 4: cnt = self._rand_dl(a)
                    v.lcnt = cnt
                f = v.lflags; ex = False
                if f & 0x100:
                    if v.lcnt == 0: ex = True
                    else: v.lcnt -= 1
                if not ex and f & 1 and v.ch >= 0x80: ex = True
                if not ex and f & 2 and v.sampend == 1: ex = True
                if ex: v.lflags = 0; v.pc += 4; continue
                v.pc = b * 4; continue
            elif op == 0x06:
                v.retprog = v.prog; v.retpc = v.pc
                v.pc = b * 4; v.prog = (a * 2) & 0xff; continue
            elif op == 0x07:
                vel = 0 if c & 0x80 else v.vel
                v.pc += 4
                self.event(0x90 | (v.ch & 0x7f), (v.note + b) & 0xff, vel, cl=a)   # (bank detail: see report)
                continue
            elif op in (0x08, 0x09, 0x0a):
                basen = v.note2 if op == 8 else (0 if op == 9 else self.chans[v.chan].prev)
                n = (basen + a) & 0xff
                v.pnote = n
                v.freq = self.note_freq(n, s8(b))
                v.pcnt = 0; v.vib = 0; v.fixed = 0
                if c:
                    v.wflags = 0x100; v.wcnt = c - 1; v.pc += 4; return
            elif op == 0x0b:
                v.fixed = 0
                n = (v.note2 + a) & 0xff
                tgt = self.note_freq(n, s8(b))
                if c == 0:
                    v.pnote = n; v.freq = tgt; v.pcnt = 0; v.vib = 0
                else:
                    v.ptarget = tgt
                    v.pstep = int(s32(tgt - v.freq) / c)       # idiv truncates toward zero
                    v.pcnt = c
            elif op == 0x0c:
                v.fixed = 0
                per = c & 0xfe
                if per:
                    v.lstep = 0xffff // per; v.lper = per; v.lcnt2 = per >> 1; v.lfo = 0
                    v.vib = b | 0x80; v.vibdepth = a
                    n = v.pnote
                    if (n * 4) & 0xfe00: n = 0
                    v.vibdelta = ((self.freqtab[n + 1] - self.freqtab[n]) << 1) & U32
                else:
                    v.vib = 0; v.lper = 0; v.lcnt2 = 0; v.lfo = 0
            elif op in (0x0d, 0x0f, 0x1d):
                sens, add = (a, b) if op != 0x1d else (b, c)
                ax = (v.vel * ((sens + 1) * 2)) & 0xffff
                hi = ((ax >> 8) + add) & 0xff
                if hi >= 0x80: hi = 0x7f
                lvl = hi << 8
                if op == 0x0d: v.car = lvl; v.cscnt = 0
                elif op == 0x1d: v.mod = lvl; v.mlon = 0
                else:
                    v.cstarget = lvl
                    if c == 0: v.car = lvl; v.cscnt = 0
                    else: v.csstep = int(s16(lvl - v.car) / c); v.cscnt = c
            elif op == 0x0e: v.pan = a
            elif op == 0x18:
                if c: v.cscnt = 0; v.pcnt = 0; v.vib = 0; v.mlon = 0
                v.keyon = 1 if a else 0
            elif op == 0x19:
                v.c0 = a | (b << 1); self.bd = c
            elif op == 0x1a:
                r20 = (b & 0xf0) | c; ksl = ((b & 0xc0) << 4) & 0xff; w = b & 3
                if a == 1: v.ce0, v.c20, v.cksl = w, r20, ksl
                else: v.me0, v.m20, v.mksl = w, r20, ksl
            elif op == 0x1b:
                if a == 1: v.c60, v.c80 = b, c
                else: v.m60, v.m80 = b, c
            elif op == 0x1e:
                v.mlhalf = v.mlhc = a; v.mlrc = v.mlrate = c
                v.mlstep = s8(b) if b else 0x80; v.mlon = 1
            elif op == 0x1f:
                v.fixlo = c; v.fixed = 0x80 | ((a & 7) << 2) | (b & 3)
            elif op == 0x22:
                pr = (a << 8) | b
                if pr == 0: v.ch |= 0x80; v.lock = 0
                v.prio = pr
            elif op == 0x24:
                n = (self.rand(c) + a) & 0xff
                v.pnote = n
                fine = 0
                if b:
                    fine = self.rand(b if b < 0x80 else (0xff00 | b))
                    if b >= 0x80: fine |= 0xffff0000
                    fine = s32(fine)
                idx = n if not (n * 4) & 0xfe00 else 0
                f = self.freqtab[idx]
                if fine: f = (f + ((fine * (self.freqtab[idx + 1] - f)) >> 7)) & U32
                v.freq = f; v.pcnt = 0; v.vib = 0; v.fixed = 0
            # 0x10-0x14, 0x23: sample/GUS and host-sync ops (unused by the game's banks) -> ignored here
            v.pc += 4

    def _rand_dl(self, n):
        self.pit1 = (self.pit1 - 1) % 0x13
        self.seed = (self.seed + self.pit1) & 0xffff
        return ((self.randword(self.seed & 0xff) * n) >> 16) & 0xff

    def output(self, v):
        ch = self.chans[v.chan]
        ax, cx = v.car, v.mod
        conn = v.c0 & 1
        if ch.vol != 0x7f:
            ax = (ax * (ch.vol + 1)) >> 7 & 0xffff
            if conn: cx = (cx * (ch.vol + 1)) >> 7 & 0xffff
        if v.fx != 1 and self.master != 0xff:
            ax = (ax * (self.master + 1)) >> 8 & 0xffff
            if conn: cx = (cx * (self.master + 1)) >> 8 & 0xffff
        v.cartl = ax >> 8; v.modtl = cx >> 8
        pan = v.pan + ch.pan - 0x40
        v.panout = 0 if pan < 0 else (0x7f if pan >= 0x80 else pan)
        f = v.freq
        if v.vib:
            depth = ch.mod if v.vib & 1 else v.vibdepth
            a = ((v.vibdelta * (depth + 1)) >> 8) & U32
            f = (f + (((s32(a) * s16(v.lfo)) >> 16) & U32)) & U32
        b = s16(ch.bend)
        if b:
            f = (f + int(s32(f) * b / (91000 if b > 0 else 39000))) & U32
        v.freqout = f

    def opl_out(self):
        w = self.write
        if self.bd != self.bd_sh: self.bd_sh = self.bd; w(0xbd, self.bd)
        for i, v in enumerate(self.voices):
            s = self.shadow[i]; o = v.op
            if v.c0 != s.c0: s.c0 = v.c0; w(0xc0 + i, v.c0)
            for nm, reg in (('c20', 0x23), ('m20', 0x20), ('c60', 0x63), ('m60', 0x60), ('c80', 0x83), ('m80', 0x80),
                            ('ce0', 0xe3), ('me0', 0xe0)):
                val = getattr(v, nm)
                if val != getattr(s, nm): setattr(s, nm, val); w(reg + o, val)
            if v.cksl != s.cksl: s.cksl = v.cksl; s.cTL = 0x80
            if v.mksl != s.mksl: s.mksl = v.mksl; s.mTL = 0x80
            t = (v.cartl >> 1) ^ 0x3f
            if t != s.cTL: s.cTL = t; w(0x43 + o, t | s.cksl)
            t = (v.modtl >> 1) ^ 0x3f
            if t != s.mTL: s.mTL = t; w(0x40 + o, t | s.mksl)
            if v.keyon != s.key: s.key = v.keyon; s.freq = U32; s.b0 = 0xff
            if v.fixed & 0x80:
                dl, al = v.fixed & 0x1f, v.fixlo
            else:
                f = v.freqout
                if f == s.freq: continue
                s.freq = f
                blk = (f >> 16).bit_length() - 1 - 4 if f >> 16 else -4   # bsr; undefined for f < 1 Hz (never audible)
                sh = (20 - blk) & 0x1f
                fn = (((f * (1 << sh)) // 50000) >> 16) & U32
                al = fn & 0xff; dl = ((blk << 2) | ((fn >> 8) & 0xff)) & 0xff
            if al != s.a0: s.a0 = al; w(0xa0 + i, al)
            if dl != s.b0: s.b0 = dl; w(0xb0 + i, (dl | (s.key << 5)) & 0xff)

    def sequencer(self):
        s = self.song
        if not self.stopped:
            if self.o_now != self.o_target:
                self.o_now += 1
            else:
                p = self.o_ptr
                while True:
                    if p == self.o_end:
                        if self.o_loop != self.o_end and self.loopflag == 1:
                            t, self.o_ptr = self._scan_fe(self.o_loop)
                            self.o_now = self.o_target = t
                            return                      # no slot processing on the loop tick
                        self.stopped = 1; break
                    dl = s[p]; p += 1
                    if dl == 0xff: p += 1; self.stopped = 1; self.o_ptr = p; self.o_now += 1; break
                    if dl == 0xfe:
                        self.o_target = (s[p] << 16) | (s[p + 1] << 8) | s[p + 2]; p += 3
                        self.o_ptr = p; self.o_now += 1; break
                    if dl == 0xfd:
                        p += 2; t = s[p]; p += 1
                        if t != self.tempo: self.set_tempo(t)
                        continue
                    if dl == 0xfc:
                        ch = s[p] >> 4; vol = s[p + 1]; prg = s[p + 2]; p += 3
                        if not vol & 0x80: self.event(0xb0 | ch, 7, vol)
                        if not prg & 0x80: self.event(0xc0 | ch, prg, 0)
                        continue
                    if dl >= 0x80: p += 3; continue
                    b1 = s[p]; slot = self.slots[b1 & 15]
                    slot.ch = b1 >> 4; slot.veladd = dl; slot.transp = s[p + 1]
                    po = struct.unpack_from('<H', s, self.pat_tab + 2 * s[p + 2])[0]
                    slot.ptr = po + 2; slot.cnt = 0; slot.wait = struct.unpack_from('<H', s, po)[0]
                    p += 3
        for sl in self.slots:
            if sl.wait == 0xffff: continue
            if sl.cnt != sl.wait: sl.cnt += 1; continue
            q = sl.ptr
            while True:
                bh, bl = s[q], s[q + 1]; q += 2
                if bl & 0x80:
                    note = ((bh & 0x7f) + sl.transp) & 0xff; vel = ((bl & 0x7f) + sl.veladd) & 0xff
                    dur = struct.unpack_from('<H', s, q)[0]; q += 2
                    v = self.event(0x90 | sl.ch, note, vel)
                    if v is not None: v.dur = dur
                else:
                    self.event(0xb0 | sl.ch, bh & 0x7f, bl & 0x7f)
                if not bh & 0x80: break
            sl.wait = struct.unpack_from('<H', s, q)[0]; sl.ptr = q + 2; sl.cnt += 1
