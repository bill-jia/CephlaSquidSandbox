"""Small operator UI for moving the X-Light emission wheel to extraction.

Run from the ``software`` directory with::

    python tools/xlight_filter_extraction.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


SOFTWARE_DIR = Path(__file__).resolve().parents[1]
if str(SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(SOFTWARE_DIR))

from control.serial_peripherals import XLight  # noqa: E402


DEFAULT_CONFIG_PATH = (
    SOFTWARE_DIR
    / "machine_configs"
    / "library"
    / "machine_config_Squid+_LDI_XLight_TucsenAries6506.yaml"
)


def load_xlight_config(path: Path) -> tuple[str, float, bool, dict[int, str]]:
    """Return the X-Light settings needed by this standalone tool."""
    with path.open("r", encoding="utf-8") as config_file:
        machine_config = yaml.safe_load(config_file)

    try:
        xlight = machine_config["devices"]["xlight"]
        serial_number = str(xlight["connection"]["serial_number"])
        config = xlight["config"]
        raw_positions = config["emission_filter_wheel"]["positions"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Missing X-Light configuration in {path}") from exc

    positions = {int(slot): str(name) for slot, name in raw_positions.items()}
    if not serial_number or serial_number == "None":
        raise ValueError(f"The X-Light serial number is not set in {path}")
    if not positions:
        raise ValueError(f"No X-Light emission filter positions are defined in {path}")

    return (
        serial_number,
        float(config.get("sleep_time_for_wheel", 0.25)),
        bool(config.get("validate_wheel_pos", False)),
        positions,
    )


class XLightExtractionWindow(QWidget):
    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        super().__init__()
        self.xlight: XLight | None = None
        self.config_path = config_path
        self.serial_number, self.sleep_time, self.validate, self.positions = load_xlight_config(config_path)

        self.setWindowTitle("X-Light filter extraction")
        self.setMinimumWidth(380)

        self.position_combo = QComboBox()
        for slot, name in sorted(self.positions.items()):
            self.position_combo.addItem(f"{slot}: {name}", slot)

        details = QFormLayout()
        details.addRow("X-Light serial number:", QLabel(self.serial_number))
        details.addRow("Emission filter:", self.position_combo)

        self.move_button = QPushButton("Move to extraction position")
        self.move_button.clicked.connect(self.move_selected_filter)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.addLayout(details)
        layout.addWidget(self.move_button)
        layout.addWidget(self.status_label)

    def connect_xlight(self) -> XLight:
        if self.xlight is None:
            self.status_label.setText("Connecting to X-Light...")
            QApplication.processEvents()
            self.xlight = XLight(
                SN=self.serial_number,
                sleep_time_for_wheel=self.sleep_time,
                validate_wheel_pos=self.validate,
                emission_filter_positions=len(self.positions),
            )
            if not self.xlight.has_emission_filters_wheel:
                self.disconnect_xlight()
                raise RuntimeError("The connected X-Light does not report an emission filter wheel")
        return self.xlight

    def disconnect_xlight(self) -> None:
        """Release the serial port without issuing unrelated motor commands.

        ``XLight.close()`` first tries to stop the spinning-disk motor. That is
        appropriate for the full microscope application, but it can wait for
        several serial retries and make this filter-only utility appear hung.
        This tool never starts the motor, so it only owns the serial connection.
        """
        xlight, self.xlight = self.xlight, None
        if xlight is None:
            return
        serial_connection = getattr(xlight, "serial_connection", None)
        serial_port = getattr(serial_connection, "serial", None)
        if serial_connection is not None and serial_port is not None:
            serial_connection.close()

    def move_selected_filter(self) -> None:
        slot = int(self.position_combo.currentData())
        self.move_button.setEnabled(False)
        try:
            xlight = self.connect_xlight()
            self.status_label.setText(f"Moving filter {slot} to extraction position...")
            QApplication.processEvents()
            xlight.set_emission_filter(slot, extraction=True, validate=True)
            self.status_label.setText(f"Filter {slot} is at its extraction position")
        except Exception as exc:
            self.status_label.setText("Move failed")
            QMessageBox.critical(self, "X-Light error", str(exc))
        finally:
            self.move_button.setEnabled(True)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        try:
            self.disconnect_xlight()
        finally:
            event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    try:
        window = XLightExtractionWindow()
    except Exception as exc:
        QMessageBox.critical(None, "X-Light configuration error", str(exc))
        return 1
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
