#include "diagnostics.h"
#include "globals.h"
#include "utils/telemetry_pages.h"
#include "utils/interval_timing.h"

namespace {
TelemetryPages telemetry;
uint32_t loop_count = 0;
IntervalTiming loop_timing;
uint32_t valid_commands = 0;
uint32_t crc_errors = 0;
IntervalTiming status_timing;
uint32_t last_z_command_ms = 0;
uint16_t last_z_command = 0xFFFF;
int16_t last_z_result = -32768; // no Z command yet; -32767 = callback not returned
}

void diagnostics_loop_tick() {
    loop_timing.observe(micros());
    ++loop_count;
}

void diagnostics_command(bool crc_valid) {
    if (!crc_valid) {
        ++crc_errors;
        return;
    }
    ++valid_commands;
    if (buffer_rx[1] == MOVE_Z || buffer_rx[1] == MOVETO_Z) {
        last_z_command = (uint16_t(buffer_rx[0]) << 8) | buffer_rx[1];
        last_z_command_ms = millis();
        last_z_result = -32767;
    }
}

void diagnostics_z_move_result(int16_t result) {
    last_z_result = result;
}

void diagnostics_status(uint8_t *packet) {
    const uint32_t now = millis();
    status_timing.observe(now);
    if (telemetry.page == 0) {
        telemetry.begin(FIRMWARE_VERSION_PATCH);
        telemetry.set32(2, now);
        telemetry.set32(4, loop_count);
        telemetry.set32(6, loop_timing.take_maximum());
        telemetry.set32(8, valid_commands);
        telemetry.set32(10, uint32_t(Z_commanded_target_position));
        // Read only non-destructive registers. Never read EVENTS for diagnostics:
        // reading it would consume the limit events used by motion control.
        telemetry.set32(12, uint32_t(tmc4361A_currentPosition(&tmc4361[z])));
        telemetry.set32(14, uint32_t(focusPosition));
        telemetry.set32(16, uint32_t(tmc4361A_readInt(&tmc4361[z], TMC4361A_STATUS)));
        telemetry.set32(18, uint32_t(tmc4361A_readInt(&tmc4361[z], TMC4361A_PID_E_RD)));
        telemetry.words[20] = uint16_t(mcu_cmd_execution_in_progress)
            | (uint16_t(X_commanded_movement_in_progress) << 1)
            | (uint16_t(Y_commanded_movement_in_progress) << 2)
            | (uint16_t(Z_commanded_movement_in_progress) << 3)
            | (uint16_t(W_commanded_movement_in_progress) << 4)
            | (uint16_t(W2_commanded_movement_in_progress) << 5)
            | (uint16_t(is_homing_Z) << 6)
            | (uint16_t(is_preparing_for_homing_Z) << 7)
            | (uint16_t(stage_PID_enabled[z]) << 8)
            | (uint16_t(watchdog_enabled) << 9);
        telemetry.words[21] = uint16_t(tmc4361[x].driver_type)
            | (uint16_t(tmc4361[y].driver_type) << 2)
            | (uint16_t(tmc4361[z].driver_type) << 4)
            | (uint16_t(tmc4361[w].driver_type) << 6)
            | (uint16_t(tmc4361[w2].driver_type) << 8);
        telemetry.words[22] = last_z_command;
        telemetry.words[23] = uint16_t(last_z_result);
        telemetry.set32(24, now - last_z_command_ms);
        telemetry.set32(26, tmc4361[z].target_tolerance);
        telemetry.set32(28, crc_errors);
        const uint32_t status_gap = status_timing.take_maximum();
        telemetry.words[30] = uint16_t(status_gap > 65535 ? 65535 : status_gap);
    }
    telemetry.write(packet);
}
