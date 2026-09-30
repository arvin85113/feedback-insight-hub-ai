from django.test import SimpleTestCase

from desktop_app.autostart import is_enabled, set_enabled


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved=0, access=0):
        return self

    def CreateKey(self, root, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class AutostartTests(SimpleTestCase):
    def test_toggle(self):
        registry = FakeRegistry()
        command = '"C:\\Apps\\FeedbackInsightHub.exe"'
        self.assertFalse(is_enabled(registry=registry, command=command))
        set_enabled(True, registry=registry, command=command)
        self.assertTrue(is_enabled(registry=registry, command=command))
        set_enabled(False, registry=registry, command=command)
        self.assertFalse(is_enabled(registry=registry, command=command))
        set_enabled(False, registry=registry, command=command)  # already off
