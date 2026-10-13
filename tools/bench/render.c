/*
 * render.c -- a digital stand-in for Mono's analog voice, for listening to
 * what the firmware asks of it.
 *
 * Input: the control values the DSP publishes every sample (engine+0x2EB4
 * .. +0x2F2C, captured in sysemu by tools/bench/bench.py), 31 float32 per
 * frame at 32 kHz.  Output: a 16-bit mono WAV at 32 kHz.
 *
 * It is a model, not a measurement: the shapes, the TZFM depth, the filter
 * and the drive are plausible analog behaviour, not Mono's circuits.  What
 * it gets right is everything the firmware decides -- pitches, levels,
 * waveform mixes, the cutoff, the envelopes and every modulation on them.
 *
 * Frame layout (float index = (offset - 0x2EB4) / 4):
 *   0..3   VCO1 saw, square, sine, triangle level CVs (exponential)
 *   4..7   VCO2 square, saw, triangle, sine level CVs (with its volume)
 *   8      TZFM depth CV (exponential)
 *   9      VCO1 volume CV (exponential)
 *   10, 11 VCO1, VCO2 pitch (semitones, MIDI)
 *   12     cutoff (semitones, 81 = 440 Hz)
 *   13     output volume CV (exponential)
 *   14     resonance 0..1
 *   15     colour 0..1 (band-pass, low-pass, high-pass; all-pass between)
 *   30     amp envelope 0..1
 * The exponential CVs read 1.0 at unity and lose 0.0502 per halving
 * (measured: a knob at 1/2 gives 0.9498, at 1/16 0.7993), 0 is off.
 *
 * usage: render in.f32 out.wav [sync 0|1]
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FS 32000.0
#define OS 4
#define NCV 31

static double expo(double cv) { return cv <= 0.01 ? 0.0 : pow(2.0, (cv - 1.0) / 0.0502); }
static double midi_hz(double m) { return 440.0 * pow(2.0, (m - 69.0) / 12.0); }

/* polyBLEP residual for a discontinuity at phase 0 */
static double blep(double t, double dt)
{
    if (dt <= 0)
        return 0;
    if (t < dt) {
        t /= dt;
        return t + t - t * t - 1.0;
    }
    if (t > 1.0 - dt) {
        t = (t - 1.0) / dt;
        return t * t + t + t + 1.0;
    }
    return 0;
}

/* the four shapes at phase ph (0..1), increment dt (for the BLEPs) */
static void shapes(double ph, double dt, double *sine, double *tri, double *sq, double *saw)
{
    double adt = fabs(dt);
    *sine = sin(2 * M_PI * ph);
    *tri = ph < 0.5 ? 4 * ph - 1 : 3 - 4 * ph;
    double s = 2 * ph - 1;
    s -= blep(ph, adt);
    *saw = s;
    double q = ph < 0.5 ? 1 : -1;
    q += blep(ph, adt);
    double p2 = ph + 0.5;
    if (p2 >= 1)
        p2 -= 1;
    q -= blep(p2, adt);
    *sq = q;
}

typedef struct { double ic1, ic2, lp1, hp1, lpb; } Svf;

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: render in.f32 out.wav [sync]\n");
        return 2;
    }
    const int sync = argc > 3 && atoi(argv[3]);
    FILE *f = fopen(argv[1], "rb");
    if (!f)
        return 1;
    fseek(f, 0, SEEK_END);
    long n = ftell(f) / (NCV * 4);
    fseek(f, 0, SEEK_SET);
    float *cv = malloc(n * NCV * 4);
    if (fread(cv, 4, n * NCV, f) != (size_t)(n * NCV))
        return 1;
    fclose(f);

    double *out = calloc(n, sizeof(double));
    const double fs = FS * OS;
    double ph1 = 0, ph2 = 0;
    Svf sv = {0};
    /* a 2-pole low-pass at 13 kHz before taking every OS-th sample */
    const double aa_w = tan(M_PI * 13000.0 / fs), aa_k = 1.41421356;
    double aa1 = 0, aa2 = 0, dcx = 0, dco = 0;
    double peak = 1e-9;

    for (long i = 0; i < n; ++i) {
        const float *a = cv + i * NCV;
        const float *b = cv + (i + 1 < n ? i + 1 : i) * NCV;
        double acc = 0;
        for (int o = 0; o < OS; ++o) {
            const double t = (double)o / OS;
#define L(k) (a[k] + (b[k] - a[k]) * t)
            const double f1 = midi_hz(L(10)), f2 = midi_hz(L(11));
            const double dt1 = f1 / fs;
            double s, tr, q, sw;
            shapes(ph1, dt1, &s, &tr, &q, &sw);
            /* VCO1's shape (what TZFM uses) and its volume into the mix */
            const double v1 = expo(L(0)) * sw + expo(L(1)) * q + expo(L(2)) * s + expo(L(3)) * tr;
            const double tz = expo(L(8));
            /* thru-zero linear FM of VCO2 by VCO1, up to 4x its frequency */
            const double dt2 = f2 * (1.0 + 4.0 * tz * v1) / fs;
            double s2, tr2, q2, sw2;
            shapes(ph2, dt2, &s2, &tr2, &q2, &sw2);
            const double v2 = expo(L(4)) * q2 + expo(L(5)) * sw2 + expo(L(6)) * tr2 + expo(L(7)) * s2;
            double x = expo(L(9)) * v1 + v2;
            /* the mixer drives the filter: the negative half clips first */
            x *= 1.6;
            x = x < 0 ? tanh(x) : x / (1.0 + 0.25 * x);
            ph1 += dt1;
            int wrapped = 0;
            while (ph1 >= 1) {
                ph1 -= 1;
                wrapped = 1;
            }
            ph2 += dt2;
            while (ph2 >= 1)
                ph2 -= 1;
            while (ph2 < 0)
                ph2 += 1;
            if (sync && wrapped)
                ph2 = ph1 * (dt1 > 0 ? dt2 / dt1 : 0);

            /* 18 dB filter: a resonant TPT state-variable stage and a pole */
            double fc = 440.0 * pow(2.0, (L(12) - 81.0) / 12.0);
            if (fc < 15)
                fc = 15;
            if (fc > 0.42 * fs)
                fc = 0.42 * fs;
            const double g = tan(M_PI * fc / fs);
            double r = L(14);
            if (r < 0)
                r = 0;
            if (r > 1)
                r = 1;
            const double k = 2.0 * pow(1.0 - r, 1.3) + 0.012;
            const double hp = (x - (k + g) * sv.ic1 - sv.ic2) / (1 + g * (k + g));
            const double v1s = g * hp, bp = v1s + sv.ic1;
            sv.ic1 = tanh(bp + v1s);          /* the integrator saturates: rings, never explodes */
            const double v2s = g * bp, lp = v2s + sv.ic2;
            sv.ic2 = lp + v2s;
            const double gp = g / (1 + g);
            const double lpv = gp * (lp - sv.lp1);
            const double lp3 = lpv + sv.lp1;
            sv.lp1 = lp3 + lpv;
            const double hpv = gp * (hp - sv.hp1);
            const double hpl = hpv + sv.hp1;
            sv.hp1 = hpl + hpv;
            const double hp3 = hp - hpl;
            double c = L(15);
            if (c < 0)
                c = 0;
            if (c > 1)
                c = 1;
            double y;
            if (c < 0.5) {
                const double u = 2 * c;
                y = (1 - u) * bp * k + u * lp3;
            } else {
                const double u = 2 * c - 1;
                y = (1 - u) * lp3 + u * hp3 - sin(M_PI * u) * k * bp;
            }
            /* the VCA: output volume times the amp envelope */
            y *= expo(L(13)) * L(30);
            /* a DC blocker (~10 Hz): the drive's asymmetric clip leaves an offset */
            const double dcy = y - dcx + 0.99951 * dco;
            dcx = y;
            dco = dcy;
            y = dcy;
#undef L
            /* anti-alias and decimate */
            const double v0 = y;
            const double h = (v0 - (aa_k + aa_w) * aa1 - aa2) / (1 + aa_w * (aa_k + aa_w));
            const double bv = aa_w * h, bb = bv + aa1;
            aa1 = bb + bv;
            const double lv = aa_w * bb, ll = lv + aa2;
            aa2 = ll + lv;
            acc = ll;
        }
        out[i] = acc;
        if (fabs(acc) > peak)
            peak = fabs(acc);
    }

    FILE *w = fopen(argv[2], "wb");
    if (!w)
        return 1;
    const uint32_t rate = (uint32_t)FS, bytes = (uint32_t)(n * 2);
    uint8_t hdr[44];
    memcpy(hdr, "RIFF", 4);
    uint32_t v = 36 + bytes;
    memcpy(hdr + 4, &v, 4);
    memcpy(hdr + 8, "WAVEfmt ", 8);
    v = 16;
    memcpy(hdr + 16, &v, 4);
    uint16_t s16 = 1;
    memcpy(hdr + 20, &s16, 2);
    memcpy(hdr + 22, &s16, 2);
    memcpy(hdr + 24, &rate, 4);
    v = rate * 2;
    memcpy(hdr + 28, &v, 4);
    s16 = 2;
    memcpy(hdr + 32, &s16, 2);
    s16 = 16;
    memcpy(hdr + 34, &s16, 2);
    memcpy(hdr + 36, "data", 4);
    memcpy(hdr + 40, &bytes, 4);
    fwrite(hdr, 1, 44, w);
    const double gain = 0.89 / peak;          /* -1 dBFS: previews, not levels */
    for (long i = 0; i < n; ++i) {
        double y = out[i] * gain;
        int16_t q = (int16_t)lrint(y * 32767.0);
        fwrite(&q, 2, 1, w);
    }
    fclose(w);
    printf("%ld frames, peak %.4f\n", n, peak);
    return 0;
}
