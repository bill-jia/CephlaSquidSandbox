import time

import pytest
import control._def
import control.microcontroller
from control.microcontroller import Microcontroller
from control.microcontroller import CommandAborted, SimSerial
from control._def import CMD_EXECUTION_STATUS, CMD_SET
from control.firmware_sim_serial import FirmwareSimSerial

def get_test_micro() -> control.microcontroller.Microcontroller:
    return control.microcontroller.Microcontroller(
        serial_device=control.microcontroller.get_microcontroller_serial_device(simulated=True)
    )


def assert_pos_almost_equal(expected, actual):
    assert len(actual) == len(expected)
    for e, a in zip(expected, actual):
        assert a == pytest.approx(e)


def test_create_simulated_microcontroller():
    micro = get_test_micro()


def test_microcontroller_simulated_positions():
    micro = get_test_micro()

    micro.move_x_to_usteps(1000)
    micro.wait_till_operation_is_completed()
    micro.move_y_to_usteps(2000)
    micro.wait_till_operation_is_completed()
    micro.move_z_to_usteps(3000)
    micro.wait_till_operation_is_completed()
    micro.move_theta_usteps(4000)
    micro.wait_till_operation_is_completed()

    assert_pos_almost_equal((1000, 2000, 3000, 4000), micro.get_pos())

    micro.home_x()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 2000, 3000, 4000), micro.get_pos())

    micro.home_y()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 3000, 4000), micro.get_pos())

    micro.home_z()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 0, 4000), micro.get_pos())

    micro.home_theta()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 0, 0), micro.get_pos())

    micro.move_x_to_usteps(1000)
    micro.wait_till_operation_is_completed()
    micro.move_y_to_usteps(2000)
    micro.wait_till_operation_is_completed()
    micro.move_z_to_usteps(3000)
    micro.wait_till_operation_is_completed()
    micro.move_theta_usteps(4000)
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((1000, 2000, 3000, 4000), micro.get_pos())

    micro.move_x_usteps(1)
    micro.wait_till_operation_is_completed()
    micro.move_y_usteps(2)
    micro.wait_till_operation_is_completed()
    micro.move_z_usteps(3)
    micro.wait_till_operation_is_completed()
    micro.move_theta_usteps(4)
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((1001, 2002, 3003, 4004), micro.get_pos())

    micro.zero_x()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 2002, 3003, 4004), micro.get_pos())

    micro.zero_y()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 3003, 4004), micro.get_pos())

    micro.zero_z()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 0, 4004), micro.get_pos())

    micro.zero_theta()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 0, 0), micro.get_pos())

    micro.move_x_to_usteps(1000)
    micro.wait_till_operation_is_completed()
    micro.move_y_to_usteps(2000)
    micro.wait_till_operation_is_completed()
    micro.move_z_to_usteps(3000)
    micro.wait_till_operation_is_completed()
    # There's no move_theta_to_usteps.
    assert_pos_almost_equal((1000, 2000, 3000, 0), micro.get_pos())

    micro.home_xy()
    micro.wait_till_operation_is_completed()
    assert_pos_almost_equal((0, 0, 3000, 0), micro.get_pos())
    micro.close()


@pytest.mark.skip(
    reason="This is likely a bug, but I'm not sure yet.  Tracking in https://linear.app/cephla/issue/S-115/microcontroller-relative-and-absolute-position-sign-mismatch"
)
def test_microcontroller_absolute_and_relative_match():
    micro = get_test_micro()

    def wait():
        micro.wait_till_operation_is_completed()

    micro.home_x()
    wait()

    micro.home_y()
    wait()

    micro.home_z()
    wait()

    micro.home_theta()
    wait()

    # For all our axes, we'd expect that moving to an absolute position from zero brings us to that position.
    # Then doing a relative move of the negative of the absolute position should bring us back to zero.
    abs_position = 1234

    # X
    micro.move_x_to_usteps(abs_position)
    wait()
    assert_pos_almost_equal((abs_position, 0, 0, 0), micro.get_pos())

    micro.move_x_usteps(-abs_position)
    wait()
    assert_pos_almost_equal((0, 0, 0, 0), micro.get_pos())

    # Y
    micro.move_y_to_usteps(abs_position)
    wait()
    assert_pos_almost_equal((0, abs_position, 0, 0), micro.get_pos())

    micro.move_y_usteps(-abs_position)
    wait()
    assert_pos_almost_equal((0, 0, 0, 0), micro.get_pos())

    # Z
    micro.move_z_to_usteps(abs_position)
    wait()
    assert_pos_almost_equal((0, 0, abs_position, 0), micro.get_pos())

    micro.move_z_usteps(-abs_position)
    wait()
    assert_pos_almost_equal((0, 0, 0, 0), micro.get_pos())
    micro.close()


def test_microcontroller_reconnects_serial():
    micro = get_test_micro()
    serial = micro._serial

    def wait():
        micro.wait_till_operation_is_completed()

    some_pos = 1234
    micro.move_x_to_usteps(some_pos)
    wait()
    assert_pos_almost_equal((some_pos, 0, 0, 0), micro.get_pos())

    # Force closed, then make sure the microcontroller handles reconnecting.  Both in the write and read cases
    # For the read, sleep a bit first since we know we have a reader loop spinning that could blowup if reconnects
    # don't work properly.
    serial.close()

    time.sleep(1)
    micro.move_y_to_usteps(2 * some_pos)
    wait()
    assert_pos_almost_equal((some_pos, 2 * some_pos, 0, 0), micro.get_pos())

    serial.close()
    micro.move_z_usteps(3 * some_pos)
    wait()
    assert_pos_almost_equal((some_pos, 2 * some_pos, 3 * some_pos, 0), micro.get_pos())
    micro.close()


def test_home_directions():
    test_micro = get_test_micro()

    dirs = (
        control.microcontroller.HomingDirection.HOMING_DIRECTION_FORWARD,
        control.microcontroller.HomingDirection.HOMING_DIRECTION_BACKWARD,
    )

    home_methods = (test_micro.home_x, test_micro.home_y, test_micro.home_z, test_micro.home_w, test_micro.home_theta)

    def wait():
        test_micro.wait_till_operation_is_completed()

    for d in dirs:
        for hm in home_methods:
            hm(homing_direction=d)
            wait()
            assert test_micro.last_command[3] == d.value

    test_micro.close()


def test_payload_helpers():
    assert isinstance(Microcontroller._int_to_payload(10, 1), int)
    assert isinstance(Microcontroller._int_to_payload(1.1, 2), int)
    assert Microcontroller._int_to_payload(1.1, 2) == 1
    assert Microcontroller._int_to_payload(2**16 - 1, 2) == 2**16 - 1
    assert Microcontroller._int_to_payload(2**16, 2) == 2**16

    assert Microcontroller._payload_to_int([0x00, 0x00, 0x00, 0xFF, 0xFF], 5) == 2**16 - 1
    assert Microcontroller._payload_to_int([0xFF, 0xFF], 2) == -1


def test_set_trigger_mode():
    """Test set_trigger_mode sends correct command to firmware."""
    micro = get_test_micro()

    # Test EDGE mode (0)
    micro.set_trigger_mode(0)
    assert micro.last_command[1] == control._def.CMD_SET.SET_TRIGGER_MODE
    assert micro.last_command[2] == 0

    # Test LEVEL mode (1)
    micro.set_trigger_mode(1)
    assert micro.last_command[1] == control._def.CMD_SET.SET_TRIGGER_MODE
    assert micro.last_command[2] == 1

    # Test with HardwareTriggerMode enum values
    from control._def import HardwareTriggerMode

    micro.set_trigger_mode(HardwareTriggerMode.EDGE)
    assert micro.last_command[2] == 0

    micro.set_trigger_mode(HardwareTriggerMode.LEVEL)
    assert micro.last_command[2] == 1

    micro.close()


class MotionStatusSerial(SimSerial):
    """Emit firmware-style statuses without moving a rejected/pending axis."""

    status_command = None
    status = CMD_EXECUTION_STATUS.IN_PROGRESS

    def queue_status(self, command_id, status):
        with self._update_lock:
            self._queue_status(command_id, status)

    def _queue_status(self, command_id, status):
        self.execution_status = status
        self.response_buffer.extend(
            self.response_bytes_for(command_id, status, self.x, self.y, self.z, self.theta, False, False)
        )
        self._update_internal_state()

    def _respond_to(self, command):
        if command[1] == self.status_command:
            self._queue_status(command[0], self.status)
        else:
            super()._respond_to(command)


@pytest.fixture
def status_micro():
    serial = MotionStatusSerial()
    micro = Microcontroller(serial, reset_and_initialize=False)
    yield micro, serial
    micro.close()


def test_execution_error_survives_heartbeat_and_aborts_without_timeout(status_micro):
    micro, serial = status_micro
    serial.status_command = CMD_SET.MOVETO_W
    serial.status = CMD_EXECUTION_STATUS.CMD_EXECUTION_ERROR
    # Hold the receiver off until both the rejection and heartbeat are queued.
    with micro._cmd_lock:
        micro.move_w_to_usteps(123)
        command = micro.last_command
        micro.send_heartbeat()
        assert micro.last_command is command
    with pytest.raises(CommandAborted, match="CMD_EXECUTION_ERROR") as exc:
        micro.wait_till_operation_is_completed(timeout_limit_s=1)
    assert exc.value.command_id == command[0]
    assert exc.value.recoverable
    assert not micro.is_busy()
    assert serial.w == 0
    # Keepalive must not acknowledge or clear the latched failure either.
    micro.send_heartbeat()
    with pytest.raises(CommandAborted):
        micro.wait_till_operation_is_completed(timeout_limit_s=0.01)


def test_heartbeat_during_move_preserves_pending_command(status_micro):
    micro, serial = status_micro
    serial.status_command = CMD_SET.MOVE_X
    micro.move_x_usteps(50)
    command = micro.last_command
    timestamp = micro.last_command_send_timestamp
    for _ in range(3):
        micro.send_heartbeat()
    with pytest.raises(TimeoutError):
        micro.wait_till_operation_is_completed(timeout_limit_s=0.02)
    assert micro.last_command is command
    assert micro.last_command_send_timestamp == timestamp
    assert micro.retry == 0
    serial.queue_status(command[0], CMD_EXECUTION_STATUS.COMPLETED_WITHOUT_ERRORS)
    micro.wait_till_operation_is_completed(timeout_limit_s=1)


def test_error_from_other_command_does_not_abort_pending_move(status_micro):
    micro, serial = status_micro
    serial.status_command = CMD_SET.MOVE_X
    micro.move_x_usteps(50)
    serial.queue_status((micro.last_command[0] - 1) % 256, CMD_EXECUTION_STATUS.CMD_EXECUTION_ERROR)
    with pytest.raises(TimeoutError):
        micro.wait_till_operation_is_completed(timeout_limit_s=0.02)
    assert micro.last_command_aborted_error is None
    serial.queue_status(micro.last_command[0], CMD_EXECUTION_STATUS.COMPLETED_WITHOUT_ERRORS)
    micro.wait_till_operation_is_completed(timeout_limit_s=1)


def test_configuration_rejection_is_not_marked_for_resend(status_micro):
    micro, serial = status_micro
    serial.status_command = CMD_SET.CONFIGURE_STEPPER_DRIVER
    serial.status = CMD_EXECUTION_STATUS.CMD_EXECUTION_ERROR
    micro.configure_motor_driver(control._def.AXIS.X, 16, 1000, 0.5)
    with pytest.raises(CommandAborted) as exc:
        micro.wait_till_operation_is_completed(timeout_limit_s=1)
    assert not exc.value.recoverable


@pytest.mark.parametrize("simulator", [SimSerial, FirmwareSimSerial])
def test_absolute_wheel_moves_and_homing_in_simulators(simulator):
    serial = simulator()
    micro = Microcontroller(serial, reset_and_initialize=False)
    try:
        assert micro.firmware_version == (1, 5)
        micro.move_w_to_usteps(345)
        micro.wait_till_operation_is_completed()
        micro.move_w2_to_usteps(-678)
        micro.wait_till_operation_is_completed()
        assert (serial.w, serial.w2) == (345, -678)
        micro.home_w2()
        micro.wait_till_operation_is_completed()
        assert (serial.w, serial.w2) == (345, 0)
    finally:
        micro.close()
