# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun

"""GUI integration tests: run in nix develop with a desktop display."""

from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from gui import FPrint, GLib, Gtk, PreviewWindow
from recording import Recording


@unittest.skipUnless(Gtk.init_check()[0], "A desktop display is required")
class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.frame = bytes.fromhex("02 2f 81 04") + struct.pack("<6080H", *range(6080)) + b"\xff\xff"
        self.window = PreviewWindow(SimpleNamespace(output=self.directory, preview=None))

    def tearDown(self):
        w = self.window
        w.running = False
        w.end_record()
        for source in (w.refresh_id, w.next_id):
            if source:
                GLib.source_remove(source)
        w.destroy()
        self.tmp.cleanup()

    def test_recording_boundaries_and_export(self):
        w = self.window
        w.running = True
        enrolled = Mock()
        enrolled.get_property.return_value = GLib.Variant("(say)", ("FT9362-RAW-v1", self.frame))
        enrolled.serialize.return_value = b"serialized-test-print"
        device = Mock()
        device.enroll_finish.return_value = enrolled

        def receive():
            w.captured(device, None)
            GLib.source_remove(w.next_id)
            w.next_id = 0

        receive()  # Preview alone must write nothing.
        self.assertEqual(list(self.directory.iterdir()), [])
        w.recording = Recording(self.directory, name="测试手指")
        database = w.recording.path
        receive()
        receive()
        w.end_record()
        receive()  # Stopping recording leaves preview active, without more DB rows.
        self.assertTrue(w.running)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT raw FROM frames").fetchall(), [(self.frame,)] * 2)
        w.save()
        self.assertEqual(next(self.directory.glob("*.bin")).read_bytes(), self.frame)
        png = next(self.directory.glob("*.png")).read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (40, 152))

    def test_preview_inversion_preserves_raw_export(self):
        w = self.window
        w.frame = self.frame
        w.render()
        self.assertEqual((w.pixbuf.get_width(), w.pixbuf.get_height()), (40, 152))
        self.assertLessEqual(w.picture.get_pixbuf().get_height(), 456)
        inverted = bytes(w.pixbuf.get_pixels())
        w.invert.set_active(False)
        normal = bytes(w.pixbuf.get_pixels())
        self.assertEqual(normal, bytes(255 - value for value in inverted))
        w.save()
        png = next(self.directory.glob("*.png")).read_bytes()
        self.assertEqual(struct.unpack(">II", png[16:24]), (40, 152))
        self.assertEqual(next(self.directory.glob("*.bin")).read_bytes(), self.frame)
        self.assertEqual(list(self.directory.glob("*.fpprint")), [])

    def test_offline_preview_disables_capture_and_recording(self):
        source = self.directory / "input.bin"
        source.write_bytes(self.frame)
        offline = PreviewWindow(SimpleNamespace(output=self.directory, preview=source))
        try:
            self.assertIsNone(offline.device)
            self.assertEqual(offline.frame, self.frame)
            self.assertFalse(offline.start_button.get_sensitive())
            self.assertFalse(offline.record_button.get_sensitive())
            self.assertTrue(offline.save_button.get_sensitive())
        finally:
            GLib.source_remove(offline.refresh_id)
            offline.destroy()

    def test_cooling_keeps_recording_open(self):
        w = self.window
        w.running = True
        w.recording = Recording(self.directory, name="测试手指")
        device = Mock()
        device.enroll_finish.side_effect = GLib.Error.new_literal(
            FPrint.device_error_quark(), "Too hot", FPrint.DeviceError.TOO_HOT)
        w.captured(device, None)
        self.assertTrue(w.running)
        self.assertIsNotNone(w.recording)
        self.assertTrue(w.next_id)
        self.assertIn("冷却", w.status.get_text())

    def test_device_tests_wait_for_preview_close_and_block_restart(self):
        w = self.window
        w.running = True
        w.pending = True
        w.cancellable = Mock()
        w.device = Mock()
        w.device.is_open.return_value = True
        w.recording = Recording(self.directory, name="测试手指")
        with patch("device_test_gui.DeviceTestWindow") as factory:
            w.open_tests()
            w.cancellable.cancel.assert_called_once()
            self.assertIsNone(w.recording)
            factory.assert_not_called()
            w.start()
            self.assertFalse(w.running)
            w.pending = False  # Capture cancellation callback has completed.
            w.release()
            factory.assert_not_called()
            w.closed(w.device, None)
            factory.assert_called_once()
            self.assertFalse(w.start_button.get_sensitive())
            w.tests_closed()
            self.assertTrue(w.start_button.get_sensitive())

    def test_failed_preview_close_does_not_open_second_usb_owner(self):
        w = self.window
        w.test_requested = True
        w.device = Mock()
        w.device.close_finish.side_effect = GLib.Error("test close failure")
        with patch("device_test_gui.DeviceTestWindow") as factory:
            w.closed(w.device, None)
            factory.assert_not_called()
        self.assertFalse(w.test_requested)
        self.assertIn("关闭设备失败", w.status.get_text())


if __name__ == "__main__":
    unittest.main()
