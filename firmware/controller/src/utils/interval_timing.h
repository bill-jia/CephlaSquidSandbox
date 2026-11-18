#ifndef INTERVAL_TIMING_H
#define INTERVAL_TIMING_H

#include <stdint.h>

// Single-writer main-loop timing. Resetting the maximum keeps the previous
// timestamp, so a gap crossing a snapshot boundary belongs to the next window.
struct IntervalTiming {
    uint32_t previous = 0;
    uint32_t maximum = 0;
    bool initialized = false;

    void observe(uint32_t now) {
        if (initialized) {
            const uint32_t gap = now - previous; // unsigned rollover is intentional
            if (gap > maximum) maximum = gap;
        }
        previous = now;
        initialized = true;
    }

    uint32_t take_maximum() {
        const uint32_t result = maximum;
        maximum = 0;
        return result;
    }
};

#endif
