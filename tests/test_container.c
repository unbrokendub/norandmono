/*
 * test_container.c -- feed a stock .syx through the reconstructed receiver
 * and decompressor and compare the result with the reference image.
 *
 *   ./test_container mono_V2.2.0r.syx build/firmware.bin
 */

#include "../src/norand/sysex_rx.h"
#include "../src/norand/lzss.h"
#include "../src/norand/midi7.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint8_t *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    uint8_t *p;

    if (!f) {
        perror(path);
        exit(1);
    }
    fseek(f, 0, SEEK_END);
    *len = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    p = malloc(*len);
    if (fread(p, 1, *len, f) != *len) {
        perror(path);
        exit(1);
    }
    fclose(f);
    return p;
}

struct ctx {
    int done;
    uint32_t packed_len;
};

static void on_progress(void *user, uint16_t index, uint16_t total)
{
    (void)user;
    if (index && index % 500 == 0)
        printf("  ... packet %u/%u\n", index, total);
}

static void on_complete(void *user, uint8_t *buf, uint32_t len)
{
    struct ctx *c = user;

    (void)buf;
    c->done = 1;
    c->packed_len = len;
}

int main(int argc, char **argv)
{
    size_t syx_len, ref_len, out_len = 0;
    uint8_t *syx, *ref, *packed, *out;
    norand_sysex_rx rx;
    norand_sysex_cb cb;
    struct ctx c = { 0, 0 };
    size_t i;
    int rc;

    if (argc < 3) {
        fprintf(stderr, "usage: %s <in.syx> <reference.bin>\n", argv[0]);
        return 2;
    }
    syx = slurp(argv[1], &syx_len);
    ref = slurp(argv[2], &ref_len);

    packed = malloc(NORAND_SYSEX_MAX_IMAGE);
    out = malloc(NORAND_SYSEX_MAX_IMAGE);

    cb.progress = on_progress;
    cb.complete = on_complete;
    cb.user = &c;
    norand_sysex_rx_init(&rx, packed);

    for (i = 0; i < syx_len; i++)
        norand_sysex_rx_byte(&rx, syx[i], &cb);

    if (!c.done) {
        fprintf(stderr, "FAIL: transfer never completed\n");
        return 1;
    }
    printf("received  : %u packed bytes in %u packets\n",
           c.packed_len, rx.total);

    rc = norand_lzss_decompress(packed, c.packed_len, out,
                                NORAND_SYSEX_MAX_IMAGE, &out_len);
    if (rc != NORAND_LZSS_OK) {
        fprintf(stderr, "FAIL: decompress error %d\n", rc);
        return 1;
    }
    printf("decoded   : %zu bytes (declared %zu)\n", out_len,
           norand_lzss_declared_size(packed, c.packed_len));

    if (out_len != ref_len || memcmp(out, ref, ref_len)) {
        fprintf(stderr, "FAIL: image differs from %s\n", argv[2]);
        return 1;
    }
    printf("image     : identical to %s\n", argv[2]);

    /* round trip the 7 bit transport as well */
    {
        uint8_t enc[64], dec[64];
        uint8_t src[35];
        size_t n, m;

        for (i = 0; i < sizeof(src); i++)
            src[i] = (uint8_t)(i * 7 + 0x80);
        n = norand_midi7_encode(src, sizeof(src), enc);
        m = norand_midi7_decode(enc, n, dec);
        if (m != sizeof(src) || memcmp(src, dec, m)) {
            fprintf(stderr, "FAIL: midi7 round trip\n");
            return 1;
        }
        printf("midi7     : round trip ok (%zu -> %zu -> %zu)\n",
               sizeof(src), n, m);
    }

    printf("PASS\n");
    return 0;
}
