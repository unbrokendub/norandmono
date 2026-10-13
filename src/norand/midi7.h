/*
 * midi7.h -- MIDI 7 bit transport used by the Norand SysEx updater.
 *
 * Reconstructed from the bootloader routine at 0x08026F24 (the hand
 * optimised version there decodes four bytes at a time with 0x80808080
 * masks; the bit assignment below is what it computes).
 *
 * Every group of 8 transmitted septets carries 7 octets.  The first byte of
 * a group holds the high bits of the seven that follow, most significant
 * first: bit6 belongs to the first byte, bit0 to the seventh.
 */

#ifndef NORAND_MIDI7_H
#define NORAND_MIDI7_H

#include <stddef.h>
#include <stdint.h>

/* Number of transmitted bytes needed for `n` payload bytes, and vice versa. */
#define NORAND_MIDI7_ENC_LEN(n) (((n) + 6) / 7 * 8 - (((7 - (n) % 7) % 7)))
#define NORAND_MIDI7_DEC_LEN(n) ((n) / 8 * 7 + ((n) % 8 ? (n) % 8 - 1 : 0))

/* Decode `len` transmitted bytes into `dst`; returns bytes written. */
size_t norand_midi7_decode(const uint8_t *src, size_t len, uint8_t *dst);

/* Encode `len` payload bytes into `dst`; returns bytes written. */
size_t norand_midi7_encode(const uint8_t *src, size_t len, uint8_t *dst);

#endif /* NORAND_MIDI7_H */
