"""Client modules with lazy convenience exports.

Importing ``modules.config`` must not eagerly import Requests, PyQt or the Excel
runtime. Keeping these exports lazy also makes configuration recovery possible
when an optional runtime dependency is damaged.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "Config": ("modules.config", "Config"),
    "Logger": ("modules.logger", "Logger"),
    "ApiClient": ("modules.api_client", "ApiClient"),
    "DeviceManager": ("modules.device_manager", "DeviceManager"),
    "ProgressReader": ("modules.progress_reader", "ProgressReader"),
    "MetricsCollector": ("modules.metrics_collector", "MetricsCollector"),
    "StatusReporter": ("modules.status_reporter", "StatusReporter"),
    "TrayIcon": ("modules.tray_icon", "TrayIcon"),
    "ConfigWindow": ("modules.config_window", "ConfigWindow"),
    "LogWindow": ("modules.log_window", "LogWindow"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
