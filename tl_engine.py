"""
tl_engine.py - the Tom Long engine core (no window, no sound).

A re-implementation of ENGINE.EXE from the decoded specification (ENGINE_SPEC.md and spec/*.md).
It reads the original data files and produces 320x200 frames of palette indices, one per tick.

Design
------
The original draws everything inside the timer interrupt (9.103 ticks per second) while the main loop,
the script interpreter and the dialogue runner busy-wait. Here the same split is kept:

* ``Engine.tick()`` is called 9.103 times per second by the front end. It first advances the game
  logic coroutine (a Python generator that ``yield``s wherever the original busy-waits), then runs the
  six timer callbacks in the original order: walker + scroll, background objects, depth-sorted pass
  (NPCs, animations, masks) + front objects + frame/step advance, label/bubble, present (cursor, flip).
* Every blocking script opcode (WAIT, walk-and-wait, wait-for-animation, dialogues, fades) is a
  generator that yields once per tick until its condition holds.

The front end only has to call ``set_mouse()``, ``key()`` and ``tick()``, and show ``frame`` with
``palette_rgb()``.
"""
from __future__ import annotations

import struct
from pathlib import Path

from tl_formats import plb_entries, sprite_decode, npc_frames, ani_record, anm_parse, VM_LEN
from tl_walk import PathFinder

TICK_HZ = 18.2065 / 2
VIEW_W, VIEW_H = 320, 160
EXE_DGROUP = 0x12C70                       # file offset of DGROUP in ENGINE.EXE
FONT = (Path(__file__).with_name("vga8x8.bin")).read_bytes()


class GameFiles:
    """Case-insensitive access to the game folder."""
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.names = {f.name.lower(): f for f in self.folder.iterdir()}

    def path(self, name: str) -> Path:
        return self.names.get(name.lower(), self.folder / name.upper())

    def read(self, name: str) -> bytes:
        p = self.names.get(name.lower())
        if p is None:
            raise FileNotFoundError(f"{name} not found in {self.folder}")
        return p.read_bytes()

    def exists(self, name: str) -> bool:
        return name.lower() in self.names


def cstr(b: bytes) -> bytes:
    return b.split(b"\0")[0]


class Frames:
    """Decoded RLE sprite frames, cached: (w, h, xoff, yoff, pixels)."""
    _cache: dict = {}

    @classmethod
    def get(cls, data: bytes, off: int):
        key = (id(data), off)
        f = cls._cache.get(key)
        if f is None:
            w, h, ox, oy, px = sprite_decode(data, off)
            f = cls._cache[key] = (w, h, ox, oy, bytes(px))
        return f


# ════════════════════════════════════════════════════════════════════════════════════════════
# Characters and animations
# ════════════════════════════════════════════════════════════════════════════════════════════

class NPC:
    """Module 071D. Position X,Y = feet; drawn at (X - w/2, Y - h + 10) + frame offsets."""
    def __init__(self, data: bytes):
        self.data = data
        self.frames = npc_frames(data)
        self.rec = [struct.unpack_from("<3H", data, i * 166) for i in range(5)]
        self.anim, self.frame, self.frozen, self.hidden = 4, 1, True, True
        self.x = self.y = 0

    def setpos(self, X: int, Y: int):                       # 071D:03FC
        cnt, w, h = self.rec[self.anim]
        self.x, self.y, self.hidden = X - w // 2, Y - h + 10, False

    def setdir(self, d: int):                               # 071D:0551
        if d == 4:
            if self.anim != 4:
                self.frame = self.anim; self.anim = 4
            self.frozen = True
            return
        self.anim = {0: 1, 1: 0, 2: 2, 3: 3}[d]
        if self.frame >= max(1, self.rec[self.anim][0]):
            self.frame = 0
        self.frozen = False

    def face(self, d: int):                                 # 071D:0693
        self.anim, self.frame, self.frozen = 4, {0: 1, 1: 0, 2: 2, 3: 3}[d], True

    def cur(self):
        fr = self.frames[self.anim]
        return Frames.get(self.data, fr[self.frame % len(fr)]) if fr else None

    def advance(self):                                      # 071D:07E2
        if not self.hidden and not self.frozen and self.frames[self.anim]:
            self.frame = (self.frame + 1) % len(self.frames[self.anim])


class Anim:
    """One CANIM slot (module 084B)."""
    def __init__(self, ani: bytes, num: int, x: int, y: int, layer: int = 0):
        nf, steps, flags, frames = ani_record(ani, num)
        self.ani, self.id, self.x, self.y = ani, num, x, y
        self.steps, self.frames = steps, frames
        self.step, self.layer = 0, layer
        self.hidden = bool(steps) and steps[0][0] == -5
        self.wait_start, self.stay_at_end = bool(flags & 1), bool(flags & 2)
        self.paused, self.loop_mark, self.loop_set, self.ended = 0, 0, False, False

    def advance(self, signal: bool):                        # 084B:0490
        if self.hidden:
            return
        if self.wait_start:
            if not signal:
                return
            self.wait_start = False; self.loop_set = False
        if self.paused == 1:
            return
        s = self.step + 1
        if s < len(self.steps):
            f = self.steps[s][0]
            if f == -1:
                s += 1; self.loop_mark = s; self.loop_set = True
            elif f == -2:
                s = self.loop_mark if self.loop_set else s + 1
            elif f == -3:
                if self.paused == 0xFF:
                    s += 1; self.paused = 0
                else:
                    self.paused = 1; s -= 1
            elif f == -4:
                s += 1; self.layer = 1
            elif f == -5:
                self.hidden = True
            elif f == -6:
                s += 1; self.layer = 0
        if s >= len(self.steps):
            if self.stay_at_end:
                s -= 1; self.paused = 1
            else:
                self.hidden = True
            self.ended = True
        self.step = max(0, s)

    def advance_special(self) -> bool:
        """Tom's special animation (E0/D5): -4 front, -3 stop point, -1/-2 skipped. Returns False at the end."""
        if self.paused == 1:
            return True
        s = self.step + 1
        while s < len(self.steps) and self.steps[s][0] in (-1, -2):
            s += 1
        if s < len(self.steps):
            f = self.steps[s][0]
            if f == -3:
                if self.paused == 0xFF:
                    s += 1; self.paused = 0
                else:
                    self.paused = 1; s -= 1
            elif f == -4:
                s += 1; self.layer = 1
        if s >= len(self.steps):
            self.ended = True
            return False
        self.step = s
        return True

    def frame_at(self):
        if self.step >= len(self.steps):
            return None
        fr, dx, dy = self.steps[self.step]
        if fr < 0:
            return None
        return Frames.get(self.ani, self.frames[fr]), dx, dy


# ════════════════════════════════════════════════════════════════════════════════════════════
# The engine
# ════════════════════════════════════════════════════════════════════════════════════════════

class Engine:
    VERB_WALK, VERB_LOOK, VERB_TAKE, VERB_USE, VERB_TALK, VERB_LOAD, VERB_SAVE, VERB_HELD = 0, 1, 2, 3, 4, 5, 6, -3
    BUTTONS = [(0, 172, 16, 188, "CURSORB"), (19, 162, 35, 178, "LOOKB"), (38, 162, 54, 178, "TAKEB"),
               (19, 182, 35, 198, "USEB"), (38, 182, 54, 198, "TALKB"), (272, 162, 288, 178, "LOADB"),
               (291, 162, 307, 178, "SAVEB")]

    def __init__(self, game_folder, save_folder=None, save_unlocked=True, fix_esc_bug=True, music=None):
        self.files = GameFiles(game_folder)
        self.save_folder = Path(save_folder) if save_folder else self.files.folder
        self.fix_esc_bug = fix_esc_bug
        self.music = music                          # optional object with play(name) / toggle() / effect(note, vel)
        f = self.files
        exe = f.read("ENGINE.EXE")
        self.exe_text = exe[EXE_DGROUP:]
        self.grafik = f.read("GRAFIK.PLB"); self.grafik_entries = plb_entries(self.grafik)
        self.system = f.read("SYSTEM.PLB"); sys_entries = plb_entries(self.system)
        self.cursors = [self.system[e[4]:e[4] + 256] for e in sys_entries[:5]]
        spal = bytearray(self.system[sys_entries[0][3]:sys_entries[0][3] + 48])
        spal[0:6] = b"\0\0\0\x3f\x3f\x3f"
        self.sys_pal = bytes(spal)
        self.ani = f.read("ANIMS.ANI")
        self.masks_file = f.read("MASKS.MSK")
        self.items_data = f.read("ITEMS.ITM")
        self.item_offsets = [o for o in struct.unpack_from("<256I", self.items_data, 0)]
        self.n_records = next((i for i, o in enumerate(self.item_offsets) if o == 0), 256)
        self.procs = f.read("PROCS.CMD")
        self.proc_offsets = list(struct.unpack_from("<256I", self.procs, 0))
        self.player_data = f.read("PLAYER.NPC")
        # UI graphics
        self.ui = {}
        for x1, y1, x2, y2, name in self.BUTTONS:
            d = f.read(name + ".ANM"); n, w, h, offs = anm_parse(d)
            self.ui[name] = (w, h, d[offs[1]:offs[1] + w * h])
        d = f.read("ARROWS.ANM"); n, w, h, offs = anm_parse(d)
        self.ui["ARROWS"] = (w, h, d[offs[0]:offs[0] + w * h])
        self.verb_names = [self.exe_str(o) for o in (0x864, 0x869, 0x86F, 0x874, 0x87C, 0x883, 0x889)]
        self.replies = {k: self.exe_str(o) for k, o in
                        (("look", 0x4B2), ("take", 0x4C9), ("use", 0x4E0), ("with", 0x4F0), ("talk", 0x506))}
        # persistent state
        self.flags = bytearray(f.read("FLAGS.FLG")[:512].ljust(512, b"\0"))
        self.item_gone = bytearray(256)
        self.morph = list(range(256))
        self.items = [dict(w=0, h=0, sprite=None, look=None, take=None, use=None, after=None, talk=None,
                           name=b"", icon=None, face=0) for _ in range(256)]
        for i in range(self.n_records):
            self.parse_item(i, i)
        self.inv: list[int] = []
        self.inv_scroll = 0
        self.verb, self.held = 0, 0
        self.save_unlocked = save_unlocked
        # runtime
        self.frame = bytearray(320 * 200)
        self.base_pal = bytearray(768)
        self.dac_override: dict[int, tuple] = {}
        self.fade = 63
        self.screen_on = False
        self.mouse = (10, 10); self.buttons = 0; self.keys: list[str] = []; self.requests: list = []
        self.latch = False                          # [0x4400] buttons-released latch
        self.locked = 0
        self.ticks = 0
        self.pointer_hidden = False
        self.label = None                           # (text bytes, x, y)
        self.label_enabled = True                   # [0xDBE]
        self.bubble = None                          # [text, x, y, ticks]
        self.exits_off = False                      # [0xDC0]
        self.inv_hidden = False                     # [0x808]
        self.bar_shown = True                       # what the bar area on screen shows (see redraw_bar)
        self.scroll_lock = False                    # [0x48C]
        self.start_signal = False                   # [0x90]
        self.pulse = None                           # F4: [colour, level, step]
        self.dialog = None                          # frozen scene while a dialogue/menu runs
        self.finished = False
        self.player = NPC(self.player_data)
        self.player_swapped = False
        self.tom_hidden = False                     # [0x48A] (DF/D0)
        self.special = None                         # Tom's special animation slot [0xA1E]
        self.special_active = False                 # [0xDC4]
        self.room = None
        self.logic = self.game()

    def start_at(self, room: str, x: int, y: int, entry: int = -1):
        """Start (or restart) the game in a given room - used by tests and a future 'jump to room' option."""
        self.logic = self.game(room, x, y, entry)

    # ── helpers ─────────────────────────────────────────────────────────────────────────────
    def exe_str(self, off: int) -> bytes:
        return cstr(self.exe_text[off:off + 256])

    def exe_node(self, off: int):
        d = self.exe_text
        return d, off

    def parse_item(self, slot: int, rec: int):
        """058A:0EAB: tags present overwrite the slot's fields, missing tags keep the old value."""
        d = self.items_data
        it = self.items[slot]
        it["face"] = 0
        if rec >= self.n_records:
            rec = 0
        p = self.item_offsets[rec]
        while d[p]:
            t = d[p]
            if t == 1:
                kind = d[p + 1]; it["w"], it["h"] = struct.unpack_from("<hh", d, p + 2); p += 6
                if kind == 0:
                    n = struct.unpack_from("<H", d, p)[0]; it["sprite"] = p + 2; p += 2 + n
                else:
                    it["sprite"] = None
            elif t == 2:
                it["name"] = cstr(d[p + 2:p + 18]); p += 18
            elif t in (3, 5, 6, 7, 8):
                key = {3: "look", 5: "take", 6: "use", 7: "after", 8: "talk"}[t]
                it[key] = p + 1; p += 1
                while True:
                    op = d[p]; p += VM_LEN[op]
                    if op == 0xC0:
                        break
            elif t == 4:
                it["icon"] = p + 2; p += 2 + 1024
            elif t == 9:
                it["face"] = d[p + 1]; p += 2
            else:
                break

    def say(self, text: bytes, x: int, y: int, ticks: int = 35):
        """0A1F:0B9B: bubble centred on the anchor (screen coordinates)."""
        text = cstr(text[:24])
        n = len(text)
        y2 = max(y - 8, 0)
        x2 = max(x - 4 * n, 0)
        if x2 + 8 * n > 319:
            x2 = 316 - 8 * n
        y2 = min(y2, 150)
        self.bubble = [text, x2, y2, ticks]

    # ── input from the front end ────────────────────────────────────────────────────────────
    def set_mouse(self, x: int, y: int, buttons: int):
        self.mouse = (max(0, min(319, x)), max(0, min(199, y)))
        self.buttons = buttons

    def key(self, ch: str):
        self.keys.append(ch)

    def wait(self, n: int):
        for _ in range(n):
            yield

    # ════════════════════════════════════════════════════════════════════════════════════════
    # Room loading (07A2:0086) and room state
    # ════════════════════════════════════════════════════════════════════════════════════════
    def room_load(self, name: str):
        d = self.files.read(name + ".ROM")
        R = dict(name=name, pic=None, W=320, H=160, bg=b"", walk=None, hotspots=[], slots=[], masks=[],
                 npc=None, anims=[], startproc=-1, keep_tom=False, dark=False, dark14=False, song=None)
        self.room = R
        self.pf = PathFinder(1, 1, b"\0")
        p, skip = 0, []
        on = lambda: not any(skip)
        while p < len(d):
            op = d[p]; p += 1
            if op == 0x05:
                break
            if op in (0x09, 0x0F):
                f = struct.unpack_from("<h", d, p)[0]; p += 2
                skip.append(not (bool(self.flags[f]) if op == 0x09 else not self.flags[f]))
                continue
            if op == 0x0A:
                if skip:
                    skip.pop()
                continue
            if op == 0x01:
                v = d[p]; p += 1
                if on():
                    self.set_picture(v)
            elif op == 0x02:
                p += 13
            elif op == 0x03:
                song = cstr(d[p + 1:p + 9]).decode("cp437"); p += 9
                if on():
                    R["song"] = song
                    if self.music:
                        self.music.play(song)
            elif op == 0x04:
                e = d[p]; x1, y1, x2, y2 = struct.unpack_from("<4h", d, p + 1)
                target = cstr(d[p + 9:p + 17]).decode("cp437")
                label = cstr(d[p + 17:p + 33])
                sx, sy, wx, wy = struct.unpack_from("<4h", d, p + 33); p += 41
                if on():
                    R["hotspots"].append(dict(kind=0, x=x1, y=y1, w=x2 - x1, h=y2 - y1, name=label, room=target,
                                              entry=e if e < 128 else e - 256, spawn=(sx, sy), walk=(wx, wy)))
            elif op == 0x06:
                ln = struct.unpack_from("<h", d, p + 1)[0]
                bpr, rows = struct.unpack_from("<HH", d, p + 3)
                if on():
                    way = list(self.pf.way)
                    self.pf = PathFinder(bpr, rows, d[p + 7:p + 3 + ln])
                    for w in way:
                        self.pf.add_way(*w)
                p += 3 + ln
            elif op == 0x07:
                it = d[p]; x, y, a, b = struct.unpack_from("<4h", d, p + 1); c = d[p + 9]; p += 10
                if on() and it not in self.inv and not self.item_gone[it]:
                    self.place_item(it, x, y, a, b, c)
            elif op == 0x08:
                x, y, w, h = struct.unpack_from("<4h", d, p + 1); off = struct.unpack_from("<I", d, p + 9)[0]; p += 13
                if on() and len(R["masks"]) < 20:
                    R["masks"].append(dict(x=x, y=y, w=w, h=h, px=self.masks_file[off:off + w * h], obj=[]))
                    R["masks"].sort(key=lambda m: m["y"] + m["h"])
            elif op == 0x0B:
                x, y = struct.unpack_from("<hh", d, p + 1); nm = cstr(d[p + 5:p + 13]).decode("cp437"); p += 13
                if on():
                    n = NPC(self.files.read(nm + ".NPC")); n.setdir(4); n.setpos(x, y); R["npc"] = n
            elif op == 0x0C:
                v = struct.unpack_from("<h", d, p + 1)[0]; p += 3
                if on():
                    R["startproc"] = v
            elif op == 0x0D:
                a, x, y = struct.unpack_from("<3h", d, p + 1); p += 7
                if on():
                    self.start_anim(a, x, y, relative=False)
            elif op == 0x0E:
                if on(): R["keep_tom"] = True
            elif op == 0x10:
                a = struct.unpack_from("<h", d, p + 1)[0]; p += 3
                if on():
                    self.special = Anim(self.ani, a, 0, 0); self.special_active = False
            elif op == 0x12:
                x, y = struct.unpack_from("<hh", d, p); p += 4
                if on():
                    self.pf.add_way(x, y)
            elif op == 0x13:
                if on(): R["dark"] = True
            elif op == 0x14:
                if on(): R["dark"] = True; R["dark14"] = True
            else:
                raise ValueError(f"{name}.ROM: unknown opcode {op:#x} at {p - 1:#x}")

    def set_picture(self, idx: int):
        t, w, h, po, px = self.grafik_entries[idx]
        R = self.room
        R["pic"], R["W"], R["H"] = idx, w, h
        R["bg"] = self.grafik[px:px + w * h]
        pal = bytearray(self.grafik[po:po + 768])
        pal[0:48] = self.sys_pal
        self.base_pal = pal

    def place_item(self, item: int, x: int, y: int, a: int, b: int, c: int):
        """058A:12B9 (ROM op 07 / script D3)."""
        R = self.room
        it = self.items[item]
        slot = None
        if it["sprite"] is not None and len([s for s in R["slots"] if not s["free"]]) < 10:
            slot = dict(x=x, y=y, w=it["w"], h=it["h"], sprite=it["sprite"], hidden=False, masked=False,
                        front=(c & 3) == 2, free=False, item=item)
            R["slots"].append(slot)
            if c & 3 == 1:
                # 099B:01FD: attach to the FIRST overlapping mask (masks sorted by bottom edge), which has
                # two object slots. If both are taken the object becomes a plain background object; it is
                # never attached to a second mask.
                for m in R["masks"]:
                    if (x <= m["x"] + m["w"] and x + it["w"] >= m["x"]
                            and y + it["h"] >= m["y"] and y <= m["y"] + m["h"]):
                        if len(m["obj"]) < 2:
                            m["obj"].append(slot); slot["masked"] = True
                        break
        R["hotspots"].append(dict(kind=1, x=x, y=y, w=it["w"], h=it["h"], name=it["name"], item=item,
                                  slot=slot, walk=(a, b)))

    def hotspot_of(self, item: int):
        for h in self.room["hotspots"]:
            if h["kind"] == 1 and h["item"] == item:
                return h
        return None

    def remove_item_from_room(self, item: int, free_sprite: bool):   # 0A1F:033C
        h = self.hotspot_of(item)
        if h:
            h["x"] = h["y"] = 1; h["w"] = h["h"] = -1
            if free_sprite and h["slot"]:
                h["slot"]["free"] = True
                for m in self.room["masks"]:
                    if h["slot"] in m["obj"]:
                        m["obj"].remove(h["slot"])

    def hit_test(self, wx: int, wy: int):
        for h in self.room["hotspots"]:
            if h["kind"] == 0 and self.exits_off:
                continue
            if h["x"] <= wx <= h["x"] + h["w"] and h["y"] <= wy <= h["y"] + h["h"]:
                return h
        return None

    def start_anim(self, a: int, x: int, y: int, relative: bool):
        """ROM 0D / script DC: negative id = front, x bit13 != bit15 = just behind Tom, DC x >= 28000 = relative."""
        layer = 0
        if a < 0:
            a, layer = -a - 1, 1
        xu = x & 0xFFFF
        if relative and 0 < x < 28000 and x & 0x2000 or not relative and ((xu >> 13) & 1) != ((xu >> 15) & 1):
            layer = -1
        if relative and x >= 28000:
            x = x - 30000 + self.X; y = y + self.Y
        anims = self.room["anims"]
        if len(anims) >= 12:
            return
        anims.append(Anim(self.ani, a, x, y, layer))

    def find_anim(self, a: int):
        for an in self.room["anims"]:
            if an.id == a:
                return an
        return None

    # ════════════════════════════════════════════════════════════════════════════════════════
    # Walking (spec/01_walking.md)
    # ════════════════════════════════════════════════════════════════════════════════════════
    def set_path(self, pts):
        """pts: list of (x, y, dir)."""
        self.path = pts; self.step = 0; self.arrived = False

    def walk_to(self, tx: int, ty: int, use_map=True):
        pf = self.pf
        if use_map:
            pf.find(self.X, self.Y, tx, ty, self.scroll[0], self.scroll[1], self.room["W"])
            n = pf.n
            pts = [(pf.X[i], pf.Y[i], pf.D[i]) for i in range(max(n, 0))]
            return n, pts
        pf.n = 0; pf.seg(self.X, self.Y, tx, ty)
        return pf.n, [(pf.X[i], pf.Y[i], pf.D[i]) for i in range(pf.n)]

    def cb_walk(self):
        """Timer callback 0 (0468:0004)."""
        if self.frozen:
            return
        P = self.path
        walking = self.step + 1 < len(P)
        if not walking:
            if P or not self.arrived:
                h = self.target
                if h and h["kind"] == 1 and self.step != 0 and self.final_dir is not None:
                    self.player.face(self.final_dir)
                elif h and h["kind"] == 0 and abs(self.X - h["walk"][0]) + abs(self.Y - h["walk"][1]) < 6:
                    pass
                else:
                    self.player.setdir(4)
                self.path = []; self.step = 0
                if not self.arrived:
                    self.arrived = True
                    if self.click_pending:
                        self.act_now = True; self.click_pending = False
        else:
            lim = 5 if self.tom_hidden else 4
            dx = dy = 0
            while abs(dx) + abs(dy) < lim and self.step < len(P) - 1:
                self.step += 1
                dx = self.X - P[self.step][0]
                if dx < 0: dx += 2
                if dx > 0: dx -= 2
                dy = self.Y - P[self.step][1]
            self.X, self.Y = P[self.step][0], P[self.step][1]
        if not self.scroll_lock:
            sx, sy = self.scroll; W, H = self.room["W"], self.room["H"]
            tx, ty = sx, sy
            if self.X - sx > 200 and sx + 320 < W: tx = sx + 4
            if self.X - sx < 120 and sx > 3: tx = sx - 4
            d3 = walking and P[self.step][2] == 3
            if self.Y - sy > 100 and (self.Y - sy > 140 or d3 or self.tom_hidden) and sy + 160 < H: ty = sy + 4
            if self.Y - sy < 80 and sy > 3: ty = sy - 4
            self.scroll_target = (tx, ty)
        if not self.tom_hidden:
            self.player.setpos(self.X, self.Y)
        if walking:
            self.player.setdir(P[self.step][2])

    def initial_scroll(self):                                # 0468:0285
        W, H = self.room["W"], self.room["H"]
        sx = max(0, min(self.X - 160, W - VIEW_W)); sx = (sx // 4) * 4
        sy = self.Y - 95 if self.Y > 95 else 0
        if H - self.Y < 110:
            sy = H - VIEW_H
        sy = max(0, min(sy, H - VIEW_H))
        self.scroll = self.scroll_target = (sx, sy)

    def face_dir(self, h, wx, wy):                           # 071D:0493
        if h["kind"] == 1:
            f = self.items[h["item"]]["face"]
            if 1 <= f <= 4:
                return {1: 1, 2: 0, 3: 2, 4: 3}[f]
        cx, cy = h["x"] + h["w"] // 2, h["y"] + h["h"] // 2
        ty = wy - self.player.rec[4][2] + 45
        dx, dy = cx - wx, cy - ty
        if abs(dx) > abs(dy):
            return 0 if dx > 0 else 1
        return 3 if dy > 0 else 2

    def reached(self, h) -> bool:                            # 0A1F:06CA
        if abs(self.X - h["walk"][0]) + abs(self.Y - h["walk"][1]) < 6:
            return True
        x0, y0 = max(0, h["x"] - 7), max(0, h["y"] - 5)
        return x0 <= self.X <= x0 + h["w"] + 14 and y0 <= self.Y <= y0 + h["h"] + 30

    # ════════════════════════════════════════════════════════════════════════════════════════
    # The game coroutine (093A:0300 room loop + 0468:0815 room main)
    # ════════════════════════════════════════════════════════════════════════════════════════
    def game(self, room="main", x=100, y=140, entry=-1):
        self.entry = entry
        while room.lower() != "exitus":
            self.locked += 1; self.screen_on = False
            self.room_load(room)
            self.locked -= 1
            room, x, y = yield from self.room_main(room, x, y)
        self.finished = True
        while True:
            yield

    def room_main(self, room, x, y):
        R = self.room
        self.inv_hidden = False
        self.frozen = not R["keep_tom"]
        if self.frozen:
            self.player.hidden = True
        self.X, self.Y = x, y
        self.path, self.step, self.arrived, self.target = [], 0, True, None
        self.final_dir, self.click_pending, self.act_now = None, False, False
        self.latch = False
        self.pending_goto = None
        self.out_room = None
        self.initial_scroll()
        self.pointer_hidden = True
        yield from self.wait(3)
        self.pointer_hidden = False
        lit = not R["dark"]
        if lit:
            self.screen_on = True
        e = self.entry
        if e != -1 and e > 5:
            self.entry_face(e); self.entry = -1; self.frozen = False
            yield
            if not lit and not R["dark14"]:
                self.screen_on = True; lit = True
        if R["startproc"] != -1:
            n, R["startproc"] = R["startproc"], -1
            yield from self.run_proc(n)
        if not self.screen_on:
            if self.entry in (5, -1):
                self.frozen = False
            yield
            self.screen_on = True
        if self.entry != -1:
            yield from self.entry_walk(self.entry)
            self.entry = -1
        self.frozen = False
        if self.pending_goto:
            self.out_room = self.pending_goto
        # main loop
        while self.out_room is None and not self.finished:
            if self.act_now:
                yield from self.perform_action()
                if self.out_room:
                    break
            self.update_label()
            mx, my = self.mouse
            if self.buttons & 2 and self.latch:
                self.verb = 0 if self.verb == -3 else (self.verb + 1 if self.verb < 4 else 0)
                self.latch = False
            if self.buttons & 1 and self.latch:
                self.latch = False
                if my < 158:
                    self.room_click(mx, my)
                old = self.verb
                yield from self.ui_click(mx, my)
                if self.verb in (5, 6):
                    yield from self.menu_loadsave(self.verb == 6, old)
                if self.pending_goto:
                    self.out_room = self.pending_goto
            elif self.buttons == 0:
                self.latch = True
            if self.keys:
                k = self.keys[-1]; self.keys.clear()
                yield from self.handle_key(k)
            if self.requests and self.out_room is None:
                yield from self.handle_request(self.requests.pop(0))
            yield
        # leave the room (0468:0FF5)
        nxt = self.out_room
        self.locked += 1; self.screen_on = False
        self.player.hidden = True; self.start_signal = False
        if self.player_swapped:
            self.player = NPC(self.player_data); self.player_swapped = False
        self.special, self.special_active = None, False
        self.pulse = None; self.scroll_lock = False
        self.dac_override.clear(); self.fade = 63
        self.bubble = None; self.label = None
        self.locked -= 1
        return nxt

    def entry_face(self, e):
        if e & 4:
            return
        self.player.setdir(e & 3)
        if e & 8:
            self.player.setdir(4)

    def entry_walk(self, e):                                 # 0468:0320
        if e & 4:
            return
        self.player.setdir(e & 3)
        if e & 8:
            self.player.setdir(4); return
        d = e & 3; X, Y = self.X, self.Y
        pts = []
        for i in range(40):
            pts.append({0: (X - 40 + i, Y, 0), 1: (X + 40 - i, Y, 1), 2: (X, Y + 40 - i, 2), 3: (X, Y - 40 + i, 3)}[d])
        self.target = None
        self.set_path(pts); self.frozen = False
        while not self.arrived:
            yield

    # ── clicks ───────────────────────────────────────────────────────────────────────────────
    def room_click(self, mx, my):                            # 0468:0A79
        wx, wy = mx + self.scroll[0], my + self.scroll[1]
        h = self.hit_test(wx, wy)
        if h:
            wx, wy = h["walk"]
        self.target = h
        self.clicked_verb = self.verb
        n, pts = self.walk_to(wx, wy)
        self.final_dir = self.face_dir(h, wx, wy) if h else None
        if 0 < n < 5:
            self.X, self.Y = pts[-1][0], pts[-1][1]; n = 0
        if h and h["kind"] == 0 and abs(self.X - h["walk"][0]) + abs(self.Y - h["walk"][1]) < 6:
            n = 0
        if n > 0:
            self.set_path(pts); self.click_pending = True
        elif n == 0:
            self.set_path([]); self.act_now = True
        else:
            self.set_path([]); self.click_pending = False

    def perform_action(self):                                # 0468:0480
        self.act_now = False
        h = self.target
        if h is None or not self.reached(h):
            return
        self.target = None
        v = self.clicked_verb
        if v == 0:
            if h["kind"] == 0:
                self.out_room = (h["room"], h["spawn"][0], h["spawn"][1])
                self.entry = h["entry"]; self.frozen = True
                return
        elif h["kind"] == 1:
            it = self.items[h["item"]]
            if v in (1, 2, 3, 4):
                key = {1: "look", 2: "take", 3: "use", 4: "talk"}[v]
                if it[key] is not None:
                    self.label_enabled = False
                    yield from self.run_item(it[key], h["item"], -1)
                    self.label_enabled = True
                else:
                    self.say(self.replies[key], *self.mouse, ticks=22)
            elif v == -3:
                r = False
                if it["use"] is not None:
                    r = yield from self.run_item(it["use"], h["item"], self.held)
                if not r:
                    self.say(self.replies["with"], *self.mouse, ticks=22)
        if h["kind"] == 1:
            it = self.items[h["item"]]
            yield from self.run_item(it["after"], h["item"], -1)
        if self.pending_goto:
            self.out_room = self.pending_goto

    def ui_click(self, mx, my):                              # 06C0:0330
        if not 160 <= my <= 196 + 4 or self.bar_off():       # no clicks on a hidden or covered bar
            return
        for i, (x1, y1, x2, y2, name) in enumerate(self.BUTTONS):
            if x1 <= mx <= x2 and y1 <= my <= y2:
                if i == 6 and not self.save_unlocked:
                    return
                self.verb = i
                return
        n = min(5, len(self.inv) - self.inv_scroll)
        for i in range(n):
            if 60 + 36 * i <= mx < 96 + 36 * i and 164 <= my <= 196:
                it = self.inv[self.inv_scroll + i]
                rec = self.items[it]
                v = self.verb
                if v in (0, 2):
                    self.held, self.verb = it, -3
                elif v == 1:
                    yield from self.run_item(rec["look"], it, -1)
                elif v == 3:
                    if rec["use"] is not None:
                        yield from self.run_item(rec["use"], it, -1)
                    else:
                        self.held, self.verb = it, -3
                else:
                    yield from self.run_item(rec["use"], it, self.held)
                return
        if 244 < mx < 270 and 160 < my < 180 and self.inv_scroll > 0:
            self.inv_scroll -= 1; self.redraw_bar()
        elif 244 < mx < 270 and 180 < my < 200 and len(self.inv) - self.inv_scroll > 5:
            self.inv_scroll += 1; self.redraw_bar()

    def handle_request(self, req):
        """Front-end requests (menu bar), run at the same point as keys: ("save"|"load", path)."""
        kind, path = req
        if kind == "save":
            if not self.save_game(0, path):
                yield from self.run_dialogue(self.exe_text, 0x54F, esc=False)
        elif kind == "load":
            r = self.load_game(0, path)
            if r is None:
                yield from self.run_dialogue(self.exe_text, 0x58B, esc=False)
            else:
                self.out_room = r; self.entry = -1

    def handle_key(self, k):
        if k in ("s", "S"):
            if not self.save_game(1):
                yield from self.run_dialogue(self.exe_text, 0x54F, esc=False)
        elif k in ("l", "L"):
            r = self.load_game(1)
            if r is None:
                yield from self.run_dialogue(self.exe_text, 0x58B, esc=False)
            else:
                self.out_room = r; self.entry = -1
        elif k in ("m", "M"):
            if self.music:
                self.music.toggle()
        elif k == "esc":
            r = yield from self.run_dialogue(self.exe_text, 0x51B, esc=False)
            if r == -1:
                self.out_room = ("Exitus", 0, 0)

    def menu_loadsave(self, save: bool, old_verb: int):
        r = yield from self.run_dialogue(self.exe_text, 0x334 if save else 0x3B8, esc=True)
        if r in (-1, -2, -3) or self.fix_esc_bug:
            self.verb = old_verb if old_verb not in (5, 6) else 0
        if r in (-1, -2, -3):
            slot = -r
            if save:
                if not self.save_game(slot):
                    yield from self.run_dialogue(self.exe_text, 0x54F, esc=False)
            else:
                res = self.load_game(slot)
                if res is None:
                    yield from self.run_dialogue(self.exe_text, 0x58B, esc=False)
                else:
                    self.out_room = res; self.entry = -1

    # ── hover label (0A1F:0827) ──────────────────────────────────────────────────────────────
    def update_label(self):
        mx, my = self.mouse
        text, x, y = None, 0, 0
        if my < 158:
            h = self.hit_test(mx + self.scroll[0], my + self.scroll[1])
            if h and h["name"]:
                text = h["name"]
                x = h["x"] + h["w"] // 2 - self.scroll[0]
                y = max(0, min(150, h["y"] + h["h"] // 2 - self.scroll[1]))
                if x + 8 * len(text) > 320:
                    x = 310 - 8 * len(text)
                x = max(0, x)
        elif not self.bar_off():
            for i, (x1, y1, x2, y2, name) in enumerate(self.BUTTONS):
                if x1 <= mx <= x2 and y1 <= my <= y2 and (i != 6 or self.save_unlocked):
                    text, x, y = self.verb_names[i], x1, 150
                    if x + 8 * len(text) > 320:
                        x = 310 - 8 * len(text)
            n = min(5, len(self.inv) - self.inv_scroll)
            for i in range(n):
                if 60 + 36 * i <= mx < 96 + 36 * i and 164 <= my <= 196:
                    text, x, y = self.items[self.inv[self.inv_scroll + i]]["name"], min(60 + 36 * i, 190), 150
        self.label = (text[:16], x, y) if text else None

    # ── inventory ─────────────────────────────────────────────────────────────────────────────
    def inv_add(self, item):
        if len(self.inv) < 30:
            self.inv.append(item)
            if len(self.inv) + 0 > 5 + self.inv_scroll:
                self.inv_scroll = len(self.inv) - 5
        self.redraw_bar()

    def inv_remove(self, item):
        if item in self.inv:
            self.inv.remove(item)
        while len(self.inv) - self.inv_scroll < 5 and self.inv_scroll > 0:
            self.inv_scroll -= 1
        self.redraw_bar()

    # ════════════════════════════════════════════════════════════════════════════════════════
    # Script VM (058A:01D1)
    # ════════════════════════════════════════════════════════════════════════════════════════
    def run_item(self, off, this, with_):
        """058A:0E69: run an item script; returns True if an IF-used-with block matched."""
        self.used_match = False
        if off is not None:
            self.verb = 0
            yield from self.vm(self.items_data, off, this, with_, 0xC0, True)
        else:
            self.verb = 0                       # the original resets the verb even for a missing script
        return self.used_match

    def run_proc(self, n):
        if 0 <= n < 256 and self.proc_offsets[n]:
            yield from self.vm(self.procs, self.proc_offsets[n], -1, -1, 0xC0, True)

    def vm(self, d, p, this, with_, end, exe):
        """Interpret until opcode `end`; returns the position after it. Not executing = just skip."""
        while d[p] != end:
            op = d[p]
            if op not in VM_LEN:
                return p + 1
            a = p + 1
            h = lambda k=0: struct.unpack_from("<h", d, a + k)[0]
            if op in (0xC4, 0xCB, 0xD4):
                if op == 0xC4:
                    cond = exe and h() == with_
                    term = 0xC5
                else:
                    cond = exe and (bool(self.flags[h()]) if op == 0xCB else not self.flags[h()])
                    term = 0xCC
                p = yield from self.vm(d, a + 2, this, with_, term, cond)
                if op == 0xC4 and cond:
                    self.used_match = True
                continue
            p += VM_LEN[op]
            if not exe:
                continue
            r = self.op(op, d, a, this, with_)
            if r is not None:
                yield from r
        return p + 1

    def op(self, op, d, a, this, with_):
        """Execute one opcode; returns a generator for blocking ones (or None)."""
        h = lambda k=0: struct.unpack_from("<h", d, a + k)[0]
        thisitem = lambda v: this if v == -1 else v
        if op == 0xC1:                                        # SAY
            text = d[a:a + 24]; x, y = h(24), h(26)
            if x == -1:
                x, y = self.mouse
            elif x == -2:
                x, y = 160, 80
            else:
                x, y = x - self.scroll[0], y - self.scroll[1]
            self.say(text, x, y, 35)
        elif op == 0xC2:                                      # take this
            self.remove_item_from_room(this, True); self.inv_add(this)
        elif op == 0xC3:                                      # morph this
            self.morph_item(this, d[a])
        elif op == 0xC6:
            self.inv_add(d[a])
        elif op == 0xC7:
            self.flags[h()] = 1
        elif op == 0xC8:
            self.flags[h()] = 0
        elif op == 0xC9:
            f = h(); self.flags[f] = 0 if self.flags[f] else 1
        elif op == 0xCA:
            return self.op_dialog(cstr(d[a:a + 8]).decode("cp437"), None)
        elif op == 0xCD:
            self.start_signal = True
        elif op == 0xCE:
            an = self.find_anim(h())
            if an:
                if an.paused == 1:
                    an.paused = 0xFF
                elif an.hidden:
                    an.hidden = False
                else:
                    an.loop_set = False
                an.wait_start = False
        elif op == 0xCF:
            return self.wait_anim(h())
        elif op == 0xD0:
            self.tom_hidden = False; self.player.setpos(self.X, self.Y)
        elif op == 0xD1:
            n = thisitem(h()); self.inv_remove(n); self.remove_item_from_room(n, True); self.item_gone[n] = 1
        elif op == 0xD2:
            return self.wait(h())
        elif op == 0xD3:
            item, x, y, aa, b = struct.unpack_from("<5h", d, a); c = d[a + 10]
            if item not in self.inv:
                self.item_gone[item] = 0; self.place_item(item, x, y, aa, b, c)
        elif op == 0xD5:
            if self.special:
                self.special.step, self.special.paused, self.special.ended = 0, 0, False
                self.special_active = True
        elif op == 0xD6:
            return self.wait_special()
        elif op == 0xD7:
            if self.special and self.special.paused == 1:
                self.special.paused = 0xFF
        elif op in (0xD8, 0xD9):
            hs = self.hotspot_of(this)
            if hs and hs["slot"]:
                hs["slot"]["hidden"] = op == 0xD8
        elif op == 0xDA:
            return self.op_dialog(cstr(d[a:a + 8]).decode("cp437"), (h(8), h(10), h(12)))
        elif op == 0xDB:
            room = cstr(d[a:a + 8]).decode("cp437"); x, y = h(8), h(10); e = d[a + 12]
            self.pending_goto = (room, x, y); self.entry = e if e < 128 else e - 256
        elif op == 0xDC:
            self.start_anim(h(), h(2), h(4), relative=True)
        elif op == 0xDD:
            an = self.find_anim(h())
            if an:
                self.room["anims"].remove(an)
        elif op == 0xDE:
            return self.script_walk([(h(), h(2))])
        elif op == 0xDF:
            self.tom_hidden = True; self.player.hidden = True
        elif op == 0xE0:
            self.special = Anim(self.ani, h(), 0, 0); self.special_active = False
        elif op == 0xE1:
            self.special, self.special_active = None, False
        elif op == 0xE2:
            nm = cstr(d[a:a + 8]).decode("cp437")
            p = NPC(self.files.read(nm + ".NPC")); p.setdir(4); p.setpos(self.X, self.Y); p.hidden = self.player.hidden
            self.player = p; self.player_swapped = True
        elif op == 0xE3:
            self.save_unlocked = True
        elif op == 0xE4:
            self.label_enabled = True
        elif op == 0xE5:
            self.label_enabled = False
        elif op == 0xE6:
            return self.do_fade(True)
        elif op == 0xE7:
            return self.do_fade(False)
        elif op == 0xE8:
            self.remove_item_from_room(thisitem(h()), False)
        elif op == 0xE9:
            self.pointer_hidden = True
        elif op == 0xEA:
            self.pointer_hidden = False
        elif op == 0xEB:
            self.scroll_lock = True
        elif op == 0xEC:
            self.scroll_lock = False
        elif op == 0xED:
            return self.script_walk([(h(), h(2)), (h(4), h(6))])
        elif op in (0xEE, 0xEF):
            hs = self.hotspot_of(thisitem(h()))
            if hs and hs["slot"]:
                hs["slot"]["front"] = op == 0xEE
        elif op == 0xF0:
            v = d[a]
            if 1 <= v <= 4:
                self.player.face({1: 1, 2: 0, 3: 2, 4: 3}[v])
        elif op == 0xF1:
            self.exits_off = True
        elif op == 0xF2:
            self.exits_off = False
        elif op == 0xF3:
            n = thisitem(h()); self.morph_item(n, n)
        elif op == 0xF4:
            self.pulse = [d[a], 0, 3]
        elif op == 0xF5:                                      # sound effect, `rep` times, `delay` ticks after each
            return self.op_sound(d[a], d[a + 1], d[a + 2], d[a + 3])
        elif op == 0xF6:
            self.inv_hidden = True
            self.redraw_bar()
        elif op == 0xF7:
            self.screen_on = True
        return None

    def morph_item(self, slot, rec):                          # 0B38:0021
        self.parse_item(slot, rec)
        self.morph[slot] = rec
        hs = self.hotspot_of(slot)
        if hs:
            hs["name"] = self.items[slot]["name"]
            if hs["slot"]:
                it = self.items[slot]
                hs["slot"]["sprite"] = it["sprite"]
                if it["sprite"] is None:
                    hs["slot"]["free"] = True

    def wait_anim(self, n):
        while True:
            an = self.find_anim(n)
            if an is None or an.paused == 1 or an.ended:
                return
            yield

    def op_sound(self, note, vel, rep, delay):               # 0468:65E3: the wait happens even with sound off
        for _ in range(rep):
            if self.music:
                self.music.effect(note, vel)
            yield from self.wait(delay)

    def wait_special(self):
        while self.special_active and self.special and self.special.paused != 1:
            yield

    def script_walk(self, points):
        pts = []
        x, y = self.X, self.Y
        for tx, ty in points:
            if (tx, ty) != (x, y):
                self.pf.n = 0; self.pf.seg(x, y, tx, ty)
                pts += [(self.pf.X[i], self.pf.Y[i], self.pf.D[i]) for i in range(self.pf.n)]
            x, y = tx, ty
        self.target = None
        was = self.frozen
        self.set_path(pts); self.frozen = False
        while not self.arrived:
            yield
        self.frozen = was and False

    def do_fade(self, fade_in: bool):
        for i in range(9):
            self.fade = min(63, i * 8) if fade_in else max(0, 63 - i * 8)
            yield
        self.fade = 63 if fade_in else 0

    def op_dialog(self, name, flags):
        try:
            data = self.files.read(name + ".DLG")
        except FileNotFoundError:
            return
        r = yield from self.run_dialogue(data, 0, esc=flags is None)
        if flags and r in (-1, -2, -3):
            for i, f in enumerate(flags):
                if f >= 0:
                    self.flags[f] = 1 if i == -r - 1 else 0

    # ════════════════════════════════════════════════════════════════════════════════════════
    # Dialogue / menu runner (08AF:0604, 08AF:030A)
    # ════════════════════════════════════════════════════════════════════════════════════════
    def run_dialogue(self, data: bytes, off: int, esc: bool):
        self.bubble = None
        scene = bytearray(self.frame[:320 * 160])
        self.draw_scene(scene)                               # freeze one full scene (objects, sorted pass)
        self.dialog = dict(scene=bytes(scene), lines=[], hot=-1)
        self.dac_override[1] = (63, 63, 63)
        result = 0
        while True:
            ptrs = struct.unpack_from("<3i", data, off)
            p = off + 12
            texts = []
            for _ in range(4):
                e = data.index(b"\0", p); texts.append(data[p:e]); p = e + 1
                if len(texts) > 1 and not texts[-1]:
                    texts.pop(); break
            npc, choices = texts[0], texts[1:4]
            self.dac_override[15] = (0, 0, 0)
            self.dialog["lines"] = []
            yield from self.wait(3)
            line = 0
            for row in self.wrap(npc):
                self.dialog["lines"].append((line, row, 1)); line += 1
            line += 1
            ranges = []
            for i, c in enumerate(choices):
                self.dac_override[6 + i] = (0, 63, 0)
                first = line
                for row in self.wrap(c):
                    self.dialog["lines"].append((line, row, 6 + i)); line += 1
                ranges.append((10 * first, 10 * line))
                line += 1
            self.dialog["ptrs"] = ptrs[:len(choices)]
            armed, pick, hot = False, None, -1
            self.latch = False
            while pick is None:
                mx, my = self.mouse
                new_hot = -1
                if 32 < mx < 288:
                    for i, (t, b) in enumerate(ranges):
                        if t < my < b:
                            new_hot = i
                if new_hot != hot:
                    if hot >= 0:
                        self.dac_override[6 + hot] = (0, 63, 0)
                    if new_hot >= 0:
                        self.dac_override[6 + new_hot] = (63, 0, 63)
                    hot = new_hot
                if new_hot < 0:
                    armed = False
                if self.buttons and new_hot >= 0:
                    armed = True
                elif armed and not self.buttons and new_hot >= 0:
                    pick = new_hot
                if self.keys:
                    k = self.keys[-1]; self.keys.clear()
                    if k.isdigit() and 1 <= int(k) <= len(choices):
                        pick = int(k) - 1
                    elif k == "esc" and esc:
                        pick = -1
                if pick is None:
                    yield
            if pick == -1:
                result = 0; break
            t = ptrs[pick]
            for i in range(3):
                self.dac_override.pop(6 + i, None)
            if t < 0:
                result = t; break
            off = t
        yield from self.wait(2)
        self.dialog = None
        for k in (6, 7, 8, 15):
            self.dac_override.pop(k, None)
        self.latch = False
        return result

    @staticmethod
    def wrap(b: bytes) -> list[bytes]:
        """08AF:00B0: 32-column wrap at a space or after '-'; the char after column 32 is never seen."""
        rows, rest = [], b
        while rest:
            if len(rest) <= 31:
                rows.append(rest); break
            cut = -1
            for i in range(min(31, len(rest) - 1), 0, -1):
                if rest[i] == 0x20:
                    cut = i; break
                if rest[i - 1] == 0x2D:
                    cut = i; break
            if cut <= 0:
                rows.append(rest[:31]); break
            rows.append(rest[:cut].rstrip(b" "))
            rest = rest[cut:].lstrip(b" ")
        return rows

    # ════════════════════════════════════════════════════════════════════════════════════════
    # Saves (035A:0009 / 035A:00F7) — compatible with the original SAVEGAME.00n
    # ════════════════════════════════════════════════════════════════════════════════════════
    def save_game(self, slot: int, path=None) -> bool:
        try:
            b = bytearray()
            b += self.room["name"].encode("cp437")[:8].ljust(8, b"\0")
            b += struct.pack("<hh", self.X, self.Y)
            inv = (self.inv + [0] * 30)[:30]
            b += struct.pack("<30h", *inv) + struct.pack("<hh", len(self.inv), self.inv_scroll)
            b += bytes(self.flags) + bytes(self.item_gone) + struct.pack("<256H", *self.morph)
            Path(path or self.save_folder / f"SAVEGAME.{slot:03d}").write_bytes(bytes(b))
            return True
        except OSError:
            return False

    def load_game(self, slot: int, path=None):
        p = Path(path or self.save_folder / f"SAVEGAME.{slot:03d}")
        try:
            d = p.read_bytes()
        except OSError:
            return None
        if len(d) < 1356:
            return None
        room = cstr(d[0:8]).decode("cp437", "replace"); x, y = struct.unpack_from("<hh", d, 8)
        if not self.files.exists(room + ".ROM"):                 # not a Tom Long save
            return None
        inv = list(struct.unpack_from("<30h", d, 12)); cnt, scr = struct.unpack_from("<hh", d, 72)
        self.inv, self.inv_scroll = inv[:cnt], scr
        self.flags[:] = d[76:588]; self.item_gone[:] = d[588:844]
        morph = list(struct.unpack_from("<256H", d, 844))
        for i in range(256):
            self.parse_item(i, morph[i] if morph[i] < self.n_records else i)
            self.morph[i] = morph[i]
        self.save_unlocked = True
        self.inv_hidden = False; self.redraw_bar()
        return (room, x, y)

    # ════════════════════════════════════════════════════════════════════════════════════════
    # Rendering
    # ════════════════════════════════════════════════════════════════════════════════════════
    def blit(self, scr, f, x, y, clip_h=VIEW_H):
        w, h, ox, oy, px = f
        x += ox; y += oy
        if x >= 320 or y >= clip_h or x + w <= 0 or y + h <= 0:
            return
        c0, c1 = max(0, -x), min(w, 320 - x)
        for r in range(max(0, -y), min(h, clip_h - y)):
            row = px[r * w + c0:r * w + c1]
            base = (y + r) * 320 + x + c0
            if 0 not in row:
                scr[base:base + len(row)] = row
            else:
                for i, v in enumerate(row):
                    if v:
                        scr[base + i] = v

    def blit_raw(self, scr, px, w, h, x, y, clip_h=VIEW_H, opaque=False):
        if opaque:
            for r in range(h):
                yy = y + r
                if 0 <= yy < clip_h:
                    scr[yy * 320 + x:yy * 320 + x + w] = px[r * w:(r + 1) * w]
            return
        self.blit(scr, (w, h, 0, 0, px), x, y, clip_h)

    def text(self, scr, s: bytes, x, y, colour, outline=None, clip_h=200):
        for k, ch in enumerate(s):
            g = FONT[ch * 8:ch * 8 + 8]
            for r in range(8):
                bits = g[r]
                if not bits:
                    continue
                yy = y + r
                if not 0 <= yy < clip_h:
                    continue
                for c in range(8):
                    if bits & (0x80 >> c):
                        xx = x + k * 8 + c
                        if 0 <= xx < 320:
                            if outline is not None:
                                for dy in (-1, 0, 1):
                                    for dx in (-1, 0, 1):
                                        if (dx or dy) and 0 <= xx + dx < 320 and 0 <= yy + dy < clip_h:
                                            i = (yy + dy) * 320 + xx + dx
                                            if scr[i] != colour:
                                                scr[i] = outline
                            scr[yy * 320 + xx] = colour

    def draw_scene(self, scr):
        """Callbacks 1 and 2: background objects, sorted pass, front objects (no advancing)."""
        R = self.room
        sx, sy = self.scroll
        bg, W = R["bg"], R["W"]
        for y in range(VIEW_H):
            o = (y + sy) * W + sx
            scr[y * 320:(y + 1) * 320] = bg[o:o + 320]
        idata = self.items_data
        for s in R["slots"]:
            if not s["free"] and not s["hidden"] and not s["masked"] and not s["front"] and s["sprite"] is not None:
                self.blit(scr, Frames.get(idata, s["sprite"]), s["x"] - sx, s["y"] - sy)
        lst, pkey = [], 0
        for n in (self.player, R["npc"]):
            if n is None:
                continue
            if n is self.player and self.special_active and self.special:
                continue
            f = n.cur()
            if n.hidden or not f:
                continue
            key = n.y + f[1] + f[3]
            lst.append((key, len(lst), f, n.x - sx, n.y - sy))
            if n is self.player:
                pkey = key
        anims = list(R["anims"])
        if self.special_active and self.special:
            self.special.x, self.special.y = self.player.x, self.player.y
            anims.append(self.special)
        for an in anims:
            if an.hidden:
                continue
            fa = an.frame_at()
            if not fa:
                continue
            f, dx, dy = fa
            key = (0x7D00 + len(lst) if an.layer == 1 else pkey - 1 if an.layer == -1 else an.y + dy + f[1] + f[3])
            lst.append((key, len(lst), f, an.x + dx - sx, an.y + dy - sy))
        lst.sort(key=lambda e: (e[0], e[1]))
        ms = R["masks"]; s = m = 0
        while s < len(lst) or m < len(ms):
            while m < len(ms) and (s == len(lst) or ms[m]["y"] + ms[m]["h"] <= lst[s][0]):
                mk = ms[m]
                self.blit_raw(scr, mk["px"], mk["w"], mk["h"], mk["x"] - sx, mk["y"] - sy)
                for o in mk["obj"]:
                    if not o["hidden"] and not o["free"] and o["sprite"] is not None:
                        self.blit(scr, Frames.get(idata, o["sprite"]), o["x"] - sx, o["y"] - sy)
                m += 1
            while s < len(lst) and (m == len(ms) or ms[m]["y"] + ms[m]["h"] >= lst[s][0]):
                _, _, f, x, y = lst[s]; self.blit(scr, f, x, y); s += 1
        for s_ in R["slots"]:
            if not s_["free"] and not s_["hidden"] and s_["front"] and not s_["masked"] and s_["sprite"] is not None:
                self.blit(scr, Frames.get(idata, s_["sprite"]), s_["x"] - sx, s_["y"] - sy)

    def bar_off(self) -> bool:
        return not self.bar_shown

    def redraw_bar(self):
        """0B10:000F. The original paints the bar only on these events (inventory change, arrow scroll, F6,
        load), never on entering a room: after F6 ("hide inventory") it stays blank, through room changes,
        until the next redraw with [0x808] clear. That is why the bar is gone in ENDE (F6 right before the jump
        there) but still shows in TIMEJUMP."""
        self.bar_shown = not self.inv_hidden

    def draw_bar(self, scr):
        scr[160 * 320:] = bytes(40 * 320)
        if not self.bar_shown:
            return
        for i, (x1, y1, x2, y2, name) in enumerate(self.BUTTONS):
            if i == 6 and not self.save_unlocked:
                continue
            w, h, px = self.ui[name]
            self.blit_raw(scr, px, w, h, x1, y1, clip_h=200)
        w, h, px = self.ui["ARROWS"]
        self.blit_raw(scr, px, w, h, 244, 160, clip_h=200)
        while len(self.inv) - self.inv_scroll < 5 and self.inv_scroll > 0:
            self.inv_scroll -= 1
        for i in range(min(5, len(self.inv) - self.inv_scroll)):
            ic = self.items[self.inv[self.inv_scroll + i]]["icon"]
            if ic is not None:
                self.blit_raw(scr, self.items_data[ic:ic + 1024], 32, 32, 60 + 36 * i, 164, clip_h=200, opaque=True)

    def cursor_pixels(self):
        if self.verb == -3:
            ic = self.items[self.held]["icon"]
            if ic is not None:
                src = self.items_data[ic:ic + 1024]
                return bytes(src[(r * 2) * 32 + c * 2] for r in range(16) for c in range(16))
        return self.cursors[self.verb if 0 <= self.verb <= 4 else 0]

    # ════════════════════════════════════════════════════════════════════════════════════════
    # One tick
    # ════════════════════════════════════════════════════════════════════════════════════════
    def tick(self):
        self.ticks += 1
        try:
            next(self.logic)
        except StopIteration:
            self.finished = True
        if self.room is None:
            return
        scr = self.frame
        if self.dialog:                                      # only callback 4 runs
            scr[:320 * 160] = self.dialog["scene"]
            for line, row, col in self.dialog["lines"]:
                self.text(scr, row, 35, 10 * line + 2, col, outline=15, clip_h=160)
        elif not self.locked:
            self.cb_walk()
            self.draw_scene(scr)
            self.player.advance()
            if self.room["npc"]:
                self.room["npc"].advance()
            for an in list(self.room["anims"]):
                an.advance(self.start_signal)
            if self.special_active and self.special:
                if not self.special.advance_special():
                    self.special_active = False
            if self.bubble:
                t, x, y, n = self.bubble
                self.text(scr, t, x, y, 1, clip_h=160)
                self.bubble[3] -= 1
                if self.bubble[3] <= 0:
                    self.bubble = None
            elif self.label and self.label_enabled and self.room["hotspots"]:
                t, x, y = self.label
                self.text(scr, t, x, y, 1, clip_h=160)
        self.draw_bar(scr)
        if self.pulse:
            c, lvl, st = self.pulse
            if lvl + st > 63: st = -st
            if lvl + st < 0: lvl = st = 0
            lvl += st
            self.pulse = [c, lvl, st]
            self.dac_override[c] = (0, 0, lvl)
        if not self.pointer_hidden:
            mx, my = self.mouse
            self.blit_raw(scr, self.cursor_pixels(), 16, 16, mx, my, clip_h=200)
        if not self.dialog:
            self.scroll = getattr(self, "scroll_target", self.scroll)

    def palette_rgb(self) -> bytes:
        """768 bytes of 8-bit RGB for the current frame (fades, overrides, screen off)."""
        if not self.screen_on:
            return bytes(768)
        pal = bytearray(self.base_pal)
        for k, (r, g, b) in self.dac_override.items():
            pal[k * 3:k * 3 + 3] = bytes((r, g, b))
        out = bytearray(768)
        f = self.fade
        for i in range(768):
            v = pal[i] & 63
            if i >= 48 and f < 63:
                v = v * f // 63
            out[i] = v * 255 // 63
        return bytes(out)
