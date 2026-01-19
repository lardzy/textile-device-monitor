"""
日志查看窗口模块 - PyQt6 实现
"""

import sys
from PyQt6.QtWidgets import (
    QDialog,
    QApplication,
    QVBoxLayout,
    QTextEdit,
    QPushButton,
    QHBoxLayout,
    QSpinBox,
    QLabel,
)
from PyQt6.QtCore import Qt, QTimer
from typing import Optional, Callable


class LogWindow(QDialog):
    def __init__(self, get_logs_callback: callable, parent=None):
        super().__init__(parent)
        self.get_logs_callback = get_logs_callback
        self.timer = QTimer()
        self.timer.timeout.connect(self._refresh_logs)
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        self.setWindowTitle("日志查看")
        self.resize(800, 600)

        layout = QVBoxLayout()

        control_layout = QHBoxLayout()

        refresh_label = QLabel("行数:")
        control_layout.addWidget(refresh_label)

        self.lines_spin = QSpinBox()
        self.lines_spin.setMinimum(10)
        self.lines_spin.setMaximum(1000)
        self.lines_spin.setValue(100)
        control_layout.addWidget(self.lines_spin)

        self.auto_refresh_checkbox = QLabel("自动刷新: 开")
        control_layout.addWidget(self.auto_refresh_checkbox)

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self._refresh_logs)
        control_layout.addWidget(self.refresh_button)

        self.auto_refresh_button = QPushButton("停止刷新")
        self.auto_refresh_button.clicked.connect(self._toggle_auto_refresh)
        control_layout.addWidget(self.auto_refresh_button)

        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self._clear_logs)
        control_layout.addWidget(self.clear_button)

        layout.addLayout(control_layout)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFontFamily("Courier New")
        self.log_text.setFontPointSize(9)
        layout.addWidget(self.log_text)

        button_layout = QHBoxLayout()

        self.ok_button = QPushButton("关闭")
        self.ok_button.clicked.connect(self.close)
        button_layout.addWidget(self.ok_button)

        layout.addLayout(button_layout)
        self.setLayout(layout)

        self._refresh_logs()
        self.timer.start(2000)

    def _refresh_logs(self):
        """刷新日志"""
        try:
            lines = self.lines_spin.value()
            logs = self.get_logs_callback(lines)

            self.log_text.clear()

            for line in logs:
                self.log_text.append(line.rstrip())

            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum()
            )

        except Exception as e:
            self.log_text.append(f"刷新日志失败: {e}")

    def _toggle_auto_refresh(self):
        """切换自动刷新"""
        if self.timer.isActive():
            self.timer.stop()
            self.auto_refresh_label = "自动刷新: 关"
            self.auto_refresh_button.setText("开始刷新")
        else:
            self.timer.start(2000)
            self.auto_refresh_label = "自动刷新: 开"
            self.auto_refresh_button.setText("停止刷新")

    def _clear_logs(self):
        """清空日志显示"""
        self.log_text.clear()

    def closeEvent(self, event):
        """关闭事件"""
        if self.timer.isActive():
            self.timer.stop()
        event.accept()

    @staticmethod
    def show_log_dialog(get_logs_callback: callable):
        """显示日志对话框

        Args:
            get_logs_callback: 获取日志的回调函数
        """
        app = None
        existing_app = QApplication.instance()

        if not existing_app:
            app = QApplication(sys.argv)

        dialog = LogWindow(get_logs_callback)
        dialog.exec()

        if app:
            app.quit()
