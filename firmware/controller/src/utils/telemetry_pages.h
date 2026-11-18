#ifndef TELEMETRY_PAGES_H
#define TELEMETRY_PAGES_H

#include <stdint.h>

// Only bytes 19..21 of the unchanged 24-byte CRC-protected status packet.
// 32 pages at 100 Hz = one coherent snapshot approximately every 320 ms.
// See software/docs/mcu-diagnostics.md for the versioned word layout.
static const int TELEMETRY_PAGE_BASE = 160;
static const int TELEMETRY_PAGE_COUNT = 32;
// Schema 2 changes timing maxima from since-boot to since-previous-snapshot.
static const int TELEMETRY_SCHEMA = 2;

struct TelemetryPages {
    uint16_t words[TELEMETRY_PAGE_COUNT] = {};
    uint8_t page = 0;
    uint16_t sequence = 0;

    void set32(uint8_t index, uint32_t value) {
        words[index] = uint16_t(value >> 16);
        words[index + 1] = uint16_t(value);
    }

    void begin(uint8_t patch) {
        ++sequence;
        words[0] = (TELEMETRY_SCHEMA << 8) | patch;
        words[1] = sequence;
        words[31] = sequence;
    }

    void write(uint8_t *status) {
        status[19] = uint8_t(TELEMETRY_PAGE_BASE + page);
        status[20] = uint8_t(words[page] >> 8);
        status[21] = uint8_t(words[page]);
        page = (page + 1) % TELEMETRY_PAGE_COUNT;
    }
};

#endif
