/*
 * sysex_rx.h -- Norand Mono firmware-update SysEx receiver.
 *
 * Reconstructed from the secondary bootloader (0x08026B00, called per MIDI
 * byte from the USB/DIN demux at 0x08026C80).
 *
 * Wire format, one message per 98 payload bytes:
 *
 *   F0 00 23 50 00 02 <h0 h1 h2 h3> <112 septets> F7
 *
 * h0..h3 are four transmitted bytes that decode (7 bit transport) to three
 * payload bytes:
 *
 *   [0..1] packet index, big endian
 *   [2]    trailing length field (0x16 in every stock file)
 *
 * The very first message carries the *total* packet count in the index
 * field instead of an index, and its payload is the first data chunk.
 * Messages 1..total follow in order.  When the index of a completed
 * message equals the total, the accumulated buffer is handed to the
 * decompressor and flashed.
 */

#ifndef NORAND_SYSEX_RX_H
#define NORAND_SYSEX_RX_H

#include <stdint.h>
#include <stddef.h>

#define NORAND_SYSEX_HDR_LEN 10
#define NORAND_SYSEX_ID_LEN 5      /* bytes that must match exactly */
#define NORAND_SYSEX_MAX_IMAGE 0x80000

/* Bytes of decoded payload the sender puts in one message.  The stock
 * bootloader assumes 112 when it estimates the image size (7 * 16); the
 * V2.2.0 updater actually sends 98 (7 * 14).  The estimate is only used
 * for the progress bar, so the mismatch is harmless. */
#define NORAND_SYSEX_ASSUMED_CHUNK 112

typedef enum {
    NORAND_SYSEX_MATCH_HEADER = 0,
    NORAND_SYSEX_RECEIVE_DATA = 1
} norand_sysex_state;

typedef struct {
    /* transfer state */
    norand_sysex_state state;
    uint8_t  hdr[NORAND_SYSEX_HDR_LEN];
    uint32_t hdr_pos;
    uint8_t  hdr_bad;

    /* 7 bit transport regrouping */
    uint8_t  group[8];
    uint8_t  group_len;

    /* progress */
    uint16_t total;          /* packet count from the first message */
    uint16_t index;          /* index of the message being received */
    uint8_t  tail;           /* third header byte                   */
    uint32_t declared_size;  /* total * 112 + tail                  */

    /* destination */
    uint8_t *buf;
    uint32_t off;
} norand_sysex_rx;

/* Hooks the caller supplies; both may be NULL. */
typedef struct {
    void (*progress)(void *user, uint16_t index, uint16_t total);
    void (*complete)(void *user, uint8_t *buf, uint32_t len);
    void *user;
} norand_sysex_cb;

void norand_sysex_rx_init(norand_sysex_rx *rx, uint8_t *buf);
void norand_sysex_rx_byte(norand_sysex_rx *rx, uint8_t byte,
                          const norand_sysex_cb *cb);

extern const uint8_t norand_sysex_header[NORAND_SYSEX_HDR_LEN];

#endif /* NORAND_SYSEX_RX_H */
