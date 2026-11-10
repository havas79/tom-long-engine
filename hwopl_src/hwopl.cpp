/*
 * hwopl.cpp - a tiny C interface to the DOSBox OPL2/OPL3 emulator (DBOPL),
 * for use from Python via ctypes (hw_editor.py plays the game's AdLib music
 * with it). DBOPL: Copyright (C) 2002-2011 The DOSBox Team, GPL v2 or later.
 * This wrapper follows pyopl.cpp by Adam Nielsen (GPL v3 or later), so the
 * DLL as a whole is under the GNU GPL v3.
 *
 *   void* hwopl_create(unsigned rate)            -> handle (NULL on failure)
 *   void  hwopl_write(void* h, int reg, int val) -> write an OPL register
 *   void  hwopl_generate(void* h, short* out, int samples)
 *                                                -> mono 16-bit samples
 *   void  hwopl_destroy(void* h)
 *   int   hwopl_version(void)                    -> 1
 */
#include <new>
#include <cstdint>
#include "dbopl.h"

#if defined(_WIN32)
#define HWOPL_API extern "C" __declspec(dllexport)
#else
#define HWOPL_API extern "C" __attribute__((visibility("default")))
#endif

namespace {
struct Sink : public MixerChannel {
    int16_t *out;
    static int16_t clip(Bit32s v) {
        v <<= 1;                                  // same gain as pyopl
        if (v > 32767) return 32767;
        if (v < -32768) return -32768;
        return (int16_t)v;
    }
    virtual void AddSamples_m32(Bitu samples, Bit32s *buffer) {
        for (Bitu i = 0; i < samples; i++) *out++ = clip(buffer[i]);
    }
    virtual void AddSamples_s32(Bitu samples, Bit32s *buffer) {
        for (Bitu i = 0; i < samples; i++) *out++ = clip(buffer[i * 2]);
    }
};
struct Opl {
    Sink sink;
    DBOPL::Handler handler;
};
}

HWOPL_API void *hwopl_create(unsigned rate) {
    Opl *o = new (std::nothrow) Opl();
    if (!o) return 0;
    o->handler.Init(rate);
    return o;
}

HWOPL_API void hwopl_write(void *h, int reg, int val) {
    if (h) ((Opl *)h)->handler.WriteReg((Bit32u)reg, (Bit8u)val);
}

HWOPL_API void hwopl_generate(void *h, int16_t *out, int samples) {
    if (!h || !out) return;
    Opl *o = (Opl *)h;
    while (samples > 0) {
        int n = samples > 512 ? 512 : samples;
        if (n < 2) {                              // DBOPL needs at least 2
            int16_t tmp[2];
            o->sink.out = tmp;
            o->handler.Generate(&o->sink, 2);
            *out++ = tmp[0];
            samples -= 1;
            continue;
        }
        o->sink.out = out;
        o->handler.Generate(&o->sink, n);
        out += n;
        samples -= n;
    }
}

HWOPL_API void hwopl_destroy(void *h) {
    delete (Opl *)h;
}

HWOPL_API int hwopl_version(void) { return 1; }
