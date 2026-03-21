"""Slack notification settings dialog for the Squid GUI."""

import os
from typing import Optional

import yaml

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import control._def
import squid.logging
from control.slack_notifier import SlackNotifier

log = squid.logging.get_logger(__name__)

SLACK_CACHE_FILE = "cache/slack_settings.yaml"

_SETTINGS_MAP = {
    "enabled": ("ENABLED", lambda: False),
    "bot_token": ("BOT_TOKEN", lambda: None),
    "channel_id": ("CHANNEL_ID", lambda: None),
    "notify_on_error": ("NOTIFY_ON_ERROR", lambda: True),
    "notify_on_timepoint_complete": ("NOTIFY_ON_TIMEPOINT_COMPLETE", lambda: True),
    "send_mosaic_snapshots": ("SEND_MOSAIC_SNAPSHOTS", lambda: True),
    "notify_on_acquisition_start": ("NOTIFY_ON_ACQUISITION_START", lambda: True),
    "notify_on_acquisition_finished": ("NOTIFY_ON_ACQUISITION_FINISHED", lambda: True),
}


def _load_slack_cached_settings() -> dict:
    if not os.path.exists(SLACK_CACHE_FILE):
        return {}
    try:
        with open(SLACK_CACHE_FILE, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log.warning(f"Failed to load Slack settings from cache: {e}")
        return {}


class SlackSettingsDialog(QDialog):
    """Non-modal dialog for configuring Slack notifications.

    Settings are saved to a cache file and override INI config values
    at runtime. Changes take effect immediately.
    """

    settings_changed = Signal()

    def __init__(
        self,
        slack_notifier: Optional[SlackNotifier] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._slack_notifier = slack_notifier
        self.setWindowTitle("Slack Notifications")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setModal(False)
        self.setMinimumWidth(500)

        self._setup_ui()
        self._load_settings()
        self._connect_signals()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        api_group = QGroupBox("Slack API Configuration")
        api_layout = QFormLayout()

        self.checkbox_enabled = QCheckBox("Enable Slack Notifications")
        api_layout.addRow(self.checkbox_enabled)

        self.lineedit_bot_token = QLineEdit()
        self.lineedit_bot_token.setPlaceholderText("xoxb-...")
        self.lineedit_bot_token.setEchoMode(QLineEdit.Password)
        api_layout.addRow("Bot Token:", self.lineedit_bot_token)

        token_buttons = QHBoxLayout()
        self.btn_show_token = QPushButton("Show")
        self.btn_show_token.setCheckable(True)
        self.btn_show_token.setMaximumWidth(60)
        token_buttons.addWidget(self.btn_show_token)
        token_buttons.addStretch()
        api_layout.addRow("", token_buttons)

        self.lineedit_channel_id = QLineEdit()
        self.lineedit_channel_id.setPlaceholderText("C0123456789")
        api_layout.addRow("Channel ID:", self.lineedit_channel_id)

        self.btn_test = QPushButton("Test Connection")
        api_layout.addRow("", self.btn_test)

        help_label = QLabel(
            "<small>Get Bot Token from your Slack App settings.<br>"
            "Channel ID: Right-click channel > View channel details > Copy ID</small>"
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray;")
        api_layout.addRow(help_label)

        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        notif_group = QGroupBox("Notification Settings")
        notif_layout = QVBoxLayout()

        self.checkbox_notify_error = QCheckBox("Notify on errors")
        self.checkbox_notify_error.setToolTip("Send a Slack message when an error occurs during acquisition")
        notif_layout.addWidget(self.checkbox_notify_error)

        self.checkbox_notify_timepoint = QCheckBox("Notify on timepoint completion")
        self.checkbox_notify_timepoint.setToolTip("Send a Slack message after each timepoint completes")
        notif_layout.addWidget(self.checkbox_notify_timepoint)

        self.checkbox_send_mosaic = QCheckBox("Include mosaic snapshots")
        self.checkbox_send_mosaic.setToolTip("Upload mosaic screenshot with timepoint notifications")
        notif_layout.addWidget(self.checkbox_send_mosaic)

        self.checkbox_notify_start = QCheckBox("Notify on acquisition start")
        self.checkbox_notify_start.setToolTip("Send a Slack message when an acquisition begins")
        notif_layout.addWidget(self.checkbox_notify_start)

        self.checkbox_notify_finished = QCheckBox("Notify on acquisition finished")
        self.checkbox_notify_finished.setToolTip("Send a Slack message when an acquisition completes")
        notif_layout.addWidget(self.checkbox_notify_finished)

        notif_group.setLayout(notif_layout)
        layout.addWidget(notif_group)

        self.label_status = QLabel("")
        self.label_status.setStyleSheet("color: gray;")
        layout.addWidget(self.label_status)

        button_layout = QHBoxLayout()
        self.btn_save = QPushButton("Save")
        self.btn_close = QPushButton("Close")
        button_layout.addStretch()
        button_layout.addWidget(self.btn_save)
        button_layout.addWidget(self.btn_close)
        layout.addLayout(button_layout)

    def _connect_signals(self):
        self.btn_show_token.toggled.connect(self._toggle_token_visibility)
        self.btn_test.clicked.connect(self._test_connection)
        self.btn_save.clicked.connect(self._save_settings)
        self.btn_close.clicked.connect(self.close)
        self.checkbox_enabled.toggled.connect(self._update_controls_state)

    def _toggle_token_visibility(self, show: bool):
        if show:
            self.lineedit_bot_token.setEchoMode(QLineEdit.Normal)
            self.btn_show_token.setText("Hide")
        else:
            self.lineedit_bot_token.setEchoMode(QLineEdit.Password)
            self.btn_show_token.setText("Show")

    def _update_controls_state(self, enabled: bool):
        self.lineedit_bot_token.setEnabled(enabled)
        self.lineedit_channel_id.setEnabled(enabled)
        self.btn_show_token.setEnabled(enabled)
        self.btn_test.setEnabled(enabled)
        self.checkbox_notify_error.setEnabled(enabled)
        self.checkbox_notify_timepoint.setEnabled(enabled)
        self.checkbox_send_mosaic.setEnabled(enabled)
        self.checkbox_notify_start.setEnabled(enabled)
        self.checkbox_notify_finished.setEnabled(enabled)

    def _load_settings(self):
        cached = _load_slack_cached_settings()

        checkbox_map = {
            "enabled": self.checkbox_enabled,
            "notify_on_error": self.checkbox_notify_error,
            "notify_on_timepoint_complete": self.checkbox_notify_timepoint,
            "send_mosaic_snapshots": self.checkbox_send_mosaic,
            "notify_on_acquisition_start": self.checkbox_notify_start,
            "notify_on_acquisition_finished": self.checkbox_notify_finished,
        }
        lineedit_map = {
            "bot_token": self.lineedit_bot_token,
            "channel_id": self.lineedit_channel_id,
        }

        for key, checkbox in checkbox_map.items():
            attr = _SETTINGS_MAP[key][0]
            default = getattr(control._def.SlackNotifications, attr)
            checkbox.setChecked(cached.get(key, default))

        for key, lineedit in lineedit_map.items():
            attr = _SETTINGS_MAP[key][0]
            default = getattr(control._def.SlackNotifications, attr) or ""
            value = cached.get(key) or default
            lineedit.setText(value)

        if cached:
            log.info("Loaded Slack settings from cache")

        self._update_controls_state(self.checkbox_enabled.isChecked())

    def _save_settings(self):
        bot_token = self.lineedit_bot_token.text().strip() or None
        channel_id = self.lineedit_channel_id.text().strip() or None

        control._def.SlackNotifications.ENABLED = self.checkbox_enabled.isChecked()
        control._def.SlackNotifications.BOT_TOKEN = bot_token
        control._def.SlackNotifications.CHANNEL_ID = channel_id
        control._def.SlackNotifications.NOTIFY_ON_ERROR = self.checkbox_notify_error.isChecked()
        control._def.SlackNotifications.NOTIFY_ON_TIMEPOINT_COMPLETE = self.checkbox_notify_timepoint.isChecked()
        control._def.SlackNotifications.SEND_MOSAIC_SNAPSHOTS = self.checkbox_send_mosaic.isChecked()
        control._def.SlackNotifications.NOTIFY_ON_ACQUISITION_START = self.checkbox_notify_start.isChecked()
        control._def.SlackNotifications.NOTIFY_ON_ACQUISITION_FINISHED = self.checkbox_notify_finished.isChecked()

        if self._slack_notifier:
            self._slack_notifier.bot_token = bot_token
            self._slack_notifier.channel_id = channel_id

        settings = {
            "enabled": self.checkbox_enabled.isChecked(),
            "bot_token": bot_token,
            "channel_id": channel_id,
            "notify_on_error": self.checkbox_notify_error.isChecked(),
            "notify_on_timepoint_complete": self.checkbox_notify_timepoint.isChecked(),
            "send_mosaic_snapshots": self.checkbox_send_mosaic.isChecked(),
            "notify_on_acquisition_start": self.checkbox_notify_start.isChecked(),
            "notify_on_acquisition_finished": self.checkbox_notify_finished.isChecked(),
        }

        try:
            os.makedirs(os.path.dirname(SLACK_CACHE_FILE), exist_ok=True)
            with open(SLACK_CACHE_FILE, "w") as f:
                yaml.dump(settings, f, default_flow_style=False)
            self.label_status.setText("Settings saved")
            self.label_status.setStyleSheet("color: green;")
            log.info("Slack settings saved to cache")
        except Exception as e:
            self.label_status.setText(f"Failed to save: {e}")
            self.label_status.setStyleSheet("color: red;")
            log.error(f"Failed to save Slack settings: {e}")

        self.settings_changed.emit()

    def _test_connection(self):
        bot_token = self.lineedit_bot_token.text().strip()
        channel_id = self.lineedit_channel_id.text().strip()

        if not bot_token:
            QMessageBox.warning(self, "Test Failed", "Please enter a Bot Token")
            return
        if not channel_id:
            QMessageBox.warning(self, "Test Failed", "Please enter a Channel ID")
            return

        temp_notifier = SlackNotifier(bot_token=bot_token, channel_id=channel_id)
        old_enabled = control._def.SlackNotifications.ENABLED
        control._def.SlackNotifications.ENABLED = True

        try:
            success, message = temp_notifier.test_connection()
        finally:
            temp_notifier.close()
            control._def.SlackNotifications.ENABLED = old_enabled

        if success:
            self.label_status.setText("Test successful!")
            self.label_status.setStyleSheet("color: green;")
            QMessageBox.information(self, "Test Successful", "Slack API connection successful!")
        else:
            self.label_status.setText(f"Test failed: {message}")
            self.label_status.setStyleSheet("color: red;")
            QMessageBox.warning(self, "Test Failed", f"Connection failed: {message}")

    def set_slack_notifier(self, notifier: SlackNotifier):
        self._slack_notifier = notifier


def load_slack_settings_from_cache():
    """Load Slack settings from cache file into runtime config (call at startup)."""
    cached = _load_slack_cached_settings()
    if not cached:
        return

    for key, (attr, _) in _SETTINGS_MAP.items():
        if key in cached:
            setattr(control._def.SlackNotifications, attr, cached[key])

    log.info("Loaded Slack settings from cache into runtime config")
