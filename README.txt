Tom Long - The Time Adventure: new engine (Python)
===================================================

Run:   python tomlong.py "C:\Games\TOMLONG"        (or start it inside the game folder, or pick the folder)

Needs: Python 3.8+ with tkinter (included in the standard Windows installer). Nothing else.
       Sound uses the DLL that matches your Python (hwopl64.dll for 64-bit, hwopl32.dll for 32-bit) and
       Windows' own winmm; on Linux libhwopl.so and aplay/pacat (or the 'sounddevice' module).

Files: tomlong.py     window, mouse, keys, timing (tkinter)
       tl_engine.py   the engine: rooms, walking, drawing, scripts, dialogues, menus, saves
       tl_formats.py  file formats (pictures, sprites, characters, animations)
       tl_walk.py     the original path finder, reproduced exactly
       tl_music.py    music and sound effects: driver data, OPL emulator, sound card output (own thread)
       tl_intro.py    INTRO.EXE re-implemented: title pictures (PCX), animated title (PowerPacker frames), fades
       tl_orpheus.py  the game's ORPHEUS music driver re-implemented (same OPL register writes as the original)
       hwopl64.dll / hwopl32.dll / libhwopl.so
                      DOSBox's OPL2/OPL3 emulator (DBOPL) built from the hwopl source you supplied
                      (GPL v2+/v3: see the licence note in that source's BUILD.txt)
       vga8x8.bin     the IBM VGA 8x8 text font (taken from DOSBox's source; the game uses the PC's built-in font)


Controls (as in the original): left click = walk / act with the current verb, right click = next verb,
verb buttons and inventory in the bar, s = quick save, l = quick load, m = music on/off
(restarts the song from the beginning, as in the original), Esc = quit menu.
Esc also skips a dialogue where the original allows it (43 of the 58 dialogues: those started with
script opcode CA; the 15 started with DA - quest conversations with outcomes - can't be skipped).

Menu bar: Game (restart, quick save, quick load, external save/load to any file, play intro at start,
quit menu, exit), View (scale 1x-4x, aspect correction 4:3,
full screen F11: no title or menu bar, the picture scaled to the whole screen with the
aspect ratio kept - 16:10, or 4:3 with aspect correction; F11 again to leave it), Sound (music, sound effects), Help. These settings are remembered in
tomlong_settings.json next to tomlong.py.

Options: --intro on|off, --scale 1-4 (window size), --sound music|fx|none (as the original's argument: music and
effects / effects only / silent), --saves FOLDER (default: the game folder, so the original's
SAVEGAME.001-003 are shared both ways), --original-save-lock (Save button hidden until the Hardy call).

Translated files: put them in the game folder - the engine reads whatever is there, including a
translated ENGINE.EXE (menus, verb names, default replies).

Intro: as INTRO.EXE with TOMLONG.BAT - the two title pictures, the animated title with the sky flash and
MACCAIN.OGM, then the game; after quitting through the quit menu, the closing McCain picture. Any key or
mouse button skips (the key isn't passed on to the game). --intro on|off or the Game menu switches it.

External saves are ordinary SAVEGAME files (1356 bytes) with any name; the original's SAVEGAME.00n can be
loaded with External load too, and an external save renamed to SAVEGAME.001-003 works in the original.
Fixed compared with the original: Esc in the Load/Save menu, Save button available from the start.
