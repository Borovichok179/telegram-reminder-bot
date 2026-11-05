# -*- coding: utf-8 -*-
"""Проверки файлов запуска для Windows.

cmd.exe плохо разбирает .bat с Unix-переводами строк и не-ASCII символами,
а WSH надёжнее работает с CRLF. Эти тесты не дают случайно сломать файлы
повторным сохранением в UTF-8/LF.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_bytes(name: str) -> bytes:
    return (ROOT / name).read_bytes()


class WindowsLauncherTests(unittest.TestCase):
    def assert_crlf_only(self, data: bytes) -> None:
        self.assertNotIn(b"\xef\xbb\xbf", data[:3], "BOM в начале файла")
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""), "найден одиночный LF")

    def test_run_bat_is_ascii_and_crlf(self):
        data = read_bytes("run.bat")
        data.decode("ascii")  # cmd.exe не должен угадывать кодировку
        self.assert_crlf_only(data)

    def test_background_launcher_is_crlf(self):
        self.assert_crlf_only(read_bytes("start_background.vbs"))


if __name__ == "__main__":
    unittest.main()
