#include <unity.h>
#include <string.h>
#include "utils/telemetry_pages.h"
#include "utils/interval_timing.h"
#include "utils/crc8.cpp"

void setUp(void) {}
void tearDown(void) {}

void test_telemetry_preserves_status_and_crc_layout() {
    TelemetryPages pages;
    pages.begin(1);
    pages.set32(10, uint32_t(-1000));
    uint8_t packet[24];
    memset(packet, 0x55, sizeof(packet));
    for (unsigned page = 0; page < 32; ++page) {
        pages.write(packet);
        TEST_ASSERT_EQUAL_UINT8(160 + page, packet[19]);
        TEST_ASSERT_EQUAL_UINT16(pages.words[page], (uint16_t(packet[20]) << 8) | packet[21]);
        for (unsigned i = 0; i < 19; ++i) TEST_ASSERT_EQUAL_UINT8(0x55, packet[i]);
        TEST_ASSERT_EQUAL_UINT8(0x55, packet[22]);
        packet[23] = crc8ccitt(packet, 23);
        TEST_ASSERT_EQUAL_UINT8(crc8ccitt(packet, 23), packet[23]);
    }
    TEST_ASSERT_EQUAL_UINT8(0, pages.page);
    TEST_ASSERT_EQUAL_UINT16(0x0201, pages.words[0]);
    TEST_ASSERT_EQUAL_UINT16(pages.words[1], pages.words[31]);
    TEST_ASSERT_EQUAL_UINT16(0xFFFF, pages.words[10]);
    TEST_ASSERT_EQUAL_UINT16(0xFC18, pages.words[11]);
}

void test_telemetry_sequence_wrap() {
    TelemetryPages pages;
    pages.sequence = 65535;
    pages.begin(1);
    TEST_ASSERT_EQUAL_UINT16(0, pages.words[1]);
    TEST_ASSERT_EQUAL_UINT16(0, pages.words[31]);
    pages.begin(1);
    TEST_ASSERT_EQUAL_UINT16(1, pages.words[1]);
    TEST_ASSERT_EQUAL_UINT16(1, pages.words[31]);
}

void test_interval_maximum_resets_but_preserves_cross_boundary_gap() {
    IntervalTiming timer;
    timer.observe(0);
    timer.observe(1000); // slow initialization interval
    TEST_ASSERT_EQUAL_UINT32(1000, timer.take_maximum());
    timer.observe(1008);
    timer.observe(1010);
    TEST_ASSERT_EQUAL_UINT32(8, timer.take_maximum());
    TEST_ASSERT_EQUAL_UINT32(0, timer.take_maximum());
    timer.observe(1020);
    TEST_ASSERT_EQUAL_UINT32(10, timer.take_maximum());
}

void test_interval_maximum_handles_timestamp_rollover() {
    IntervalTiming timer;
    timer.observe(0xFFFFFFF0u);
    timer.observe(0x00000010u);
    TEST_ASSERT_EQUAL_UINT32(32, timer.take_maximum());
}

int main(int argc, char **argv) {
    UNITY_BEGIN();
    RUN_TEST(test_telemetry_preserves_status_and_crc_layout);
    RUN_TEST(test_telemetry_sequence_wrap);
    RUN_TEST(test_interval_maximum_resets_but_preserves_cross_boundary_gap);
    RUN_TEST(test_interval_maximum_handles_timestamp_rollover);
    return UNITY_END();
}
