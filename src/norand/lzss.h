/*
 * lzss.h -- Norand Mono firmware container codec.
 *
 * Reconstructed from the secondary bootloader (decoder @ 0x08026460, node
 * methods @ 0x08026010..0x080260C0).  The on-device decoder is a pull based
 * chain of heap allocated "node" objects; the algorithm below is the same
 * bit-for-bit, expressed as a straight loop.
 *
 * Stream layout
 * -------------
 *   [0xCF 0x00 <uleb128 uncompressed size>]   -- optional header
 *   <token> ... <token> 0x00                  -- token stream
 *   <literal pool>                            -- bytes consumed by the
 *                                                literal and RLE tokens
 *
 * Tokens
 * ------
 *   t == 0x00              end of stream
 *   t & 0x80               match, two bytes:
 *                            D = ((t & 0x7F) << 3) | (t1 >> 5)   0..1023
 *                            L = (t1 & 0x1F) + 3                 3..34
 *                          copy L bytes starting at out[len - L - D]
 *   (t & 0xC0) == 0x40     literal run of (t & 0x3F) + 1 bytes from the pool
 *   otherwise              RLE, (t + 2) copies of one pool byte    3..65
 *
 * The device keeps a 1057 byte ring buffer, which is exactly the largest
 * reachable back reference (L + D <= 34 + 1023).
 */

#ifndef NORAND_LZSS_H
#define NORAND_LZSS_H

#include <stddef.h>
#include <stdint.h>

#define NORAND_LZSS_WINDOW 1057
#define NORAND_LZSS_MIN_MATCH 3
#define NORAND_LZSS_MAX_MATCH 34
#define NORAND_LZSS_MAX_DIST 1023

typedef enum {
    NORAND_LZSS_OK = 0,
    NORAND_LZSS_ETRUNC,     /* input ended in the middle of a token   */
    NORAND_LZSS_EBACKREF,   /* back reference points before the start */
    NORAND_LZSS_EOVERFLOW,  /* output does not fit the caller buffer  */
    NORAND_LZSS_ESIZE       /* header size disagrees with the result  */
} norand_lzss_err;

/*
 * Decompress `src` (`src_len` bytes) into `dst` (`dst_cap` bytes).
 * On success *out_len holds the number of bytes produced.
 */
norand_lzss_err norand_lzss_decompress(const uint8_t *src, size_t src_len,
                                       uint8_t *dst, size_t dst_cap,
                                       size_t *out_len);

/*
 * Read the uncompressed size from the optional 0xCF header without
 * decompressing.  Returns 0 when the stream carries no size header.
 */
size_t norand_lzss_declared_size(const uint8_t *src, size_t src_len);

#endif /* NORAND_LZSS_H */
