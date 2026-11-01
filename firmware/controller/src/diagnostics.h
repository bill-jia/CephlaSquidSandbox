#ifndef MCU_DIAGNOSTICS_H
#define MCU_DIAGNOSTICS_H

#include <stdint.h>

void diagnostics_loop_tick();
void diagnostics_command(bool crc_valid);
void diagnostics_z_move_result(int16_t result);
void diagnostics_status(uint8_t *packet);

#endif
