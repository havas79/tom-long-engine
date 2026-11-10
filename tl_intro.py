"""
tl_intro.py - INTRO.EXE re-implemented (spec/04_objects_state.md §9), without any GUI code.

    intro(read)       the title sequence before the game  (TOMLONG.BAT: "intro music")
    intro(read, out)  the closing screen after quitting   (TOMLONG.BAT: "intro out")

Both are generators of drawing steps for the front end:

    ("hi", pixels, pal)   640x480 16-colour picture (mode 12h): pixels = 307200 indices, pal = 48 bytes 0-63
    ("hipal", pal)        new palette for the current hi-res picture (fades)
    ("lo", pixels, pal)   320x200 256-colour screen (mode 13h): pixels = 64000 indices, pal = 768 bytes 0-63
    ("wait", seconds)     show it for that long; any key or mouse button skips the REST of the sequence
    ("music", name)       start a song (the intro plays MACCAIN.OGM throughout)

Timing as in the original: the pictures are held 54 BIOS clock ticks (18.2 Hz), fades take 64 steps with the
palette set on every 2nd one at the vertical retrace (70 Hz), the animation frames are held delay_ms*18//1000
ticks, colour 0 makes the sky flash on fixed frames, and the last frame stays 144 ticks.
"""
from __future__ import annotations

import struct

TICK = 1 / 18.2065                       # BIOS clock tick
VSYNC = 1 / 70.086                       # mode 12h / 13h vertical retrace
FLASH = {33: (0, 0, 3), 41: (0, 0, 6), 52: (0, 0, 20), 63: (0, 0, 28), 74: (0, 0, 45),
         83: (0, 36, 63), 89: (0, 0, 45), 94: (0, 0, 28), 99: (0, 0, 20), 103: (0, 0, 0)}


# ── file formats ──────────────────────────────────────────────────────────────────────────────
def pcx_mode12(d: bytes):
    """640x480 4-plane PCX (06E6:0061) -> (307200 indices, 48-byte 6-bit palette)."""
    pal = bytes(b >> 2 for b in d[16:64])
    bpl = struct.unpack_from("<H", d, 66)[0]
    planes = d[65]
    line = bpl * planes
    raw = bytearray(); p = 128
    need = line * 480
    while len(raw) < need and p < len(d):                   # RLE runs may cross scan lines (as in the original)
        b = d[p]; p += 1
        if b > 0xC0:
            raw += bytes([d[p]]) * (b - 0xC0); p += 1
        else:
            raw.append(b)
    raw += bytes(need - len(raw))
    expand = [int.from_bytes(bytes((v >> (7 - i)) & 1 for i in range(8)), "big") for v in range(256)]
    out = bytearray()
    for y in range(480):
        r = y * line
        p0, p1, p2, p3 = (raw[r + k * bpl:r + k * bpl + 80] for k in range(4))
        out += b"".join((expand[a] | expand[b] << 1 | expand[c] << 2 | expand[e] << 3).to_bytes(8, "big")
                        for a, b, c, e in zip(p0, p1, p2, p3))
    return bytes(out), pal


def pp_decrunch(blob: bytes) -> bytes:
    """PowerPacker 2.0 (INTRO.EXE 0735:0095); blob = u32 len, eff[4], stream, u24be out_len, u8 skip."""
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


def acf_frames(d: bytes):
    """TITEL.ACF -> [(x, y, w, h, delay_ms, pixels)]."""
    n = struct.unpack_from("<H", d, 0)[0]; p = 2 + 14 * n; out = []
    for i in range(n):
        x, y, w, h, size, delay = struct.unpack_from("<4HIH", d, 2 + 14 * i)
        out.append((x, y, w, h, delay, d[p:p + size])); p += size
    return out


# ── fades (06E6:03FB FadeIn16 / 06E6:02B6 FadeOut16) ──────────────────────────────────────────
def fade_steps(pal: bytes, fade_in: bool):
    """The palettes shown during a fade: 64 Bresenham steps, displayed on every 2nd step (32 retraces)."""
    acc = [0] * len(pal)
    cur = [0] * len(pal) if fade_in else list(pal)
    for k in range(64):
        for i, c in enumerate(pal):
            acc[i] += c
            if acc[i] > 63:
                acc[i] -= 63
                cur[i] += 1 if fade_in else -1
        if k % 2 == 0:
            yield bytes(max(0, min(63, v)) for v in cur)


# ── the sequences ─────────────────────────────────────────────────────────────────────────────
def _picture(read, name):
    px, pal = pcx_mode12(read(name))
    yield ("hi", px, bytes(48))
    for p in fade_steps(pal, True):
        yield ("hipal", p); yield ("wait", VSYNC)
    yield ("wait", 54 * TICK)
    for p in fade_steps(pal, False):
        yield ("hipal", p); yield ("wait", VSYNC)


def intro(read, out: bool = False):
    if out:
        yield from _picture(read, "TITEL2.PCX")
        return
    yield ("music", "maccain")
    yield from _picture(read, "TITEL1.PCX")
    yield from _picture(read, "TITEL2.PCX")
    yield ("lo", bytes(64000), bytes(768))
    yield ("wait", 1.0)                                       # 0000:0B34(1) = sleep(1)
    pal = bytearray(b & 63 for b in read("TITEL.ACP")[:768])
    frames = acf_frames(read("TITEL.ACF"))
    scr = bytearray(64000)
    first = pp_decrunch(frames[0][5])
    scr[:len(first)] = first[:64000]
    yield ("lo", bytes(scr), bytes(pal))
    for k in range(1, len(frames)):
        x, y, w, h, delay, blob = frames[k]
        px = pp_decrunch(blob)
        for r in range(h):
            if 0 <= y + r < 200:
                scr[(y + r) * 320 + x:(y + r) * 320 + x + w] = px[r * w:(r + 1) * w]
        if k in FLASH:
            pal[0:3] = bytes(FLASH[k])
        yield ("lo", bytes(scr), bytes(pal))
        yield ("wait", max(VSYNC, (delay * 18 // 1000) * TICK))
    yield ("wait", 144 * TICK)
