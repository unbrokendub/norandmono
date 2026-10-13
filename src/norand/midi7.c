/* midi7.c -- see midi7.h */

#include "midi7.h"

size_t norand_midi7_decode(const uint8_t *src, size_t len, uint8_t *dst)
{
    size_t i = 0, n = 0;

    while (i < len) {
        uint8_t msb = src[i++];
        unsigned k;

        for (k = 0; k < 7 && i < len; k++) {
            uint8_t b = src[i++];
            dst[n++] = (uint8_t)(b | ((msb & (1u << (6 - k))) ? 0x80 : 0));
        }
    }
    return n;
}

size_t norand_midi7_encode(const uint8_t *src, size_t len, uint8_t *dst)
{
    size_t i = 0, n = 0;

    while (i < len) {
        size_t grp = len - i < 7 ? len - i : 7;
        size_t msb_at = n++;
        uint8_t msb = 0;
        unsigned k;

        for (k = 0; k < grp; k++) {
            uint8_t b = src[i + k];

            if (b & 0x80)
                msb |= 1u << (6 - k);
            dst[n++] = (uint8_t)(b & 0x7F);
        }
        dst[msb_at] = msb;
        i += grp;
    }
    return n;
}
