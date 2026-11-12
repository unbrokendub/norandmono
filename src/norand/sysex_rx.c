/* sysex_rx.c -- see sysex_rx.h */

#include "sysex_rx.h"
#include "midi7.h"

#include <string.h>

/* Table at 0x08027720.  Only the first NORAND_SYSEX_ID_LEN bytes are
 * compared; the remaining five are the packed per-message header and are
 * allowed to differ. */
const uint8_t norand_sysex_header[NORAND_SYSEX_HDR_LEN] = {
    0xF0, 0x00, 0x23, 0x50, 0x00, 0x02, 0x00, 0x00, 0x11, 0x03
};

static uint16_t bswap16(uint16_t v)
{
    return (uint16_t)((v >> 8) | (v << 8));
}

void norand_sysex_rx_init(norand_sysex_rx *rx, uint8_t *buf)
{
    memset(rx, 0, sizeof(*rx));
    rx->buf = buf;
}

static void reset_header(norand_sysex_rx *rx)
{
    rx->hdr_pos = 0;
    rx->hdr_bad = 0;
    memset(rx->hdr, 0, sizeof(rx->hdr));
}

/* One completed group of 8 transmitted bytes -> 7 payload bytes. */
static void flush_group(norand_sysex_rx *rx)
{
    uint8_t tmp[7];

    norand_midi7_decode(rx->group, rx->group_len, tmp);
    memcpy(rx->buf + rx->off, tmp, 7);
    rx->group_len = 0;
    /* The device advances by a full 7 even for a short group; stock
     * senders never produce one, see NORAND_SYSEX_ASSUMED_CHUNK. */
    rx->off += 7;
}

static void handle_header(norand_sysex_rx *rx)
{
    uint8_t packed[4];
    uint8_t hdr[3];

    memcpy(packed, rx->hdr + 6, 4);
    norand_midi7_decode(packed, 4, hdr);

    if (rx->total == 0) {
        rx->index = 0;
        rx->total = bswap16((uint16_t)(hdr[0] | (hdr[1] << 8)));
        rx->tail = hdr[2];
        rx->declared_size = (uint32_t)rx->tail +
                            (uint32_t)rx->total * NORAND_SYSEX_ASSUMED_CHUNK;
        /* the device aborts here when the image cannot fit bank 2 */
    } else {
        rx->index = bswap16((uint16_t)(hdr[0] | (hdr[1] << 8)));
    }

    rx->state = NORAND_SYSEX_RECEIVE_DATA;
    rx->hdr_pos = 0;
}

void norand_sysex_rx_byte(norand_sysex_rx *rx, uint8_t byte,
                          const norand_sysex_cb *cb)
{
    if (rx->state == NORAND_SYSEX_MATCH_HEADER) {
        uint32_t pos;

        if (byte > 0xF0) {          /* real time / stray F7: start over */
            reset_header(rx);
            return;
        }
        pos = rx->hdr_pos;
        /* Not in the stock: 0x08026B48 stores past the 10-byte header with
         * no bound, so a long foreign SysEx overwrites the write offset and
         * the buffer pointer that follow it (docs/protocol.md, section 8). */
        if (pos >= NORAND_SYSEX_HDR_LEN)
            return;
        rx->hdr[pos] = byte;
        rx->hdr_pos = ++pos;

        if (norand_sysex_header[pos - 1] != byte && pos <= NORAND_SYSEX_ID_LEN) {
            rx->hdr_bad = 1;
            return;
        }
        if (pos == NORAND_SYSEX_HDR_LEN && !rx->hdr_bad)
            handle_header(rx);
        return;
    }

    /* NORAND_SYSEX_RECEIVE_DATA */
    if (byte & 0x80) {
        if (byte != 0xF7)
            return;

        if (cb && cb->progress)
            cb->progress(cb->user, rx->index, rx->total);
        rx->state = NORAND_SYSEX_MATCH_HEADER;

        if (rx->index == rx->total && cb && cb->complete)
            cb->complete(cb->user, rx->buf, rx->off);
        return;
    }

    rx->group[rx->group_len++] = byte;
    if (rx->group_len == 8) {
        flush_group(rx);
        return;
    }
    /* the last message of a transfer may end on a short group */
    if (rx->index == rx->total && rx->tail == rx->group_len)
        flush_group(rx);
}
