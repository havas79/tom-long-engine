#!/usr/bin/env python3
"""
Tom Long – The Time Adventure: a new engine for the original 1994 game files.

    python tomlong.py [game folder] [--scale 3] [--saves folder]

Needs only Python 3 (with tkinter, which the standard Windows installer includes). Point it at the folder
with the original game (ENGINE.EXE, GRAFIK.PLB, ITEMS.ITM, *.ROM, *.DLG …). Translated game files placed in
that folder are used as they are, including a translated ENGINE.EXE (menus, verbs, replies).

Controls are the original ones: left click = walk / use the current verb, right click = next verb,
verb buttons and inventory in the bar; keys: s = quick save, l = quick load, m = music on/off, Esc = quit menu.
Menu bar: Game (quick save/load, quit), View (scale 1x-4x, aspect correction 4:3, full screen),
Sound (music, sound effects), Help. Settings are remembered in tomlong_settings.json next to this script.

Music and effects: the original ORPHEUS driver re-implemented (tl_orpheus.py) playing through DOSBox's OPL
emulator (hwopl64.dll / hwopl32.dll / libhwopl.so next to this script). --sound music|fx|none works like the
original's command line argument.

The intro (INTRO.EXE: two title pictures, the animated title with MACCAIN.OGM) plays at start and the closing
picture after quitting through the quit menu; any key or mouse button skips. Game menu: Restart, External
save/load (any file name and folder), "Play the intro at start".
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tl_engine import Engine, TICK_HZ  # noqa: E402
from tl_intro import intro  # noqa: E402

try:
    from tl_music import Music  # noqa: E402
except Exception as _ex:          # missing ctypes etc.: play without sound
    Music, MUSIC_ERROR = None, str(_ex)
else:
    MUSIC_ERROR = ""

TITLE = "Tom Long – The Time Adventure"


SETTINGS = Path(__file__).resolve().with_name("tomlong_settings.json")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def save_settings(d: dict):
    try:
        SETTINGS.write_text(json.dumps(d, indent=1))
    except Exception:
        pass


class Window:
    def __init__(self, root: tk.Tk, engine: Engine, scale: int | None = None, sound: str | None = None,
                 make_engine=None, play_intro: bool | None = None):
        self.root, self.e = root, engine
        self.make_engine = make_engine                 # () -> new Engine, for Restart
        self.settings = load_settings()
        self.intro_var = tk.BooleanVar(value=self.settings.get("intro", True) if play_intro is None else play_intro)
        self.mode = "game"                             # "intro" | "game" | "outro"
        self.seq = None; self.seq_job = None
        self.hi = None; self.hi_cache = None; self.last_lo = None; self.last_hi_pal = bytes(48)
        if sound:
            self.settings.update(music=sound == "music", fx=sound != "none")
        self.music_var = tk.BooleanVar(value=self.settings.get("music", True))
        self.fx_var = tk.BooleanVar(value=self.settings.get("fx", True))
        self.sound_error = ""
        self.start_sound()
        self.scale_var = tk.IntVar(value=scale or self.settings.get("scale", 3))
        self.aspect_var = tk.BooleanVar(value=self.settings.get("aspect", False))
        root.title(TITLE)
        root.configure(bg="black")
        self.build_menu()
        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="none")
        self.canvas.pack(expand=True, fill="both")
        self.img = tk.PhotoImage()
        self.shown = None
        self.item = self.canvas.create_image(0, 0, anchor="nw")
        self.buttons = 0
        self.mx = self.my = 0
        self.offset = (0, 0)
        c = self.canvas
        c.bind("<Motion>", self.on_move)
        c.bind("<ButtonPress-1>", lambda e: self.on_button(e, 1, True))
        c.bind("<ButtonRelease-1>", lambda e: self.on_button(e, 1, False))
        for b in (2, 3):
            c.bind(f"<ButtonPress-{b}>", lambda e: self.on_button(e, 2, True))
            c.bind(f"<ButtonRelease-{b}>", lambda e: self.on_button(e, 2, False))
        c.bind("<Configure>", self.on_resize)
        root.bind_all("<Key>", self.on_key)
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.geom = None; self.last_shown = None
        self.update_geom()
        self.apply_size()
        if self.settings.get("fullscreen"):
            root.after(50, self.toggle_fullscreen)
        # keyboard focus: on Windows the window often starts without it (after the folder dialog, or when
        # launched from a console), so keys would go nowhere until it is clicked; grab it now and on clicks
        root.after(150, self.grab_focus)
        c.bind("<Enter>", lambda e: self.grab_focus(False))
        self.next_time = time.perf_counter()
        if self.intro_var.get():
            self.run_sequence(intro(self.e.files.read), "intro")
        self.loop()

    # ── menu bar ────────────────────────────────────────────────────────────────────────────
    def build_menu(self):
        m = tk.Menu(self.root)
        game = tk.Menu(m, tearoff=False)
        game.add_command(label="Restart…", command=self.restart)
        game.add_separator()
        game.add_command(label="Quick save", accelerator="S", command=lambda: self.game_key("s"))
        game.add_command(label="Quick load", accelerator="L", command=lambda: self.game_key("l"))
        game.add_command(label="External save…", command=self.external_save)
        game.add_command(label="External load…", command=self.external_load)
        game.add_separator()
        game.add_checkbutton(label="Play the intro at start", variable=self.intro_var, command=self.apply_intro)
        game.add_separator()
        game.add_command(label="Quit menu…", accelerator="Esc", command=lambda: self.game_key("esc"))
        game.add_command(label="Exit", command=self.close)
        m.add_cascade(label="Game", menu=game)
        view = tk.Menu(m, tearoff=False)
        sc = tk.Menu(view, tearoff=False)
        for n in (1, 2, 3, 4):
            sc.add_radiobutton(label=f"{n}x  ({320 * n}×{200 * n})", value=n, variable=self.scale_var,
                               command=self.apply_size)
        view.add_cascade(label="Scale", menu=sc)
        view.add_checkbutton(label="Aspect correction (4:3)", variable=self.aspect_var, command=self.apply_size)
        view.add_separator()
        view.add_command(label="Full screen (F11 to leave)", accelerator="F11", command=self.toggle_fullscreen)
        m.add_cascade(label="View", menu=view)
        snd = tk.Menu(m, tearoff=False)
        st = "normal" if self.e.music else "disabled"
        snd.add_checkbutton(label="Music", accelerator="M", variable=self.music_var, command=self.apply_sound, state=st)
        snd.add_checkbutton(label="Sound effects", variable=self.fx_var, command=self.apply_sound, state=st)
        if not self.e.music:
            snd.add_separator()
            snd.add_command(label="No sound: " + self.sound_error[:70], state="disabled")
        m.add_cascade(label="Sound", menu=snd)
        hlp = tk.Menu(m, tearoff=False)
        hlp.add_command(label="Controls", command=self.show_controls)
        m.add_cascade(label="Help", menu=hlp)
        self.menubar = m
        self.root.configure(menu=m)

    # ── sound ───────────────────────────────────────────────────────────────────────────────
    def start_sound(self):
        if Music is None:
            self.sound_error = MUSIC_ERROR
            return
        try:
            self.e.music = Music(self.e.files.read, music_on=self.music_var.get(), fx_on=self.fx_var.get())
        except Exception as ex:
            self.sound_error = str(ex)
            print("no sound:", ex, file=sys.stderr)

    def apply_sound(self):
        mu = self.e.music
        if mu:
            mu.set_music(self.music_var.get())
            mu.fx_on = self.fx_var.get()
        self.settings.update(music=self.music_var.get(), fx=self.fx_var.get())
        save_settings(self.settings)

    # ── game menu actions ───────────────────────────────────────────────────────────────────
    def game_key(self, k):
        if self.mode == "game":
            self.e.key(k)

    def apply_intro(self):
        self.settings["intro"] = self.intro_var.get(); save_settings(self.settings)

    def restart(self):
        if self.mode != "game" or not self.make_engine:
            return
        if not messagebox.askyesno(TITLE, "Restart the game from the beginning?\n(Unsaved progress is lost.)"):
            return
        try:
            new = self.make_engine()
        except Exception as ex:
            messagebox.showerror(TITLE, f"Can't restart:\n{ex}"); return
        new.music = self.e.music
        if new.music:
            new.music.play("none")                     # silence now; the first room starts its song again
        self.e = new
        self.next_time = time.perf_counter()

    SAVE_TYPES = [("Tom Long saved games", "*.sav *.SAV SAVEGAME.*"), ("All files", "*.*")]

    def external_save(self):
        if self.mode != "game":
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Save the game to a file",
                                            initialdir=str(self.e.save_folder), defaultextension=".sav",
                                            filetypes=self.SAVE_TYPES, initialfile="tomlong.sav")
        self.grab_focus()
        if path:
            self.e.requests.append(("save", path))

    def external_load(self):
        if self.mode != "game":
            return
        path = filedialog.askopenfilename(parent=self.root, title="Load a saved game from a file",
                                          initialdir=str(self.e.save_folder), filetypes=self.SAVE_TYPES)
        self.grab_focus()
        if path:
            self.e.requests.append(("load", path))

    # ── intro / outro (tl_intro.py drives, this only shows) ───────────────────────────────────
    def run_sequence(self, gen, mode):
        self.mode, self.seq = mode, gen
        self.skip_armed = False
        self.root.after(1, self.seq_step)

    def seq_step(self):
        self.seq_job = None
        if self.mode not in ("intro", "outro"):
            return
        while True:
            try:
                st = next(self.seq)
            except StopIteration:
                self.end_sequence(); return
            k = st[0]
            if k == "wait":
                self.seq_job = self.root.after(max(1, int(st[1] * 1000)), self.seq_step); return
            if k == "music":
                if self.e.music:
                    self.e.music.play(st[1])
            elif k == "hi":
                self.hi = st[1]; self.hi_cache = None; self.present_hi(st[2])
            elif k == "hipal":
                self.present_hi(st[1])
            elif k == "lo":
                self.hi = None
                self.last_lo = (st[1], bytes(v * 255 // 63 for v in st[2]))
                self.present(*self.last_lo)

    def skip_sequence(self):
        if self.seq_job:
            self.root.after_cancel(self.seq_job); self.seq_job = None
        self.end_sequence()

    def end_sequence(self):
        mode, self.seq, self.hi, self.hi_cache = self.mode, None, None, None
        if mode == "outro":
            self.close(); return
        self.mode = "game"
        self.e.keys.clear()                             # the skipping key doesn't leak into the game
        if self.e.music:
            self.e.music.play("none")
        self.next_time = time.perf_counter()

    def present_hi(self, pal48):
        """A 640x480 16-colour picture at the window size: resampled once, palette applied per fade step."""
        W, H = self.out_size()
        z = max(1, W // 640)
        w, h = W // z, H // z
        if self.hi_cache is None or self.hi_cache[0] != (w, h):
            src, cols = self.hi, [x * 640 // w for x in range(w)]
            rows = []
            for y in range(h):
                r = (y * 480 // h) * 640
                line = src[r:r + 640]
                rows.append(line if w == 640 else bytes(line[c] for c in cols))
            self.hi_cache = ((w, h), b"".join(rows), z)
        (w, h), px, z = self.hi_cache
        pal = bytes(v * 255 // 63 for v in pal48) + bytes(768 - 48)
        rgb = bytearray(len(px) * 3)
        rgb[0::3] = px.translate(pal[0::3]); rgb[1::3] = px.translate(pal[1::3]); rgb[2::3] = px.translate(pal[2::3])
        self.img.configure(data=b"P6 %d %d 255\n" % (w, h) + bytes(rgb), format="PPM", width=w, height=h)
        self.shown = self.img.zoom(z) if z > 1 else self.img
        self.canvas.itemconfigure(self.item, image=self.shown)
        self.last_hi_pal = pal48
        self.last_shown = None

    def show_controls(self):
        messagebox.showinfo(TITLE, "Left click: walk / use the current verb\nRight click: next verb\n"
                                   "S: quick save   L: quick load   M: music on/off\nEsc: quit menu (or skip a skippable dialogue)\n"
                                   "1, 2, 3: pick a dialogue choice\nF11: full screen on/off (the menu bar is hidden in full screen)")

    # ── geometry ────────────────────────────────────────────────────────────────────────────
    @property
    def scale(self) -> int:
        return self.scale_var.get()

    def is_fullscreen(self) -> bool:
        return bool(self.root.attributes("-fullscreen"))

    def update_geom(self):
        """Output geometry: the picture is built at w x h and zoomed by the integer z (W = w*z, H = h*z).
        Windowed: w = 320, h = 200/240, z = the chosen scale. Full screen: the largest size that fits the screen
        with the aspect ratio kept (any factor, not only whole ones); it is built at about half that size and
        zoomed x2 (or more) by Tk, which keeps it fast."""
        base_h = 240 if self.aspect_var.get() else 200
        if self.is_fullscreen():
            cw, ch = self.root.winfo_width(), self.root.winfo_height()
            if cw < 320 or ch < 200:
                cw, ch = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            k = min(cw / 320, ch / base_h)
            W0, H0 = int(320 * k), int(base_h * k)
            z = max(1, int(k) // 2)
            w, h = W0 // z, H0 // z
        else:
            z, w, h = self.scale, 320, base_h
        if getattr(self, "geom", None) != (w, h, z):
            self.geom = (w, h, z)
            self.cols = None if w == 320 else [x * 320 // w for x in range(w)]
            self.colidx = None if self.cols is None else [r * 320 + c for r in range(200) for c in self.cols]
            self.rows = None if h == 200 else [y * 200 // h for y in range(h)]
            self.hi_cache = None

    def out_size(self):
        w, h, z = self.geom
        return w * z, h * z

    def refresh(self):
        self.last_shown = None
        if self.mode == "game":
            self.present()
        elif self.hi is not None:
            self.hi_cache = None; self.present_hi(self.last_hi_pal)
        elif self.last_lo:
            self.present(*self.last_lo)

    def apply_size(self):
        self.settings.update(scale=self.scale, aspect=self.aspect_var.get())
        save_settings(self.settings)
        self.update_geom()
        if not self.is_fullscreen():
            w, h = self.out_size()
            self.canvas.configure(width=w, height=h)
            self.root.geometry("")                    # shrink/grow the window to the new size
        self.on_resize()
        if self.mode == "game":
            self.present()
        elif self.hi is not None:
            self.hi_cache = None; self.present_hi(self.last_hi_pal)
        elif self.last_lo:
            self.present(*self.last_lo)

    def toggle_fullscreen(self):
        on = not self.is_fullscreen()
        # no title bar and no menu bar in full screen (F11 again to leave it)
        self.root.configure(menu="" if on else self.menubar)
        self.root.attributes("-fullscreen", on)
        self.settings["fullscreen"] = on; save_settings(self.settings)
        self.root.after(150, self.apply_size)
        self.root.after(600, self.apply_size)          # some window managers settle late

    def on_resize(self, _e=None):
        if _e is not None and self.is_fullscreen():
            old = self.geom
            self.update_geom()
            if self.geom != old:
                self.refresh()
        w, h = self.out_size()
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.offset = (max(0, (cw - w) // 2), max(0, (ch - h) // 2))
        self.canvas.coords(self.item, *self.offset)

    def to_game(self, e):
        ox, oy = self.offset
        w, h = self.out_size()
        return (e.x - ox) * 320 // w, (e.y - oy) * 200 // h

    # ── input ───────────────────────────────────────────────────────────────────────────────
    def on_move(self, e):
        self.mx, self.my = self.to_game(e)
        if self.mode == "game":
            self.e.set_mouse(self.mx, self.my, self.buttons)

    def grab_focus(self, force=True):
        try:
            if force:
                self.root.lift(); self.root.focus_force()
            self.canvas.focus_set()
        except tk.TclError:
            pass

    def on_button(self, e, bit, down):
        if down:
            self.canvas.focus_set()
            if self.mode != "game":
                self.skip_sequence(); return
        self.mx, self.my = self.to_game(e)
        self.buttons = (self.buttons | bit) if down else (self.buttons & ~bit)
        self.e.set_mouse(self.mx, self.my, self.buttons | (bit if down else 0))

    def on_key(self, e):
        if self.mode != "game":
            if e.keysym not in ("F11",) and (e.char or e.keysym in ("Escape", "Return", "space")):
                self.skip_sequence()
            return
        if e.keysym in ("Escape", "Cancel") or e.char == "\x1b":
            self.e.key("esc")
        elif e.char and e.char.isprintable():
            self.e.key(e.char)

    # ── drawing ─────────────────────────────────────────────────────────────────────────────
    def present(self, fr=None, pal=None):
        if fr is None:
            fr, pal = self.e.frame, self.e.palette_rgb()
        w, h, z = self.geom
        key = (fr, pal, self.geom)
        if key == self.last_shown:                     # nothing changed since the last tick: skip the work
            return
        self.last_shown = (bytes(fr), pal, self.geom)
        if self.colidx is not None:                    # widen each row (nearest neighbour)
            fr = bytes(map(fr.__getitem__, self.colidx))
        rgb = bytearray(len(fr) * 3)
        rgb[0::3] = fr.translate(pal[0::3])
        rgb[1::3] = fr.translate(pal[1::3])
        rgb[2::3] = fr.translate(pal[2::3])
        if self.rows is not None:                      # 200 rows -> h rows, spread evenly
            src, n = bytes(rgb), w * 3
            data = b"".join(src[r * n:(r + 1) * n] for r in self.rows)
        else:
            data = bytes(rgb)
        self.img.configure(data=b"P6 %d %d 255\n" % (w, h) + data, format="PPM", width=w, height=h)
        self.shown = self.img.zoom(z) if z > 1 else self.img
        self.canvas.itemconfigure(self.item, image=self.shown)

    def loop(self):
        if self.mode != "game":
            self.root.after(50, self.loop); return
        now = time.perf_counter()
        steps = 0
        while now >= self.next_time and steps < 3:          # catch up a little after a stall
            self.e.set_mouse(self.mx, self.my, self.buttons)
            self.e.tick()
            self.next_time += 1 / TICK_HZ
            steps += 1
        if now - self.next_time > 1:
            self.next_time = now
        if steps:
            self.present()
            mu = self.e.music
            if mu and mu.music_on != self.music_var.get():          # the M key toggled it
                self.music_var.set(mu.music_on); self.apply_sound()
        if self.e.finished:                              # quit menu -> "Exitus": the closing screen, as TOMLONG.BAT
            if self.e.music:
                self.e.music.play("none")
            self.run_sequence(intro(self.e.files.read, out=True), "outro")
            self.root.after(50, self.loop)
            return
        delay = max(1, int((self.next_time - time.perf_counter()) * 1000))
        self.root.after(delay, self.loop)

    def close(self):
        save_settings(self.settings)
        if self.e.music:
            self.e.music.close()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=TITLE)
    ap.add_argument("game", nargs="?", help="folder with the original game files")
    ap.add_argument("--scale", type=int, choices=(1, 2, 3, 4), help="window size multiplier (default: last used, else 3)")
    ap.add_argument("--saves", help="folder for SAVEGAME.001-003 (default: the game folder)")
    ap.add_argument("--sound", choices=("music", "fx", "none"),
                    help="like the original: music = music and effects, fx = effects only, none = silent "
                         "(default: last used; also in the Sound menu)")
    ap.add_argument("--intro", choices=("on", "off"), help="play the intro at start (default: last used, else on)")
    ap.add_argument("--original-save-lock", action="store_true",
                    help="keep the Save button locked until the Hardy conversation, as in the original")
    a = ap.parse_args()
    root = tk.Tk()
    folder = a.game
    if not folder:
        here = Path.cwd()
        if (here / "ENGINE.EXE").exists() or (here / "engine.exe").exists():
            folder = str(here)
        else:
            folder = filedialog.askdirectory(title="Choose the Tom Long game folder")
            if not folder:
                return
    def make_engine():
        return Engine(folder, save_folder=a.saves, save_unlocked=not a.original_save_lock)
    try:
        engine = make_engine()
    except Exception as ex:
        messagebox.showerror(TITLE, f"Can't start the game:\n{ex}")
        return
    Window(root, engine, a.scale, a.sound, make_engine, None if a.intro is None else a.intro == "on")
    root.mainloop()


if __name__ == "__main__":
    main()
