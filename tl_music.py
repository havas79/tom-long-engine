"""
tl_music.py - the game's AdLib music and sound effects.

    ORPHEUS player (tl_orpheus.py, a re-implementation of the original driver)
      -> OPL2 register writes -> DOSBox's OPL emulator DBOPL (hwopl64.dll / hwopl32.dll / libhwopl.so)
      -> 16-bit mono samples -> sound card (Windows: winmm waveOut; Linux: aplay / pacat / paplay;
         anywhere: the 'sounddevice' module if it is installed)

Everything runs in one background thread; the engine only queues commands (play / toggle / effect), which the
thread applies before rendering the next block (about 1/30 s), so the game loop is never held up by audio.

Driver rules copied from the original (spec/03_music.md §2.2, §5.2):
  * play(name): same song name as the current one -> nothing happens (re-entering a room doesn't restart it);
    "last" reloads the current song; "none" = silence (the tick rate stays); the song only starts if music is on,
    otherwise it is loaded but not started (that is what sets the 192 Hz tick rate the effects then run at).
  * effects (script op F5) are a Note-On on FX channel 16 with program 0 of MCCAINFX.OGD (the effect key map);
    velocity 0 stops a looping effect. Before the first song is loaded the tick rate is 0, so effects are silent.
"""
from __future__ import annotations

import collections
import ctypes
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tl_orpheus import Orpheus  # noqa: E402

RATE = 44100
RTC_HZ = 1024
BLOCK = RATE // 30                       # samples per block handed to the sound card
AHEAD = 0.12                             # seconds of audio kept queued ahead of the speaker
DRIVER_DS_SEG = 0x1347                   # ORPHEUS data segment in ENGINE.EXE (load-relative)
FREQTAB_OFF = 0x1544                     # u32[129] note frequency table in that segment
RANDTAB_OFF = 0x17D6                     # 256-byte noise table
FREQTAB_SIG = bytes.fromhex("135a1000e05211008d5a120014721300")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# DBOPL through ctypes
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _lib_names():
    if sys.platform == "win32":
        return ["hwopl64.dll" if struct.calcsize("P") == 8 else "hwopl32.dll"]
    if sys.platform == "darwin":
        return ["libhwopl.dylib", "libhwopl.so"]
    return ["libhwopl.so"]


def load_hwopl():
    errors = []
    for name in _lib_names():
        for folder in (HERE, HERE / "lib", Path.cwd()):
            p = folder / name
            if not p.exists():
                continue
            try:
                lib = ctypes.CDLL(str(p))
            except OSError as ex:
                errors.append(f"{p}: {ex}")
                continue
            lib.hwopl_create.restype = ctypes.c_void_p
            lib.hwopl_create.argtypes = [ctypes.c_uint]
            lib.hwopl_write.restype = None
            lib.hwopl_write.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            lib.hwopl_generate.restype = None
            lib.hwopl_generate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            lib.hwopl_destroy.restype = None
            lib.hwopl_destroy.argtypes = [ctypes.c_void_p]
            return lib
    raise OSError("OPL emulator library not found (" + ", ".join(_lib_names()) + ")"
                  + ("" if not errors else ": " + "; ".join(errors)))


class OPL:
    def __init__(self, rate=RATE):
        self.lib = load_hwopl()
        self.h = self.lib.hwopl_create(rate)
        if not self.h:
            raise OSError("hwopl_create failed")
        self.buf = (ctypes.c_int16 * 4096)()
        self.write = lambda r, v, w=self.lib.hwopl_write, h=self.h: w(h, r, v)

    def generate(self, n: int) -> bytes:
        out = []
        while n > 0:
            k = min(n, 4096)
            if k < 2:                      # the wrapper drops a sample for 1-sample requests; ask for 2
                k = 2
            self.lib.hwopl_generate(self.h, self.buf, k)
            out.append(ctypes.string_at(self.buf, 2 * k))
            n -= k
        return b"".join(out)

    def close(self):
        if self.h:
            self.lib.hwopl_destroy(self.h)
            self.h = None


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Sound card output
# ════════════════════════════════════════════════════════════════════════════════════════════════
class WinMMOut:
    """waveOut with a ring of buffers, polled (no callbacks), only ctypes."""
    NBUF = 6

    def __init__(self, rate):
        from ctypes import wintypes as wt

        class WAVEFORMATEX(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                        ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
                        ("cbSize", wt.WORD)]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wt.DWORD), ("dwBytesRecorded", wt.DWORD),
                        ("dwUser", ctypes.c_size_t), ("dwFlags", wt.DWORD), ("dwLoops", wt.DWORD),
                        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

        self.WAVEHDR = WAVEHDR
        w = self.w = ctypes.WinDLL("winmm")
        w.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.POINTER(WAVEFORMATEX),
                                  ctypes.c_size_t, ctypes.c_size_t, wt.DWORD]
        for f in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            getattr(w, f).argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        w.waveOutReset.argtypes = [ctypes.c_void_p]
        w.waveOutClose.argtypes = [ctypes.c_void_p]
        fmt = WAVEFORMATEX(1, 1, rate, rate * 2, 2, 16, 0)
        self.hwo = ctypes.c_void_p()
        r = w.waveOutOpen(ctypes.byref(self.hwo), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0)
        if r:
            raise OSError(f"waveOutOpen failed ({r})")
        self.hdrs = [WAVEHDR() for _ in range(self.NBUF)]
        self.bufs = [None] * self.NBUF
        self.busy = [False] * self.NBUF

    def free_slot(self):
        for i, h in enumerate(self.hdrs):
            if not self.busy[i]:
                return i
            if h.dwFlags & 1:                                     # WHDR_DONE
                self.w.waveOutUnprepareHeader(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
                self.busy[i] = False
                return i
        return -1

    def write(self, data: bytes, stop: threading.Event):
        while True:
            i = self.free_slot()
            if i >= 0:
                break
            if stop.is_set():
                return
            time.sleep(0.004)
        buf = ctypes.create_string_buffer(data, len(data))
        self.bufs[i] = buf
        h = self.hdrs[i]
        h.lpData = ctypes.cast(buf, ctypes.c_void_p); h.dwBufferLength = len(data); h.dwFlags = 0; h.dwLoops = 0
        self.w.waveOutPrepareHeader(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
        self.w.waveOutWrite(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
        self.busy[i] = True

    paced = True                                                   # write() blocks by itself

    def close(self):
        try:
            self.w.waveOutReset(self.hwo)
            for i, h in enumerate(self.hdrs):
                if self.busy[i]:
                    self.w.waveOutUnprepareHeader(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
            self.w.waveOutClose(self.hwo)
        except Exception:
            pass


class PipeOut:
    """Raw samples into aplay / pacat / paplay (Linux); paced by the clock so latency stays low."""
    paced = False

    def __init__(self, rate):
        cmds = [["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(rate), "--buffer-time=80000", "-"],
                ["pacat", "--raw", "--format=s16le", "--channels=1", f"--rate={rate}", "--latency-msec=60"],
                ["paplay", "--raw", "--format=s16le", "--channels=1", f"--rate={rate}"]]
        for c in cmds:
            if shutil.which(c[0]):
                self.p = subprocess.Popen(c, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                return
        raise OSError("no aplay/pacat/paplay found")

    def write(self, data, stop):
        try:
            self.p.stdin.write(data); self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            stop.set()

    def close(self):
        try:
            self.p.stdin.close(); self.p.terminate()
        except Exception:
            pass


class SoundDeviceOut:
    paced = True

    def __init__(self, rate):
        import sounddevice as sd                                    # optional
        self.s = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", latency="low")
        self.s.start()

    def write(self, data, stop):
        self.s.write(data)

    def close(self):
        try:
            self.s.stop(); self.s.close()
        except Exception:
            pass


def open_output(rate):
    kinds = [WinMMOut] if sys.platform == "win32" else [PipeOut]
    kinds.append(SoundDeviceOut)
    errors = []
    for k in kinds:
        try:
            return k(rate)
        except Exception as ex:
            errors.append(f"{k.__name__}: {ex}")
    raise OSError("no sound output: " + "; ".join(errors))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Driver data from ENGINE.EXE
# ════════════════════════════════════════════════════════════════════════════════════════════════
def driver_ds(exe: bytes) -> bytes:
    """The ORPHEUS data segment image (initial voice/channel records, frequency and noise tables)."""
    hdr = struct.unpack_from("<H", exe, 8)[0] * 16
    off = hdr + DRIVER_DS_SEG * 16
    if exe[off + FREQTAB_OFF:off + FREQTAB_OFF + 16] != FREQTAB_SIG:
        i = exe.find(FREQTAB_SIG)                                   # a rebuilt EXE: find the table instead
        if i < 0:
            raise ValueError("ENGINE.EXE: music driver data not found")
        off = i - FREQTAB_OFF
    return (exe[off:off + 0x1A00] + bytes(0x1A00))[:0x1A00]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# The music object the engine talks to
# ════════════════════════════════════════════════════════════════════════════════════════════════
class Music:
    def __init__(self, read, music_on=True, fx_on=True, rate=RATE, output=True):
        """read(name) -> bytes reads a game file (ENGINE.EXE, MCCAINGM.OGD, *.OGM)."""
        self.read, self.rate = read, rate
        self.music_on, self.fx_on = music_on, fx_on
        self.current = ""
        self.cmds = collections.deque()
        self.lock = threading.Lock()
        ds = driver_ds(read("ENGINE.EXE"))
        gm = read("MCCAINGM.OGD") + bytes(256)
        fx = read("MCCAINFX.OGD") + bytes(256)
        self.opl = OPL(rate)
        self.orph = Orpheus(ds, gm, fx, ds[RANDTAB_OFF:RANDTAB_OFF + 257], self.opl.write)
        self.pending = 0                                            # samples owed since the last OPL change
        self.frac = 0
        self.stop = threading.Event()
        self.out = open_output(rate) if output else None
        self.thread = None
        if self.out:
            self.thread = threading.Thread(target=self.run, name="music", daemon=True)
            self.thread.start()

    # ── engine interface (called from the game thread) ─────────────────────────────────────────
    def play(self, name: str):
        self.cmds.append(("play", name))

    def toggle(self):
        self.music_on = not self.music_on
        self.cmds.append(("play", "last"))

    def set_music(self, on: bool):
        if on != self.music_on:
            self.toggle()

    def effect(self, note: int, vel: int):
        if self.fx_on:
            self.cmds.append(("fx", note, vel))

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(1.0)
        if self.out:
            self.out.close()
        self.opl.close()

    # ── audio thread ──────────────────────────────────────────────────────────────────────────
    def _music_play(self, name: str):                              # 0B55:0197
        n = name.lower()
        if not n.endswith(".ogm") and n != "last":
            n += ".ogm"
        if n == self.current:
            return
        last = n == "last"
        self.orph.reset()                                           # musicOff
        if not last:
            self.current = n
        if self.current in ("", "none", "none.ogm"):
            return
        try:
            data = self.read(self.current)
        except OSError:
            return
        if data[:4] != b"OMID":
            return
        self.orph.load_song(data)
        if self.music_on:
            self.orph.start(1, 1)

    def _apply(self):
        while self.cmds:
            c = self.cmds.popleft()
            if c[0] == "play":
                self._music_play(c[1])
            elif c[0] == "fx":
                self.orph.event(0x90, c[1], c[2], cl=0, fxpath=True)

    def render(self, nrtc: int) -> bytes:
        """nrtc periods of the 1024 Hz RTC interrupt as samples; OPL writes land between the right samples."""
        o, out = self.orph, []
        for _ in range(nrtc):
            self.frac += self.rate
            self.pending += self.frac // RTC_HZ
            self.frac %= RTC_HZ
            old = o.acc
            o.acc = (o.acc + o.inc) & 0xFFFF
            if old < 0x8000 <= o.acc:                               # signed overflow = a driver tick
                if self.pending:
                    out.append(self.opl.generate(self.pending)); self.pending = 0
                o.tick()
        if self.pending:
            out.append(self.opl.generate(self.pending)); self.pending = 0
        return b"".join(out)

    def run(self):
        per_block = max(1, BLOCK * RTC_HZ // self.rate)             # RTC periods per block
        t0, done = time.perf_counter(), 0
        try:
            while not self.stop.is_set():
                self._apply()
                data = self.render(per_block)
                self.out.write(data, self.stop)
                done += len(data) // 2
                if not self.out.paced:
                    ahead = t0 + done / self.rate - time.perf_counter()
                    if ahead > AHEAD:
                        time.sleep(ahead - AHEAD)
                    elif ahead < -0.5:                              # we fell behind (stall): don't race
                        t0, done = time.perf_counter(), 0
        except Exception as ex:                                     # never take the game down with the sound
            print("music stopped:", ex, file=sys.stderr)


def render_wav(game_folder: str, song: str, seconds: float, out: str, effects=()):
    """Test helper: render a song (and optional (time, note) effects) to a WAV file without a sound card."""
    import wave
    folder = Path(game_folder)
    files = {p.name.upper(): p for p in folder.iterdir()}
    m = Music(lambda n: files[n.upper()].read_bytes(), output=False)
    m.play(song)
    fx = sorted(effects)
    chunks, t = [], 0.0
    step = 32
    while t < seconds:
        while fx and fx[0][0] <= t:
            _, note = fx.pop(0); m.effect(note, 127)
        m._apply()
        chunks.append(m.render(step))
        t += step / RTC_HZ
    m.opl.close()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(b"".join(chunks))


if __name__ == "__main__":
    # python tl_music.py GAME_FOLDER song.ogm seconds out.wav
    render_wav(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4])
