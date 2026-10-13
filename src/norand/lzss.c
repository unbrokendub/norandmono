/*
 * lzss.c -- see lzss.h
 *
 * Reconstruction of the secondary bootloader's firmware decompressor.
 * Verified against mono_V2.2.0r.syx: 280182 packed bytes decode to the
 * 479704 byte image the device flashes at 0x08100000.
 */

#include "lzss.h"

/* Walk the token stream to find where the literal pool starts.  The device
 * does this in the same way (0x080264AC..0x080264C6): one byte per token,
 * two for a match, stop on a zero first byte. */
static const uint8_t *find_pool(const uint8_t *tok, const uint8_t *end)
{
    const uint8_t *p = tok;

    while (p < end && *p != 0)
        p += (*p & 0x80) ? 2 : 1;
    if (p >= end)
        return 0;
    return p + 1;
}

size_t norand_lzss_declared_size(const uint8_t *src, size_t src_len)
{
    size_t size = 0;
    unsigned shift = 0;
    size_t p;

    if (src_len < 3 || src[0] != 0xCF || src[1] != 0x00)
        return 0;
    for (p = 2; p < src_len; p++) {
        size |= (size_t)(src[p] & 0x7F) << shift;
        shift += 7;
        if (!(src[p] & 0x80))
            break;
    }
    return size;
}

norand_lzss_err norand_lzss_decompress(const uint8_t *src, size_t src_len,
                                       uint8_t *dst, size_t dst_cap,
                                       size_t *out_len)
{
    const uint8_t *end = src + src_len;
    const uint8_t *tok = src;
    const uint8_t *pool;
    size_t declared = 0;
    size_t n = 0;

    if (src_len >= 2 && src[0] == 0xCF && src[1] == 0x00) {
        unsigned shift = 0;
        tok = src + 2;
        while (tok < end) {
            uint8_t b = *tok++;
            declared |= (size_t)(b & 0x7F) << shift;
            shift += 7;
            if (!(b & 0x80))
                break;
        }
    }

    pool = find_pool(tok, end);
    if (!pool)
        return NORAND_LZSS_ETRUNC;

    while (tok < end) {
        uint8_t t = *tok;

        if (t == 0)
            break;

        if (t & 0x80) {                         /* back reference */
            unsigned dist, len, i;
            size_t from;

            if (tok + 1 >= end)
                return NORAND_LZSS_ETRUNC;
            dist = ((unsigned)(t & 0x7F) << 3) | (tok[1] >> 5);
            len = (tok[1] & 0x1F) + 3;
            tok += 2;

            if (len + dist > n)
                return NORAND_LZSS_EBACKREF;
            if (n + len > dst_cap)
                return NORAND_LZSS_EOVERFLOW;
            from = n - len - dist;
            for (i = 0; i < len; i++)
                dst[n + i] = dst[from + i];
            n += len;
        } else if (t & 0x40) {                  /* literal run */
            unsigned len = (t & 0x3F) + 1, i;

            tok++;
            if (pool + len > end)
                return NORAND_LZSS_ETRUNC;
            if (n + len > dst_cap)
                return NORAND_LZSS_EOVERFLOW;
            for (i = 0; i < len; i++)
                dst[n + i] = pool[i];
            pool += len;
            n += len;
        } else {                                /* run of one byte */
            unsigned len = (unsigned)t + 2, i;
            uint8_t v;

            tok++;
            if (pool >= end)
                return NORAND_LZSS_ETRUNC;
            v = *pool++;
            if (n + len > dst_cap)
                return NORAND_LZSS_EOVERFLOW;
            for (i = 0; i < len; i++)
                dst[n + i] = v;
            n += len;
        }
    }

    if (declared && declared != n)
        return NORAND_LZSS_ESIZE;

    *out_len = n;
    return NORAND_LZSS_OK;
}
