"""Start the node when the user signs in to Windows (HKCU Run key, no admin rights)."""

import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "FeedbackInsightHub"


def _registry(registry):
    if registry is not None:
        return registry
    import winreg

    return winreg


def launch_command():
    return f'"{sys.executable}"'


def is_enabled(*, registry=None, command=None):
    registry = _registry(registry)
    command = command or launch_command()
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
            value, _kind = registry.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return False
    return value == command


def set_enabled(enabled, *, registry=None, command=None):
    registry = _registry(registry)
    command = command or launch_command()
    with registry.CreateKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_SZ, command)
        else:
            try:
                registry.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
