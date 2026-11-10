
# ══════════════════════════════════════════════════════════════════════════
# Game formats: decoders, encoders, container rebuilders
# ══════════════════════════════════════════════════════════════════════════

import struct

PIC_TYPES = {0: (320, 240), 1: (320, 144), 2: (320, 160), 3: (320, 400), 4: (320, 200),
             5: (480, 160), 6: (480, 80), 7: (352, 160), 8: (400, 160), 9: (640, 160),
             11: (32, 32), 12: (16, 16)}


def vga_to_rgb(pal: bytes) -> list[tuple[int, int, int]]:
    """6-bit VGA palette → 8-bit RGB. The DAC ignores the top two bits (1082:00c0)."""
    return [tuple(((pal[i * 3 + k] & 0x3F) * 255) // 63 for k in range(3)) for i in range(len(pal) // 3)]


# ── PCLL picture library (SYSTEM.PLB, GRAFIK.PLB) ───────────────────────────

def plb_entries(d: bytes):
    """[(type, w, h, palette_offset, pixel_offset)] — 1014:0002 / 1014:00ef."""
    if d[:4] != b"PCLL":
        raise ValueError("not a PCLL library")
    out = []
    for i in range(d[6] + 1):
        t, off = struct.unpack_from("<B3xI", d, 0x10 + 8 * i)
        w, h = PIC_TYPES[t]
        p = 0x810 + off
        out.append((t, w, h, p, p + 768))
    return out


# ── RLE sprite frames (NPC, ANIMS.ANI, item room sprites) ───────────────────

def sprite_decode(d: bytes, p: int):
    """→ (w, h, x, y, pixels row-major). Blitter 1147:000e: Mode-X planes, 0x01 n v = run."""
    w, h, x, y = struct.unpack_from("<HHhh", d, p)
    q = p + 8; buf = bytearray()
    while len(buf) < w * h:
        c = d[q]; q += 1
        if c == 1:
            buf += bytes([d[q + 1]]) * d[q]; q += 2
        else:
            buf.append(c)
    out = bytearray(w * h); i = 0; cols = w // 4
    for plane in range(4):
        for r in range(h):
            for c in range(cols):
                out[r * w + c * 4 + plane] = buf[i]; i += 1
    return w, h, x, y, bytes(out)


def sprite_encode(w: int, h: int, x: int, y: int, pixels: bytes) -> bytes:
    """Inverse of sprite_decode. Runs of ≥3 (and any colour-1 pixel, since 0x01 is the run
    marker) become 01 n v; runs never cross a plane boundary, like the originals."""
    if w % 4:
        raise ValueError("sprite width must be a multiple of 4")
    cols = w // 4
    out = bytearray(struct.pack("<HHhh", w, h, x, y))
    for plane in range(4):
        stream = bytes(pixels[r * w + c * 4 + plane] for r in range(h) for c in range(cols))
        i = 0; n = len(stream)
        while i < n:
            v = stream[i]; j = i
            while j < n and stream[j] == v and j - i < 255:
                j += 1
            run = j - i
            if run >= 3 or v == 1:
                out += bytes((1, run, v)); i = j
            else:
                out.append(v); i += 1
    return bytes(out) + b"\0\0"          # the originals end every frame with 2 slack bytes


# ── NPC ─────────────────────────────────────────────────────────────────────

def npc_frames(d: bytes) -> list[list[int]]:
    """5 records × 166 bytes: u16 frames, u16 w, u16 h, u32 offset[40] — 071d:0076."""
    return [list(struct.unpack_from("<40I", d, r * 166 + 6)[:struct.unpack_from("<H", d, r * 166)[0]])
            for r in range(5)]


def npc_rebuild(d: bytes, new_frames: dict[int, bytes]) -> bytes:
    """Replace frames (keyed by original file offset) and relink the offset tables."""
    recs = npc_frames(d)
    order = sorted({o for r in recs for o in r})
    ends = order[1:] + [len(d)]
    head = bytearray(d[:0x33E]); body = bytearray(); newoff = {}
    for o, e in zip(order, ends):
        newoff[o] = 0x33E + len(body)
        body += new_frames.get(o, d[o:e])
    for r, offs in enumerate(recs):
        for k, o in enumerate(offs):
            struct.pack_into("<I", head, r * 166 + 6 + 4 * k, newoff[o])
    return bytes(head + body)


# ── ANIMS.ANI ───────────────────────────────────────────────────────────────

def ani_index(d: bytes):
    return list(struct.unpack_from("<256I", d, 0)), list(struct.unpack_from("<256I", d, 0x400))


def ani_record(d: bytes, i: int):
    """→ (frame_count, steps[(frame,dx,dy)], flags, [absolute frame offsets]) — 084b:0066."""
    offs, sizes = ani_index(d)
    o, s = offs[i], sizes[i]
    nf, ns, fl = struct.unpack_from("<3H", d, o)
    steps = list(zip(*(struct.unpack_from(f"<{ns}h", d, o + 6 + k * 2 * ns) for k in range(3))))
    rel = struct.unpack_from(f"<{nf}I", d, o + s - 4 * nf)
    return nf, steps, fl, [o + r for r in rel]


def ani_rebuild(d: bytes, new_frames: dict[int, bytes]) -> bytes:
    """Replace frames (keyed by absolute original offset); rebuild records and the index."""
    offs, sizes = ani_index(d)
    used = sorted((offs[i], i) for i in range(256) if sizes[i])
    out = bytearray(d[:0x800]); new_offs = [0] * 256; new_sizes = [0] * 256
    for o, i in used:
        s = sizes[i]
        nf, ns = struct.unpack_from("<2H", d, o)
        head_len = 6 + 6 * ns
        rel = list(struct.unpack_from(f"<{nf}I", d, o + s - 4 * nf))
        order = sorted(set(rel))
        ends = order[1:] + [s - 4 * nf]
        rec = bytearray(d[o:o + head_len]); newrel = {}
        if order and order[0] > head_len:                 # keep any bytes before the first frame
            rec += d[o + head_len:o + order[0]]
        for r, e in zip(order, ends):
            newrel[r] = len(rec)
            rec += new_frames.get(o + r, d[o + r:o + e])
        rec += struct.pack(f"<{nf}I", *[newrel[r] for r in rel])
        new_offs[i] = len(out); new_sizes[i] = len(rec)
        out += rec
    struct.pack_into("<256I", out, 0, *new_offs)
    struct.pack_into("<256I", out, 0x400, *new_sizes)
    return bytes(out)


# ── ANM (cursors, verb buttons) ─────────────────────────────────────────────

def anm_parse(d: bytes):
    if d[:4] != b"ANIM":
        raise ValueError("no ANIM header")
    n, w, h = struct.unpack_from("<3H", d, 4)
    return n, w, h, [10 + k * w * h for k in range(n)]


# ── ITEMS.ITM (icons, room sprites) ─────────────────────────────────────────

VM_LEN = {0xc0: 1, 0xc1: 0x1d, 0xc2: 1, 0xc3: 2, 0xc4: 3, 0xc5: 1, 0xc6: 2, 0xc7: 3, 0xc8: 3, 0xc9: 3, 0xca: 9,
          0xcb: 3, 0xcc: 1, 0xcd: 1, 0xce: 3, 0xcf: 3, 0xd0: 1, 0xd1: 3, 0xd2: 3, 0xd3: 0xc, 0xd4: 3, 0xd5: 1,
          0xd6: 1, 0xd7: 1, 0xd8: 1, 0xd9: 1, 0xda: 0xf, 0xdb: 0xe, 0xdc: 7, 0xdd: 3, 0xde: 5, 0xdf: 1, 0xe0: 3,
          0xe1: 1, 0xe2: 9, 0xe3: 1, 0xe4: 1, 0xe5: 1, 0xe6: 1, 0xe7: 1, 0xe8: 3, 0xe9: 1, 0xea: 1, 0xeb: 1,
          0xec: 1, 0xed: 9, 0xee: 3, 0xef: 3, 0xf0: 2, 0xf1: 1, 0xf2: 1, 0xf3: 3, 0xf4: 2, 0xf5: 5, 0xf6: 1, 0xf7: 1}


def items_parse(d: bytes):
    """→ [dict(index, name, icon_at, sprite_len_at, sprite_at, sprite_len)] — 058a:10e9 / 058a:0eab."""
    out = []
    for idx, p in enumerate(struct.unpack_from("<256I", d, 0)):
        if not p:
            break
        it = dict(index=idx, name="", offset=p)
        while d[p]:
            t = d[p]
            if t == 1:
                kind = d[p + 1]; p += 6
                if kind == 0:
                    n = struct.unpack_from("<H", d, p)[0]
                    it.update(sprite_len_at=p, sprite_at=p + 2, sprite_len=n)
                    p += 2 + n
            elif t == 2:
                it["name"] = d[p + 2:p + 18].split(b"\0")[0].decode("cp437"); p += 18
            elif t in (3, 5, 6, 7, 8):
                p += 1
                while True:
                    op = d[p]; p += VM_LEN[op]
                    if op == 0xC0:
                        break
            elif t == 4:
                it["icon_at"] = p + 2; p += 2 + 1024
            elif t == 9:
                p += 2
            else:
                raise ValueError(f"ITEMS.ITM item {idx}: unknown tag {t}")
        out.append(it)
    return out


def items_rebuild(d: bytes, icons: dict[int, bytes], sprites: dict[int, bytes]) -> bytes:
    """Replace icons (in place) and room sprites (relocating) by item index."""
    items = items_parse(d)
    patches = []
    for it in items:
        if it["index"] in icons and "icon_at" in it:
            patches.append((it["icon_at"], 1024, icons[it["index"]]))
        if it["index"] in sprites and "sprite_at" in it:
            new = sprites[it["index"]]
            patches.append((it["sprite_len_at"], 2 + it["sprite_len"], struct.pack("<H", len(new)) + new))
    patches.sort()
    out = bytearray(); pos = 0; shifts = []
    for off, n, new in patches:
        out += d[pos:off]; out += new; pos = off + n; shifts.append((off, len(new) - n))
    out += d[pos:]
    offs = struct.unpack_from("<256I", d, 0)
    remap = lambda o: o + sum(dl for off, dl in shifts if off < o)
    struct.pack_into("<256I", out, 0, *[remap(o) if o else 0 for o in offs])
    return bytes(out)


# ── Room files: which room picture, masks, NPCs, animations ─────────────────

def rom_ops(d: bytes):
    """Yield (op, args) for a room file (07a2:0086)."""
    p = 0
    rd = lambda fmt: struct.unpack_from(fmt, d, p)
    while p < len(d):
        op = d[p]; p += 1
        if op == 1:
            yield op, dict(pict=d[p]); p += 1
        elif op == 2: p += 13
        elif op == 3: p += 9
        elif op == 4: p += 41
        elif op == 6: p += 3 + struct.unpack_from("<h", d, p + 1)[0]
        elif op == 7:
            yield op, dict(item=d[p]); p += 10
        elif op == 8:
            x, y, w, h, off = struct.unpack_from("<hhhhI", d, p + 1)
            yield op, dict(x=x, y=y, w=w, h=h, off=off); p += 13
        elif op in (9, 15): p += 2
        elif op == 11:
            yield op, dict(npc=d[p + 5:p + 13].split(b"\0")[0].decode("cp437")); p += 13
        elif op == 12: p += 3
        elif op == 13:
            a = struct.unpack_from("<h", d, p + 1)[0]
            yield op, dict(anim=(-a - 1) if a < 0 else a); p += 7
        elif op == 16:
            a = struct.unpack_from("<h", d, p + 1)[0]
            yield op, dict(anim=(-a - 1) if a < 0 else a); p += 3
        elif op == 18: p += 4
        elif op in (5, 10, 14, 19, 20): pass
        else:
            raise ValueError(f"bad room opcode {op:#x}")


# ── TITEL.ACF / PowerPacker ─────────────────────────────────────────────────

def pp_decrunch(blob: bytes) -> bytes:
    """PowerPacker 2.0 (INTRO.EXE 0735:0095)."""
    eff = blob[4:8]; out_len = int.from_bytes(blob[-4:-1], "big"); skip = blob[-1]
    src = blob[8:-4]; pos = len(src); out = bytearray(out_len); o = out_len
    buf = nbits = 0

    def bits(n):
        nonlocal buf, nbits, pos
        while nbits < n:
            pos -= 1
            buf |= src[pos] << nbits; nbits += 8
        v = 0
        for _ in range(n):
            v = (v << 1) | (buf & 1); buf >>= 1
        nbits -= n
        return v

    bits(skip)
    while o > 0:
        if bits(1) == 0:
            n = 1
            while True:
                x = bits(2); n += x
                if x != 3:
                    break
            for _ in range(n):
                o -= 1; out[o] = bits(8)
            if o <= 0:
                break
        x = bits(2); ob = eff[x]; n = x + 2
        if x == 3:
            if bits(1) == 0:
                ob = 7
            off = bits(ob)
            while True:
                y = bits(3); n += y
                if y != 7:
                    break
        else:
            off = bits(ob)
        for _ in range(n):
            o -= 1; out[o] = out[o + 1 + off]
    return bytes(out)


PP_EFF = (9, 10, 11, 12)


def pp_crunch(data: bytes) -> bytes:
    """PowerPacker 2.0 compatible packer (greedy LZ77 on the reversed data).
    Blob layout as in TITEL.ACF: u32le packed_len, eff[4], stream, u24be unpacked_len, u8 skip."""
    R = data[::-1]; N = len(R)
    maxd = [1 << b for b in PP_EFF]                    # distance = offset + 1 ≤ 2^bits
    heads: dict[bytes, list[int]] = {}
    bits: list[int] = []

    def put(v, n):
        for k in range(n - 1, -1, -1):
            bits.append((v >> k) & 1)

    def best_match(i):
        if i + 2 > N:
            return 0, 0
        cands = heads.get(R[i:i + 2], [])
        best_len, best_d = 0, 0
        for j in reversed(cands[-256:]):
            d = i - j
            if d > maxd[3]:
                break
            ln = 0
            while i + ln < N and R[j + ln] == R[i + ln] and ln < 4096:
                ln += 1
            limit = maxd[min(ln, 5) - 2] if ln >= 2 else 0
            if ln >= 2 and d > limit:                  # too far for this length class: shorten
                for L in (4, 3, 2):
                    if ln >= L and d <= maxd[L - 2]:
                        ln = L; break
                else:
                    continue
            if ln > best_len:
                best_len, best_d = ln, d
        return best_len, best_d

    def add(i):
        if i + 2 <= N:
            heads.setdefault(R[i:i + 2], []).append(i)

    tokens = []; lits = []; i = 0
    while i < N:
        ln, d = best_match(i)
        if ln >= 2:
            tokens.append(("L", lits)); lits = []
            tokens.append(("M", ln, d))
            for k in range(ln):
                add(i + k)
            i += ln
        else:
            lits.append(R[i]); add(i); i += 1
    tokens.append(("L", lits))

    # serialise: literal runs are only allowed right before a match (or at the very end)
    k = 0
    while k < len(tokens):
        t = tokens[k]
        if t[0] == "L":
            if t[1]:
                put(0, 1)
                n = len(t[1]) - 1
                while n >= 3:
                    put(3, 2); n -= 3
                put(n, 2)
                for b in t[1]:
                    put(b, 8)
                k += 1
                continue                                # a match follows (or the end)
            k += 1
            if k < len(tokens) and tokens[k][0] == "M":
                put(1, 1)                                # match without literals
            continue
        _, ln, d = t
        off = d - 1
        if ln < 5:
            put(ln - 2, 2); put(off, PP_EFF[ln - 2])
        else:
            put(3, 2)
            if off < 128:
                put(0, 1); put(off, 7)
            else:
                put(1, 1); put(off, PP_EFF[3])
            n = ln - 5
            while n >= 7:
                put(7, 3); n -= 7
            put(n, 3)
        k += 1

    skip = (-len(bits)) % 32
    allbits = [0] * skip + bits
    nbytes = len(allbits) // 8
    stream = bytearray(nbytes)
    for kbit, b in enumerate(allbits):
        if b:
            stream[nbytes - 1 - kbit // 8] |= 1 << (kbit % 8)
    body = bytes(PP_EFF) + bytes(stream) + len(data).to_bytes(3, "big") + bytes([skip])
    return struct.pack("<I", 4 + len(stream)) + body           # u32 = packed size - 8


def acf_parse(d: bytes):
    """→ [dict(x, y, w, h, delay, blob_at, blob_len)] — INTRO.EXE 065a:0256."""
    n = struct.unpack_from("<H", d, 0)[0]; p = 2 + 14 * n; out = []
    for i in range(n):
        x, y, w, h, size, delay = struct.unpack_from("<4HIH", d, 2 + 14 * i)
        out.append(dict(x=x, y=y, w=w, h=h, delay=delay, blob_at=p, blob_len=size)); p += size
    return out


ACF_MAX_PACKED = 0xFA00      # INTRO.EXE reads all frame data with one fread(…, 0xFA00)


def acf_rebuild(d: bytes, new_pixels: dict[int, bytes]) -> bytes:
    frames = acf_parse(d)
    head = bytearray(d[:2 + 14 * len(frames)]); body = bytearray()
    for i, f in enumerate(frames):
        blob = pp_crunch(new_pixels[i]) if i in new_pixels else d[f["blob_at"]:f["blob_at"] + f["blob_len"]]
        struct.pack_into("<I", head, 2 + 14 * i + 8, len(blob))
        body += blob
    if len(body) > ACF_MAX_PACKED:
        raise ValueError(f"intro frames pack to {len(body)} bytes; INTRO.EXE loads at most {ACF_MAX_PACKED}")
    return bytes(head + body)
