"""
配置窗口模块 - PyQt6 实现
"""

import sys
import os
from PyQt6.QtWidgets import (
    QDialog,
    QApplication,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QMessageBox,
    QFileDialog,
)
from PyQt6.QtCore import Qt
from typing import Optional, Tuple


class ConfigWindow(QDialog):
    def __init__(self, current_config: dict, preset_devices: list, parent=None):
        super().__init__(parent)
        self.current_config = current_config
        self.preset_devices = preset_devices
        self.config_data = {}
        self._setup_ui()
        self._load_current_config()

    def _setup_ui(self):
        """设置 UI"""
        self.setWindowTitle("设备配置")
        self.setFixedWidth(450)

        layout = QVBoxLayout()

        device_code_label = QLabel("设备编码:")
        layout.addWidget(device_code_label)

        self.device_code_combo = QComboBox()
        self.device_code_combo.setEditable(True)
        self.device_code_combo.addItems(self.preset_devices)
        layout.addWidget(self.device_code_combo)

        self.device_code_combo.currentTextChanged.connect(self._on_device_code_changed)

        device_name_label = QLabel("设备名称:")
        layout.addWidget(device_name_label)

        self.device_name_edit = QLineEdit()
        layout.addWidget(self.device_name_edit)

        server_url_label = QLabel("服务器地址:")
        layout.addWidget(server_url_label)

        self.server_url_edit = QLineEdit()
        layout.addWidget(self.server_url_edit)

        working_path_label = QLabel("工作路径:")
        layout.addWidget(working_path_label)

        working_path_layout = QHBoxLayout()
        self.working_path_edit = QLineEdit()
        working_path_layout.addWidget(self.working_path_edit)

        self.browse_button = QPushButton("浏览...")
        self.browse_button.clicked.connect(self._browse_working_path)
        working_path_layout.addWidget(self.browse_button)

        layout.addLayout(working_path_layout)

        interval_label = QLabel("上报间隔（秒）:")
        layout.addWidget(interval_label)

        self.interval_spin = QSpinBox()
        self.interval_spin.setMinimum(1)
        self.interval_spin.setMaximum(3600)
        self.interval_spin.setValue(5)
        layout.addWidget(self.interval_spin)

        button_layout = QHBoxLayout()

        self.ok_button = QPushButton("确定")
        self.ok_button.clicked.connect(self._on_ok)
        button_layout.addWidget(self.ok_button)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)

        layout.addLayout(button_layout)
        self.setLayout(layout)

    def _browse_working_path(self):
        """浏览工作路径"""
        folder_path = QFileDialog.getExistingDirectory(self, "选择工作路径", "")
        if folder_path:
            self.working_path_edit.setText(folder_path)

    def _load_current_config(self):
        """加载当前配置"""
        device_code = self.current_config.get("device_code", "1号")

        if device_code in self.preset_devices:
            index = self.preset_devices.index(device_code)
            self.device_code_combo.setCurrentIndex(index)
        else:
            self.device_code_combo.setEditText(device_code)

        self.device_name_edit.setText(self.current_config.get("device_name", ""))
        self.server_url_edit.setText(
            self.current_config.get("server_url", "http://192.168.1.100:8000")
        )
        self.working_path_edit.setText(self.current_config.get("working_path", ""))
        self.interval_spin.setValue(self.current_config.get("report_interval", 5))

    def _on_device_code_changed(self, text: str):
        """设备编码改变事件"""
        if text in self.preset_devices:
            self.device_name_edit.setText(f"{text}设备")
        else:
            self.device_name_edit.setText("")

    def _on_ok(self):
        """确定按钮点击事件"""
        device_code = self.device_code_combo.currentText().strip()
        device_name = self.device_name_edit.text().strip()
        server_url = self.server_url_edit.text().strip()
        working_path = self.working_path_edit.text().strip()
        interval = self.interval_spin.value()

        if not device_code:
            QMessageBox.warning(self, "错误", "设备编码不能为空")
            return

        if not device_name:
            QMessageBox.warning(self, "错误", "设备名称不能为空")
            return

        if not server_url:
            QMessageBox.warning(self, "错误", "服务器地址不能为空")
            return

        if not working_path:
            QMessageBox.warning(self, "错误", "工作路径不能为空")
            return

        self.config_data = {
            "device_code": device_code,
            "device_name": device_name,
            "server_url": server_url,
            "working_path": working_path,
            "report_interval": interval,
            "manual_status": None,
            "is_first_run": False,
            "device_registered": False,
        }

        self.accept()

    def get_config(self) -> Optional[dict]:
        """获取配置数据

        Returns:
            dict or None: 配置数据
        """
        if self.result() == QDialog.DialogCode.Accepted:
            return self.config_data
        return None

    @staticmethod
    def show_config_dialog(
        current_config: dict, preset_devices: list
    ) -> Optional[dict]:
        """显示配置对话框

        Args:
            current_config: 当前配置
            preset_devices: 预设设备列表

        Returns:
            dict or None: 用户输入的配置
        """
        app = None
        existing_app = QApplication.instance()
        config = None

        if not existing_app:
            app = QApplication(sys.argv)

        dialog = ConfigWindow(current_config, preset_devices)
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted:
            config = dialog.get_config()

        if app:
            app.quit()

        return config
