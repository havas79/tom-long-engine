# TomLongEngine: a Python re-implementation of *Tom Long – The Time Adventure* (1994)


Addresses like `0468:0815` are *segment:offset* in the loaded ENGINE.EXE image (MZ header 0xE00 bytes;
image offset = segment·16 + offset; DGROUP = segment 11E7 = file offset 0x12C70). They point to the original
routine that a piece of engine code reproduces.

---

## Contents

1. [Status](#1-status)
2. [Running the engine](#2-running-the-engine)
3. [Package layout and architecture](#3-package-layout-and-architecture)
4. [Timing](#4-timing)
5. [Game data files and formats](#5-game-data-files-and-formats)
6. [Rooms: loading, entering, leaving](#6-rooms-loading-entering-leaving)
7. [Walking, path finding and scrolling](#7-walking-path-finding-and-scrolling)
8. [Rendering](#8-rendering)
9. [Interaction: verbs, clicks, inventory](#9-interaction-verbs-clicks-inventory)
10. [The script VM](#10-the-script-vm)
11. [Dialogues and menus](#11-dialogues-and-menus)
12. [Saved games](#12-saved-games)
13. [Music and sound effects](#13-music-and-sound-effects)
14. [Intro and closing screen](#14-intro-and-closing-screen)
15. [The front end (window)](#15-the-front-end-window)
16. [Differences from the original](#16-differences-from-the-original)
17. [Verification](#17-verification)

---

## 1. Status

The engine plays the whole game from start to finish on the original data files, including translated ones.

| Area | State |
|---|---|
| Rooms, walking, scrolling, path finder | complete; path finder reproduced bug-for-bug |
| Drawing: background, objects, depth sorting, masks, NPCs, animations, text | complete |
| Interaction: verbs, hotspots, inventory, held items, default replies | complete |
| Script VM (all 56 opcodes C0–F7) | complete |
| Dialogues, Load/Save/Quit menus, failure messages | complete; Esc only where the original allows it |
| Saved games | byte-compatible with the original's SAVEGAME.001–003 |
| Music and sound effects | the ORPHEUS driver re-implemented; same OPL register stream as the original |
| Intro and closing screen (INTRO.EXE) | complete |
| Front end | tkinter window, scale 1–4×, 4:3 aspect correction, scaled full screen, menus, external saves, restart |

Only Python 3.8+ with tkinter is needed. Sound uses a small native OPL emulator library that ships with the
engine (DLLs for 32/64-bit Windows, a .so for Linux).

---

## 2. Running the engine

```
python tomlong.py ["C:\Games\TOMLONG"] [--scale 1-4] [--saves FOLDER] [--sound music|fx|none]
                  [--intro on|off] [--original-save-lock]
```

Without a folder argument, the engine uses the current folder if it holds ENGINE.EXE; otherwise it asks for
the folder.

| Option | Meaning |
|---|---|
| `--scale` | window size multiplier (default: last used, else 3) |
| `--saves` | folder for SAVEGAME.001–003 (default: the game folder, so saves are shared with the original both ways) |
| `--sound` | as the original's argument: `music` = music and effects, `fx` = effects only, `none` = silent |
| `--intro` | play the intro at start (default: last used, else on) |
| `--original-save-lock` | keep the original's Save-button lock until the Hardy conversation (script op E3) |

**Controls (as in the original).**

| Input | Action |
|---|---|
| left click | walk / use the current verb on the clicked thing; verb buttons and inventory in the bar |
| right click | next verb (Walk → Look → Take → Use → Talk → Walk; a held item → Walk) |
| `S` / `L` | quick save / quick load (slot 1) |
| `M` | music on/off (restarts the song from the beginning, as in the original) |
| `Esc` | quit menu; skips a dialogue where the original allows it |
| `1` `2` `3` | pick a dialogue choice |
| `F11` | full screen on/off |

**Menu bar.**

* **Game:** Restart…, Quick save, Quick load, External save…, External load…, "Play the intro at start",
  Quit menu…, Exit.
* **View:** Scale 1×–4×, Aspect correction (4:3), Full screen.
* **Sound:** Music, Sound effects. If sound can't start, the reason is shown, greyed out.
* **Help:** Controls.

Settings (scale, aspect, full screen, music, effects, intro) are kept in `tomlong_settings.json` next to
`tomlong.py`.

Translated files are used as they are: the engine reads whatever is in the game folder, including a
translated ENGINE.EXE (verb names, default replies, menus).

---

## 3. Package layout and architecture

| File | Contents |
|---|---|
| `tomlong.py` | tkinter front end: window, menus, input, timing loop, scaling, intro/outro playback |
| `tl_engine.py` | the engine core: rooms, walking, drawing, script VM, dialogues, menus, saves (no window, no sound) |
| `tl_formats.py` | file-format decoders: PLB pictures, RLE sprites, NPC, ANI, ANM, ITEMS.ITM, opcode lengths |
| `tl_walk.py` | the original path finder (`037B`), reproduced exactly |
| `tl_music.py` | music front: driver data, OPL emulator via ctypes, sound-card output thread, engine interface |
| `tl_orpheus.py` | the ORPHEUS Player386 music driver re-implemented |
| `tl_intro.py` | INTRO.EXE re-implemented (PCX, PowerPacker frames, fades), GUI-free |
| `hwopl64.dll`, `hwopl32.dll`, `libhwopl.so` | DOSBox's OPL emulator (DBOPL) with a small C interface; source in `hwopl_src/` (GPL) |
| `vga8x8.bin` | IBM VGA 8×8 text font (the original uses the BIOS font; this copy comes from DOSBox's source) |
| `headless.py` | scripted tests without a window (`wait/move/click/rclick/key/dlgend/shot/gif`) |

### 3.1 How the original is structured, and how the engine mirrors it

The original does all drawing inside the timer interrupt. The main loop, the script interpreter and the
dialogue runner busy-wait on a tick counter (`[0xF02C]`). The engine keeps this split:

* `Engine.tick()` is called 9.103 times per second by the front end. It first advances the **game logic
  coroutine** (a Python generator that `yield`s exactly where the original busy-waits). Then it runs the
  timer callbacks in the original order:
  0. walker and scroll (`0468:0004`);
  1. background objects;
  2. the depth-sorted pass (NPCs, animations, masks with attached objects), front objects, frame/step advance;
  3. label or speech bubble;
  4. the bar, colour pulse and mouse pointer, then the scroll position is committed.
* Every blocking script opcode (WAIT, walk-and-wait, wait-for-animation, dialogues, fades, sound repeats) is a
  generator that yields once per tick until its condition holds.
* While a dialogue runs, only the dialogue callback draws: the scene is frozen and the text drawn over it.

The front end only calls `set_mouse(x, y, buttons)`, `key(ch)` and `tick()`. It shows `frame` (64 000 palette
indices) with `palette_rgb()` (768 bytes of 8-bit RGB). Menu actions that need the game's state (external
load/save) are queued in `Engine.requests` and run at the same point in the room loop as keys.

---

## 4. Timing

| Clock | Rate | Used for |
|---|---|---|
| game tick | every 2nd PIT tick = 18.2065 / 2 = **9.103 Hz** | logic, drawing, animation, walking, script waits |
| RTC periodic interrupt | **1024 Hz** | the music driver (its tick rate = floor(1.6 × tempo) Hz) |
| BIOS clock | 18.2 Hz | INTRO.EXE timing |
| VGA retrace | 70 Hz | intro fades |

The front end runs the game at 9.103 ticks per second. After a stall it catches up by at most 3 ticks, and
after more than 1 s behind it resynchronises.

---

## 5. Game data files and formats

All integers are little-endian. Text is code page 437.

### 5.1 File list

| File | Contents |
|---|---|
| `ENGINE.EXE` | game program; also the source of verb names, default replies, menu texts, and the music driver's data |
| `GRAFIK.PLB` | room backgrounds (PCLL library) |
| `SYSTEM.PLB` | 16×16 mouse pointers + system palette |
| `ITEMS.ITM` | objects: names, icons, room sprites, scripts |
| `PROCS.CMD` | procedures (room start scripts etc.) |
| `*.ROM` | room descriptions (22 rooms) |
| `*.NPC` | characters (player, Mutter, Tomato, …) |
| `ANIMS.ANI` | room animations |
| `MASKS.MSK` | foreground mask pixels |
| `FLAGS.FLG` | initial values of the 512 game flags |
| `*.DLG` | dialogues (58) |
| `CURSORB/LOOKB/TAKEB/USEB/TALKB/LOADB/SAVEB/ARROWS.ANM` | bar graphics |
| `*.OGM`, `MCCAINGM.OGD`, `MCCAINFX.OGD` | songs, instrument bank, effect bank |
| `TITEL1.PCX`, `TITEL2.PCX`, `TITEL.ACF`, `TITEL.ACP`, `INTRO.EXE` | intro |

### 5.2 PCLL picture libraries (`GRAFIK.PLB`, `SYSTEM.PLB`)

* Magic `PCLL`; byte 6 = number of entries − 1. The directory starts at 0x10: 8 bytes per entry
  (`u8 type, 3 pad, u32 offset`).
* Entry data at `0x810 + offset`: 768-byte palette (6-bit VGA), then `w×h` raw pixels.
* Type → size:

| type | size | type | size |
|---|---|---|---|
| 0 | 320×240 | 6 | 480×80 |
| 1 | 320×144 | 7 | 352×160 |
| 2 | 320×160 | 8 | 400×160 |
| 3 | 320×400 | 9 | 640×160 |
| 4 | 320×200 | 11 | 32×32 |
| 5 | 480×160 | 12 | 16×16 |

* Room backgrounds may be wider or taller than the 320×160 view and scroll. ENDE (22) and TIMEJUMP (21) are
  320×200 pictures whose bottom 40 rows are never shown (see §8.8).
* **Palette:** a room's palette is its picture's palette with entries 0–15 (48 bytes) replaced by the system
  palette (`SYSTEM.PLB` entry 0), whose colour 0 is forced to black and colour 1 to white. The DAC ignores
  the top two bits of each byte.
* `SYSTEM.PLB` entries 0–4 are the 16×16 pointers for Walk, Look, Take, Use, Talk.

### 5.3 RLE sprites (NPC frames, animation frames, object room sprites)

```
u16 w, u16 h, s16 xoff, s16 yoff, RLE stream
```

The stream decodes to `w·h` bytes: byte `01 n v` = n × v, any other byte = itself. The pixels are stored in
Mode-X plane order: plane 0..3, then rows, then columns `w/4` (pixel at row r, column 4c+p). Colour 0 is
transparent. `w` must be a multiple of 4.

### 5.4 NPC (`*.NPC`)

5 records × 166 bytes: `u16 frame_count, u16 w, u16 h, u32 frame_offset[40]`, then frame data.

| record | animation |
|---|---|
| 0 | walk left |
| 1 | walk right |
| 2 | walk away (up) |
| 3 | walk toward (down) |
| 4 | standing; frame = facing |

* **Positions:** an NPC's position is its feet `(X, Y)`. It is drawn at `(X − w/2, Y − h + 10)` + the
  frame's own offsets, where w/h come from the current record (`071D:03FC`).
* **Direction codes** (also used by paths): 0 right, 1 left, 2 up/away, 3 down/toward; direction 4 = stand.
* `setdir(d)` picks walk animation `{0:1, 1:0, 2:2, 3:3}[d]`. `setdir(4)` switches to record 4 with the last
  walk animation's number as the facing frame. `face(d)` sets the standing frame directly.

### 5.5 ANIMS.ANI

* Index: `u32 offset[256]` at 0, `u32 size[256]` at 0x400.
* Record: `u16 frame_count, u16 step_count, u16 flags`, then three `s16[step_count]` arrays (frame, dx, dy),
  frames, and at the end `u32 frame_offset[frame_count]` relative to the record.
* **Flags:** bit 0 = wait for the start signal (script CD); bit 1 = stay on the last step (pause) instead of
  disappearing.
* Steps are `(frame, dx, dy)`. dx/dy are **absolute offsets from the animation origin**, not accumulated.
* Special frame values (`084B:0490`):

| value | meaning |
|---|---|
| −1 | loop mark (skip; later −2 jumps back here) |
| −2 | jump back to the loop mark (if one was set; script CE clears it to end the loop) |
| −3 | stop point: pause here until script CE releases it |
| −4 | move to the front layer |
| −5 | hide (end) |
| −6 | move back to the normal layer |

  An animation whose first step is −5 starts hidden.

### 5.6 ANM (bar graphics)

`"ANIM", u16 n, u16 w, u16 h`, then `n` raw `w×h` images from offset 10. The verb buttons use image 1 (the
unpressed button); ARROWS uses image 0.

### 5.7 ITEMS.ITM (objects)

* `u32 offset[256]` (0 ends the list); each record is a tag list ending with 0. Tags present overwrite a
  slot's fields; missing tags keep the old value (`058A:0EAB`, which matters for morphing).

| tag | layout | meaning |
|---|---|---|
| 01 | `u8 kind, s16 w, s16 h` [+ `u16 len, sprite`] if kind = 0 | hotspot size; kind 0 has a room sprite |
| 02 | `u8, char name[16]` | name (hover label) |
| 03 | script … C0 | Look script |
| 04 | `u8, u8, 1024 bytes` | 32×32 inventory icon (opaque) |
| 05 | script … C0 | Take script |
| 06 | script … C0 | Use script (also Use-with, with IF-used-with blocks) |
| 07 | script … C0 | "after" script: runs after every verb on this object |
| 08 | script … C0 | Talk script |
| 09 | `u8 face` | direction Tom faces when acting: 1 left, 2 right, 3 up, 4 down |

* The held-item pointer is the icon scaled down to 16×16 (every 2nd pixel and row, `06C0:02F1`).
* **Morphing** (`0B38:0021`): object slot `s` is re-parsed from record `r`, `morph[s] = r`. The hotspot's
  name and sprite follow; a sprite-less record frees the sprite slot.

### 5.8 PROCS.CMD

`u32 offset[256]`, then scripts (same VM as items; end with C0). A room's start procedure is set by ROM op
0C.

### 5.9 Room files (`*.ROM`) — `07A2:0086`

A list of opcodes ending with 05. Ops 09/0F/0A form conditional blocks (nestable).

| op | args (after the opcode) | meaning |
|---|---|---|
| 01 | `u8 pic` | background picture (GRAFIK.PLB entry) + palette |
| 02 | 13 bytes | skipped (unused) |
| 03 | `u8, char song[8]` | music: play `song.ogm` ("none" = silence) |
| 04 | `u8 entry, s16 x1,y1,x2,y2, char room[8], char label[16], s16 spawn_x, spawn_y, walk_x, walk_y` | exit hotspot: rectangle, target room, hover label, where Tom appears there, where he walks to here |
| 05 | — | end |
| 06 | `u8, s16 len, u16 bytes_per_row, u16 rows, bitmap` | walk map (1 bit per pixel, MSB = leftmost) |
| 07 | `u8 item, s16 x, y, a, b, u8 c` | place an object (unless carried or gone): x,y = sprite/hotspot top-left; a,b = walk-to point; c = layer (0 background, 1 attached to a mask, 2 in front) |
| 08 | `u8, s16 x, y, w, h, u32 offset` | foreground mask: pixels at MASKS.MSK + offset (colour 0 = see-through); max 20 |
| 09 / 0F | `s16 flag` | IF flag set / IF flag clear … |
| 0A | — | END IF |
| 0B | `u8, s16 x, y, char npc[8]` | room NPC at feet position (x,y), standing |
| 0C | `u8, s16 proc` | start procedure (runs on entering, §6.2) |
| 0D | `u8, s16 anim, s16 x, y` | start an animation (§8.5 for negative ids / x bits) |
| 0E | — | "keep Tom": Tom is shown and movable from the start |
| 10 | `u8, s16 anim` | Tom's special animation slot |
| 12 | `s16 x, y` | path-finder waypoint (max 10) |
| 13 | — | dark room: the screen stays off until the entry sequence lights it |
| 14 | — | dark room that stays dark until a script lights it (F7) |

Only flagged-on ops take effect inside conditional blocks. Song names in rooms:

| Rooms | Song |
|---|---|
| ANLEGER, SALOON | western |
| BAHNHOF | zeitbahn |
| CASTLE, KERKER1, STUBE | kerker |
| COMPUT, ENDE | maccain |
| LABOR | tomato |
| MAIN, TOMROOM | tomszimm |
| MERLIN | merlinsh |
| RIVERB1–3 | forrest |
| SHERWOOD | shiffdec |
| URZEIT1/2 | steinzei |
| TIMEJUMP | none |

HOF2, MSG1 and MSG2 have no op 03, so the previous song keeps playing.

### 5.10 FLAGS.FLG

512 bytes: initial game flags (0/1).

### 5.11 Dialogue files (`*.DLG`) and the menus in ENGINE.EXE

A node is three `s32` pointers, then up to four NUL-terminated strings: the NPC line, then 1–3 choices (an
empty string ends the list early). Pointer *i* belongs to choice *i*: ≥ 0 = file offset of the next node;
−1/−2/−3 = end with that result. The Load, Save and Quit menus and the error messages are nodes of the same
format inside ENGINE.EXE's DGROUP (offsets in §5.12).

### 5.12 Strings and nodes used from ENGINE.EXE (offsets relative to DGROUP, file 0x12C70)

| Offset | Content |
|---|---|
| 0x864, 0x869, 0x86F, 0x874, 0x87C, 0x883, 0x889 | verb names: Walk, Look, Take, Use, Talk, Load, Save |
| 0x4B2 / 0x4C9 / 0x4E0 / 0x4F0 / 0x506 | default replies: look / take / use / use-with / talk |
| 0x334 / 0x3B8 | Save / Load menu (choices 1–3 = slots) |
| 0x51B | quit menu (choice 1 = quit) |
| 0x54F / 0x58B | "save failed" / "load failed" messages |

Driver data segment: segment 0x1347 (file offset = header + 0x13470), 0x1A00 bytes. The engine validates it
by the frequency-table signature and searches the file for it if a rebuilt EXE moved it.

### 5.13 Music files

* **OMID songs (`*.OGM`):** magic `OMID`, `u16 pattern_table, u16 subsong_table` at 4.
  * Subsong *k* = `u16 start, loop, end` offsets into the order list.
  * Order-list records are 4 bytes: a time stamp `FE t24` (target tick), a pattern start
    `vel_add, ch<<4|slot, transpose, pattern`, `FD x x tempo`, `FC ch<<4 vol prg` (volume/program unless
    bit 7), or `FF` (stop the order list).
  * Pattern: `u16 first_wait`, then events `bh, bl` (+ `u16 duration` for notes). `bl & 0x80` = note
    (note = `bh&0x7F` + transpose, velocity = `bl&0x7F` + vel_add); otherwise a controller (`bh&0x7F`,
    `bl&0x7F`). `bh & 0x80` = more events in the same tick. Each group is followed by `u16 wait`.
* **ORPH banks (`*.OGD`):** `u16` at 4 = instrument table, at 6 = priority/type table, at 0x10 = drum map.
  * Each instrument is a script of 4-byte ops run per voice per driver tick. Program type 0x20 = a key map
    to other programs.
  * FX bank program 0 is the effect key map (one effect per note); GM program 120 is the drum kit.

### 5.14 Intro files

* `TITEL1.PCX`, `TITEL2.PCX`: 640×480, 4 bit planes, standard PCX RLE; the palette is in the header (>>2).
* `TITEL.ACP`: 768-byte 6-bit palette.
* `TITEL.ACF`: `u16 count` (104), then count × `{u16 x, y, w, h; u32 packed_size; u16 delay_ms}`, then one
  **PowerPacker 2.0** blob per frame. Each blob is `u32 len, u8 eff[4], bit stream read backwards,
  u24be unpacked_len, u8 skip_bits`. Frame 0 is the full 320×160 picture; frames 1… overwrite rectangles.
  INTRO.EXE reads all frame data in one 64 000-byte read.

---

## 6. Rooms: loading, entering, leaving

### 6.1 Room loop (`093A:0300`)

The game starts in room `main` at (100, 140). It loads and runs rooms until the next room is "Exitus" (the
quit menu), then the game ends.

### 6.2 Entering a room (`0468:0815`)

1. Clear the inventory-hidden flag `[0x808]`.
2. If the room has no op 0E, Tom is frozen and hidden.
3. Place Tom at the spawn point and compute the initial scroll (§7.4). The pointer is hidden for 3 ticks.
4. If the room is not dark, switch the screen on.
5. **Entry code** *e* (from the exit hotspot or the GOTO): if *e* > 5, set the facing now (bit 2 = none,
   bits 0–1 = direction, bit 3 = stand) and unfreeze. A dark-13 room is lit at this point.
6. Run the start procedure (ROM op 0C), if any.
7. If the screen is still off, wait a tick and switch it on.
8. **Entry walk** (`0468:0320`) for codes 0–5: Tom walks 40 px into the room in direction `e & 3`. Code 4 =
   no walk; bit 3 = just stand facing that way.
9. The main loop runs:
   * a pending action (§9.3);
   * the hover label;
   * right click = next verb, left click = room click + bar click (§9);
   * keys (all pending keys are read, only the last counts);
   * front-end requests.

### 6.3 Leaving a room (`0468:0FF5`)

* The screen goes off, Tom is hidden and the start signal cleared.
* A swapped player character (script E2) is restored to PLAYER.NPC.
* The special animation, colour pulse and scroll lock are cleared, and the palette overrides and fade reset.
* The bubble and label are removed.

The next room comes from an exit hotspot (walked to with Walk), a script GOTO (DB), a loaded game, or the quit
menu.

---

## 7. Walking, path finding and scrolling

### 7.1 Walk map

The walk map is 1 bit per pixel at world coordinates. A point is walkable if
`map[y·bpr + x/8] & (0x80 >> (x&7))` (x/8 truncates toward zero). Reads outside the map are unwalkable.

### 7.2 Path finder (`037B:071C`), in `tl_walk.py`

It is reproduced exactly, including its bugs, because they change routes:

1. If start = target, there is no path.
2. **Snap the start** if unwalkable (`037B:0549`): rings r = 1..4, using the original's buggy address
   `si·bpr + y + x + di/8`.
3. **Unwalkable target:** walk the line of sight from the start until it hits the first blocked pixel and stop
   at the last good one. If the start itself is blocked, fail.
4. **Direct line of sight** (Bresenham with the original's error terms): use the straight line.
5. **Stuck next to a wall** (last good point within 2 px of the start): try all neighbours within ±5. Use the
   first one with sight to the target; otherwise remember the farthest partial.
6. **8-px grid bend:**
   * consider every grid point in the visible screen (scroll x … +320, scroll y … +220, step 8) that sees
     both start and target;
   * minimise `(y1−gy)²+(gy−y0)²+(gx−x0)²+(x1−gx)²`;
   * the path is two segments.
7. **One room waypoint** (ROM op 12) with the same cost function.
8. **Two waypoints:** only if exactly one waypoint sees the start and exactly one sees the target, and the
   pair is in the visibility matrix. That 10×10 matrix overlaps the path arrays in the original, so only its
   first row survives; the overlap is modelled.
9. Otherwise a partial path to the last good point, or failure.

Paths are lists of `(x, y, direction)` per pixel (max 720 points). The segment direction follows the
dominant axis: horizontal → 0 right / 1 left; vertical → 2 up / 3 down.

### 7.3 Stepping (timer callback 0, `0468:0004`)

* **Speed:** each tick Tom advances along the path until he has moved at least 4 px (5 px while Tom is
  hidden). The horizontal distance counts 2 px less, giving about 6 px/tick horizontally and 4 vertically.
* **End of the path:**
  * at an object, with the click's facing known: face it (§9.3);
  * at an exit within 6 px of its walk point: keep the walking pose;
  * otherwise: stand.

  If a click was pending, the action now fires.
* Tom's NPC is placed at the walker position each tick unless Tom is hidden (DF).

### 7.4 Scrolling

The view is 320×160. It scrolls 4 px per tick unless the scroll is locked (EB):

* **Horizontal:** right if Tom is more than 200 px from the left edge, left if less than 120 (and the
  background allows).
* **Vertical:** down if Tom is more than 100 px from the top and either:
  * more than 140 px down, or
  * walking downward, or
  * hidden;

  and the background allows. Up if less than 80 px from the top.

**Initial scroll** (`0468:0285`):
* `sx = clamp(X − 160)`, rounded down to a multiple of 4.
* `sy = Y − 95` if Y > 95, else 0. If `H − Y < 110`, `sy = H − 160`. Then clamp.

---

## 8. Rendering

### 8.1 Frame order per tick

1. Background: 160 rows of the room picture at the scroll position.
2. **Background objects:** placed objects with layer 0, not attached to a mask, not hidden.
3. **Sorted pass:** player and room NPC, animations, and Tom's special animation, merged with the masks (§8.2).
4. **Front objects** (layer 2 / script EE).
5. Frame and animation steps advance (NPC walk frames, animations, special animation).
6. Speech bubble, or else the hover label (if labels are enabled and the room has hotspots).
7. The bar (§8.8), the colour pulse (F4), the mouse pointer.

### 8.2 Depth sorting and masks (`099B:03A3`)

* **Sort key** = the sprite's bottom edge on screen:
  * NPC: `y + yoff + h`.
  * Animation: `y + dy + yoff + h`.
  * Front-layer animation: `0x7D00 + n` (always on top).
  * "Just behind Tom" animation: Tom's key − 1.
* Equal keys keep insertion order (the original's Borland qsort is unstable; insertion order is used).
* Masks are kept sorted by bottom edge `y + h` and merged with the sorted sprites (painter's algorithm):

```
while sprites or masks remain:
    while masks remain and (no sprites left or mask.y+mask.h <= sprite.key):
        draw mask (colour 0 transparent), then its attached objects; next mask
    while sprites remain and (no masks left or mask.y+mask.h >= sprite.key):
        draw sprite; next sprite
```

So a mask covers every sprite whose bottom edge is above the mask's bottom edge. On a tie the mask is drawn
first.

* **Objects with layer 1** (`099B:01FD`) are attached at placement time to **one** mask: the first mask, in
  bottom-edge order, whose rectangle overlaps theirs (inclusive test). Each mask has two object slots. If both
  are taken, or no mask overlaps, the object becomes a plain background object. It is never attached to a
  second mask. Attached objects are drawn right after their mask, at the mask's depth.
  * Example: TOMROOM's drawer overlaps the shelf mask (bottom 107) and the desk mask (bottom 159). It belongs
    to the shelf mask, so Tom is drawn over it.

### 8.3 Objects

* At most 10 sprite slots per room.
* Script D8/D9 hide or show the object's sprite.
* EE/EF move it to the front or back.
* Removing an object (C2, D1) moves its hotspot off-screen and frees the sprite. E8 removes only the hotspot.

### 8.4 NPCs

Up to two: the player (PLAYER.NPC, or another character after E2) and the room NPC (op 0B). A hidden or
frame-less NPC is skipped. The player is replaced by the special animation while that runs.

### 8.5 Animations (`084B`)

* Max 12 per room.
* **Starting** (ROM 0D, script DC):
  * a negative id `a` means animation `−a−1` on the front layer;
  * ROM: if bit 13 ≠ bit 15 of x, the animation goes just behind Tom;
  * DC: `0 < x < 28000` with bit 13 set = behind Tom; `x ≥ 28000` = relative to Tom
    (`x − 30000 + X`, `y + Y`).
* An animation with flag bit 0 waits for the start signal (CD).
* CE releases a stop point, unhides a hidden animation, or ends a loop.
* CF waits until the animation is paused or has ended. DD removes it.
* **Tom's special animation** (ROM 10 / E0, started by D5) is drawn at Tom's position in place of Tom.
  * Steps −1/−2 are skipped, −3 is a stop point (D7 releases it), −4 moves it to the front.
  * D6 waits for it to pause or end.

### 8.6 Text

* The BIOS VGA 8×8 font; each character is 8 px wide.
* **Speech bubble** (`0A1F:0B9B`):
  * up to 24 characters in colour 1 (white), centred on the anchor and placed 8 px above it;
  * clamped to x ≥ 0 and `x + 8n ≤ 316`, y ≤ 150;
  * lasts 35 ticks (default replies: 22).
* **Hover label** (`0A1F:0827`): the hotspot name, up to 16 characters, centred on the hotspot.
  * In the bar, it shows the verb name or the item name at y = 150.
  * Off while a bubble shows or an item script runs (and via E5/E4).
* **Dialogue text:** colour 1 for the NPC line; colours 6–8 for the choices, with a colour-15 outline.

### 8.7 Palette effects

| Effect | Behaviour |
|---|---|
| Fades (E6/E7) | 9 ticks in steps of 8/63; affect colours 16–255, not the system colours |
| Screen off | room change, dark rooms: all black |
| Overrides | dialogue colours 1, 6–8, 15; pulse colour |
| F4 colour pulse | a colour's blue component cycles 0 → 63 → 0 in steps of 3 per tick |

### 8.8 The verb/inventory bar

* **Layout (rows 160–199):** buttons Walk (0,172), Look (19,162), Take (38,162), Use (19,182),
  Talk (38,182), Load (272,162), Save (291,162), each 16×16. Arrows at (244,160). Five 32×32 icon slots at
  x = 60 + 36·i, y = 164.
* **On-demand painting.** The original paints the bar only on certain events (`0B10:000F`):
  * an inventory change;
  * a scroll-arrow click;
  * F6 ("hide inventory");
  * loading a game.

  It never repaints on room entry. So after F6 the bar area stays black, across room changes, until the next
  repaint with `[0x808]` clear. This is why the bar is gone in ENDE (the script does F6 right before jumping
  there) while TIMEJUMP still shows it. The engine copies this exactly (`bar_shown` / `redraw_bar`).
* A blank bar is black in colour 0, which the dialogue's colour changes don't touch.

---

## 9. Interaction: verbs, clicks, inventory

### 9.1 Verbs

| Verb | Value |
|---|---|
| Walk | 0 |
| Look | 1 |
| Take | 2 |
| Use | 3 |
| Talk | 4 |
| Load | 5 |
| Save | 6 |
| holding an item | −3 |

Right click cycles 0→1→2→3→4→0; with an item held it goes back to Walk. After any item script the verb resets
to Walk, even when the object has no script for it (`058A:0E69`).

### 9.2 Room click (`0468:0A79`)

* If the click (y < 158) hits a hotspot, the walk target is the hotspot's walk point; otherwise the clicked
  point. Exits are ignored while exits are off (F1/F2).
* The facing at the end comes from the object's tag 09 or, failing that, from its centre relative to Tom
  (`071D:0493`).
* A path of 1–4 points is a jump (Tom is placed there). Standing within 6 px of an exit's walk point counts as
  arrived. A failed path cancels the action.

### 9.3 Performing the action (`0468:0480`)

* The action fires on arrival, if Tom "reached" the hotspot (`0A1F:06CA`). That means within 6 px of the walk
  point (Manhattan distance), or inside the hotspot rectangle widened by 7 px left/right, 5 up and 25 down.
* **Walk on an exit:** go to the target room with the exit's spawn point and entry code.
* **Look/Take/Use/Talk on an object:** run its script. With no script, the default reply from ENGINE.EXE
  appears at the mouse for 22 ticks.
* **Held item on an object:** run the target's Use script with the held item as "with". If no IF-used-with
  block matched, show the use-with reply.
* Then the object's **tag 07 "after" script** runs, for every verb.
* A GOTO set by a script takes effect after the action.

### 9.4 Bar clicks (`06C0:0330`)

* **A verb button:** select that verb. Save stays locked until op E3 unless the save lock is disabled (the
  default).
* **An inventory icon,** depending on the current verb:
  * Walk or Take: pick the item up (verb −3);
  * Look: run its Look script;
  * Use: run its Use script, or pick it up if it has none;
  * holding another item: run the clicked item's Use script with the held item.
* **Arrows:** scroll the five visible icons.
* Clicks and labels are ignored while the bar is blank.

### 9.5 Inventory

At most 30 items. Adding scrolls the view so the new item is visible; removing scrolls back so the row stays
full.

---

## 10. The script VM

### 10.1 Interpreter

The interpreter (`058A:01D1`) is recursive: `vm(code, pos, this, with, end_op, execute)`. It runs until
`end_op` (C0 at top level).

* **IF blocks:** C4…C5 = IF used-with item; CB…CC = IF flag set; D4…CC = IF flag clear. A false block is
  parsed in skip mode: operands are skipped, nothing runs.
* `this` = the object the script belongs to; −1 in an item operand means `this`.
* Unknown opcodes end the script.

### 10.2 Opcodes

Lengths include the opcode byte.

| op | len | name / effect |
|---|---|---|
| C0 | 1 | END |
| C1 | 29 | SAY `char text[24], s16 x, y`: bubble; x = −1 → at the mouse; x = −2 → screen centre (160,80); else world coordinates |
| C2 | 1 | TAKE this: remove from room, add to inventory |
| C3 | 2 | MORPH this into record `u8` |
| C4 | 3 | IF used-with `s16 item` … C5 |
| C5 | 1 | END IF (used-with) |
| C6 | 2 | add item `u8` to the inventory |
| C7 / C8 / C9 | 3 | flag `s16`: set / clear / toggle |
| CA | 9 | DIALOG `char name[8]` (Esc allowed; result ignored) |
| CB | 3 | IF flag set `s16` … CC |
| CC | 1 | END IF (flag) |
| CD | 1 | start signal for waiting animations |
| CE | 3 | release animation `s16`: stop point / unhide / end loop |
| CF | 3 | WAIT until animation `s16` is paused or ended |
| D0 | 1 | show Tom again |
| D1 | 3 | destroy item `s16` (inventory and room; marked gone) |
| D2 | 3 | WAIT `s16` ticks |
| D3 | 12 | place object `s16 item, x, y, a, b, u8 c` (un-gone) |
| D4 | 3 | IF flag clear `s16` … CC |
| D5 | 1 | start Tom's special animation |
| D6 | 1 | WAIT for the special animation (paused/ended) |
| D7 | 1 | release the special animation's stop point |
| D8 / D9 | 1 | hide / show this object's sprite |
| DA | 15 | DIALOG `char name[8], s16 flag[3]` (no Esc); result −1/−2/−3 sets flag[0/1/2] and clears the other two (−1 entries are skipped) |
| DB | 14 | GOTO room `char name[8], s16 x, y, u8 entry` (after the current action) |
| DC | 7 | start animation `s16 id, x, y` (§8.5) |
| DD | 3 | remove animation `s16` |
| DE | 5 | WALK Tom to `s16 x, y` (straight line) and wait |
| DF | 1 | hide Tom |
| E0 | 3 | set the special animation `s16` |
| E1 | 1 | clear the special animation |
| E2 | 9 | swap the player character to `char npc[8]` |
| E3 | 1 | unlock the Save button |
| E4 / E5 | 1 | hover labels on / off |
| E6 / E7 | 1 | fade in / out (blocking) |
| E8 | 3 | remove item `s16`'s hotspot only |
| E9 / EA | 1 | hide / show the mouse pointer |
| EB / EC | 1 | scroll lock on / off |
| ED | 9 | WALK via two points `s16 x1, y1, x2, y2` and wait |
| EE / EF | 3 | object `s16` in front / back |
| F0 | 2 | Tom faces `u8`: 1 left, 2 right, 3 up, 4 down |
| F1 / F2 | 1 | exits off / on |
| F3 | 3 | re-morph item `s16` into itself (reload its record) |
| F4 | 2 | colour pulse on colour `u8` |
| F5 | 5 | SOUND `u8 note, vel, repeats, delay`: send the effect `repeats` times, waiting `delay` ticks after each; the waits happen even with sound off |
| F6 | 1 | hide the inventory bar (`[0x808] = 1` + bar repaint) |
| F7 | 1 | switch the screen on |

---

## 11. Dialogues and menus

The dialogue runner is `08AF:030A`, with its wrapper at `08AF:0604`.

* **Display:**
  * the current scene is frozen;
  * the NPC line is drawn from row 0 (10 px per line, x = 35, y = 10·line + 2), then a blank line;
  * each choice follows, then a blank line.
* **Word wrap** (`08AF:00B0`): 31 characters per row, breaking at a space or after a hyphen. The 16-row
  screen limit therefore applies to NPC line + choices + the blank lines.
* **Colours:**
  * NPC line: colour 1, white;
  * choices: colours 6–8, green, magenta while hovered;
  * outline: colour 15, black.
* **Picking:**
  * hover over a choice's rows with 32 < x < 288, press and release the mouse there;
  * or press 1–3;
  * Esc ends the dialogue with result 0, but only where allowed.
* **Esc:** allowed for CA dialogues (43) and the Load/Save menus. Not allowed for DA dialogues (15: DIAL05B,
  05C, 05D, 11, 17A, 17B, 20, 21, 23A, 25C, 25D, 26A, 26B, 29A, 29C) or the quit menu.
* **Menus:**
  * Load/Save: result −1/−2/−3 = slot 1/2/3; afterwards the previous verb is restored;
  * Quit: result −1 → room "Exitus";
  * failed save/load shows the ENGINE.EXE error node.

---

## 12. Saved games

1356 bytes, compatible with the original (`035A:0009` save / `035A:00F7` load):

| Offset | Size | Contents |
|---|---|---|
| 0 | 8 | room name (NUL-padded) |
| 8 | 4 | `s16 X, Y` (Tom) |
| 12 | 60 | `s16 inventory[30]` |
| 72 | 4 | `s16 count, s16 scroll` |
| 76 | 512 | flags |
| 588 | 256 | item-gone flags |
| 844 | 512 | `u16 morph[256]` |

* **Loading:**
  * rejects files shorter than 1356 bytes or naming a room that doesn't exist;
  * re-parses every object from its morph record;
  * unlocks Save, repaints the bar, and enters the room with no entry code.
* Quick save/load uses slot 1. The menus use slots 1–3.
* **External save/load** uses the same format with any file name. External saves renamed to SAVEGAME.00n work
  in the original, and the original's files load via External load.

---

## 13. Music and sound effects

### 13.1 The original

The music driver is "ORPHEUS – Player386" (Christoph Winkler / Chris Hülsbeck, © 1994 VISION Engineering),
linked into ENGINE.EXE (code segment 0B8B, data segment 1347) and driving an OPL2 at port 0x388.

* **Command line:** `music` = sound on, music on; `fx` = sound on, music off; anything else = no driver at all.
* **Timing:** the RTC periodic interrupt at 1024 Hz adds `inc = floor(192·T/120)·64` to a 16-bit
  accumulator. A driver tick happens when the accumulator crosses 0x7FFF→0x8000 (signed overflow). That gives
  **floor(1.6·T) ticks/s**.
  * `set_tempo` resets the accumulator; the song load sets T = 120.
  * Before the first song is loaded the rate is 0, so effects don't play.
  * Effects always run at the current song's rate: 192 Hz with `fx`, and the old rate after "none".
* **Voices:** 9 OPL2 voices allocated by priority.
  * MIDI channels 0–15 play the song with the GM bank (MCCAINGM.OGD).
  * Channels 16–31 play effects with the FX bank (MCCAINFX.OGD).
  * Effects have priority 0x40 and music 0, so an effect steals a music voice.
* **`music_play(name)`** (`0B55:0197`):
  1. The same name as the current song does nothing (re-entering a room doesn't restart it).
  2. "last" reloads the current song.
  3. Otherwise `musicOff` (reset all voices and channels; levels go to 0), then remember the name.
  4. "none" = silence, tempo kept.
  5. Load the song (T = 120) and start subsong 1 with loop on, but only if music is on.
* **M key:** toggles music and calls `music_play("last")`, so the song starts again from the beginning.
* **Script F5** = `musicPlayFX`: a Note-On (0x90) on FX channel 16 with program override 0, the effect key
  map. Velocity 0 = note off (stops a looping effect). The game uses notes 1–21.

### 13.2 The re-implementation

* **`tl_orpheus.py`** (Orpheus class) implements the following:
  * **voice records** with the original's data-segment layout (9 voices × 0x90 bytes at ds 0x32C, OPL shadows
    × 20 at 0x14C, 32 channels × 9 at 0x20C, 16 sequencer slots × 11 at 0x95);
  * **the MIDI-like event handler:**
    * the music path and the FX path differ: the FX path leaves vibrato and the LFOs alone;
    * voice allocation and key maps;
    * controllers 1/7/10/0x40/0x79 and 0x46/0x47;
    * pitch bend;
  * **the per-voice instrument interpreter:** about 30 opcodes, including waits, loops, sub-programs,
    pitch/portamento/vibrato, carrier/modulator levels and slides, register writes, priority and random
    notes;
  * **the output stage:** channel volume, master volume, vibrato, bend;
  * **OPL register generation:** a shadow cache, writing only changes;
  * **block/F-number:** computed from a 32-bit frequency with the 50 000 Hz constant;
  * **the KSL quirk;**
  * **the order-list sequencer.**

  Unused GUS/sample opcodes are ignored. Initial values, the frequency table (`u32[129]` at ds 0x1544) and the
  noise table (ds 0x17D6) are read from the user's ENGINE.EXE.
* **`tl_music.py`:**
  * **DBOPL** (DOSBox's OPL emulator) is used through ctypes: `hwopl_create(rate)`,
    `hwopl_write(h, reg, val)`, `hwopl_generate(h, short*, n)` (mono 16-bit), `hwopl_destroy`.
  * **Rendering:** at 44 100 Hz, the 1024 Hz RTC is simulated with a fractional sample counter. Samples are
    generated up to each driver tick before that tick's register writes, so the writes land between the
    right samples.
  * **Threading:** one background thread renders blocks of about 1/30 s. The engine only queues commands
    (`play`, `toggle`, `effect`), which are applied before the next block, so audio never holds up the game.
  * **Output:**
    * Windows: winmm waveOut via ctypes (6 polled buffers);
    * Linux: `aplay`/`pacat`/`paplay` through a pipe, paced to stay about 0.12 s ahead;
    * anywhere: the `sounddevice` module if installed;
    * otherwise the game runs silent, with the reason shown in the Sound menu.
  * **Engine interface:** `play(name)`, `toggle()`, `set_music(on)`, `effect(note, vel)`, `close()`;
    `music_on` / `fx_on` flags.
  * **Menu mapping:** "Music" off is the original's `fx` mode (songs loaded but not started, so effects run at
    192 Hz). "Sound effects" off drops F5 effects.
  * **`render_wav(folder, song, seconds, out)`** is a test helper.
* **Build of the libraries** (from the supplied `hwopl_src/`):
  * `x86_64-w64-mingw32-g++ -O2 -shared -static -static-libgcc -static-libstdc++ -s -o hwopl64.dll hwopl.cpp dbopl.cpp`
  * the same with `i686-w64-mingw32-g++ … -Wl,--kill-at -o hwopl32.dll`
  * Linux: `g++ -O2 -shared -fPIC -o libhwopl.so hwopl.cpp dbopl.cpp`

  The DLLs depend only on KERNEL32/msvcrt. Licence: GPL (DBOPL GPL v2+, wrapper GPL v3).

---

## 14. Intro and closing screen

The original's `TOMLONG.BAT` runs `intro music` → `engine music` → (unless ENGINE.EXE exits with errorlevel 1)
`intro out`. INTRO.EXE (`065A:0051`) never starts the engine itself. `tl_intro.py` reproduces it as a
generator of drawing steps (`hi`, `hipal`, `lo`, `wait`, `music`) that the front end shows.

### 14.1 Start (normal mode)

MACCAIN.OGM plays throughout.

1. **Title pictures:** mode 12h, TITEL1.PCX ("Promotion Software"), then TITEL2.PCX (McCain). Each:
   * **fade in:** 64 Bresenham steps, palette set on every 2nd step at the 70 Hz retrace (about 0.46 s);
   * **hold** 54 BIOS ticks (about 3 s);
   * **fade out** the same way.
2. **Pause:** mode 13h, black, `sleep(1)`.
3. **Animation:**
   * palette = TITEL.ACP;
   * frame 0 is decrunched straight to the screen;
   * frames 1–103 each copy their rectangle and are held `delay_ms·18/1000` BIOS ticks;
   * **sky flash:** colour 0 is set after these frames:

     | frame | colour 0 (6-bit RGB) |
     |---|---|
     | 33 | (0,0,3) |
     | 41 | (0,0,6) |
     | 52 | (0,0,20) |
     | 63 | (0,0,28) |
     | 74 | (0,0,45) |
     | 83 | (0,36,63) |
     | 89 | (0,0,45) |
     | 94 | (0,0,28) |
     | 99 | (0,0,20) |
     | 103 | (0,0,0) |

4. **Hold:** the last frame stays 144 BIOS ticks (about 7.9 s).

* **Skipping:** any key or mouse button skips the **rest** of the sequence. The original leaves the key in the
  BIOS buffer for the next program; the engine doesn't pass it on.
* **Display:** the 640×480 pictures are resampled to the window size once, then each fade step only
  re-applies the palette.

### 14.2 Closing screen (`out` mode)

After quitting through the quit menu (room "Exitus"): TITEL2.PCX with fade in, 54 ticks or skip, fade out,
then the program ends. Closing the window directly skips it.

---

## 15. The front end (window)

### 15.1 Geometry

* **Windowed:** the frame is built at 320×200 (or 320×240 with aspect correction, spreading 200 rows evenly
  over 240) and zoomed by the chosen integer scale.
* **Full screen (F11):**
  * no title bar and no menu bar;
  * the picture takes the largest size that fits the screen with the aspect ratio kept (16:10, or 4:3 with
    aspect correction), at any factor, centred;
  * it is built at about half that size by nearest-neighbour resampling and zoomed ×2 (or more) by Tk, which
    keeps a frame at about 25 ms;
  * F11 leaves full screen; the state is remembered.
* Unchanged frames aren't redrawn.

### 15.2 Input

* Mouse coordinates are mapped back to 320×200.
* Keys go through `bind_all`, with Escape detected by keysym, `Cancel` or the character `\x1b`.
* The window grabs keyboard focus at start, on mouse enter and on click. Windows often starts a Tk window
  without focus, for example after the folder dialog.

### 15.3 Game menu actions

* **Restart:** asks for confirmation, then builds a new Engine from the same settings (no intro). The music
  object is kept.
* **External save/load:** file dialogs; the request is queued for the room loop (§3.1), so choosing one during
  a dialogue takes effect when it ends.

---

## 16. Differences from the original

### 16.1 Fixed

| Original behaviour | Engine |
|---|---|
| Save button hidden at start but still clickable and labelled; Save only unlocked by op E3 | unlocked from the start (`--original-save-lock` restores the lock) |
| Esc in the Load/Save menu leaves that menu as the current verb (every later click first opens the menu) | the previous verb is restored |
| A blank (hidden) bar still reacts to clicks and shows labels | ignored while blank |
| A missing script = a NULL pointer read (works by luck) | treated as "no script" |
| The key that skips the intro leaks into the game | not passed on |
| Layer-2 objects are never drawn (buggy flag handling) | drawn in front |

The patch `TOMFIX.COM` fixes the first two bugs (3 bytes) in the original EXE.

### 16.2 Copied on purpose

* Path-finder bugs (the start-snap address, the visibility matrix overlap): they change routes.
* The verb resets to Walk after any item script.
* A song doesn't restart on re-entry; M restarts the song.
* F5 waits even with sound off.
* The bar is painted only on events (§8.8).
* The driver's KSL quirk and the 50 000 Hz constant.

---

## 17. Verification

* **Path finder:** checked against the original code run under an x86 emulator (unicorn) on fuzzed routes.
* **Engine:**
  * a stress run of 238 interactions across all 22 rooms with no errors;
  * scripted headless runs of the opening, walking, scrolling, exits, Look/Take/Use-with, inventory, quick
    save/load, the quit menu, Esc in CA vs DA dialogues, and external save/load round trips;
  * about 8 ms per tick.
* **Music:** the driver re-implementation produced the same OPL2 register writes as the original driver
  (run under unicorn) for all 10 songs, each over a full loop plus 3 s, and for a 55 s demo of all 21
  effects. The engine's own rendering loop was rechecked on TOMSZIMM: 40 s, identical values and millisecond
  timing. The 64-bit DLL was tested in a Windows test program.
* **Intro:** the pictures, the animation frames and the sky flash were rendered and checked by eye; the full
  sequence was run in the window.
* **Full game:** played start to finish by the user with no crashes; graphics and music correct. The last
  reports (drawer layering, Esc in Mutti's dialogues, ending-screen bar) are fixed.

---

