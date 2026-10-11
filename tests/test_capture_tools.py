# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun

"""Raw layout and persistent recording regression tests; no sensor required."""

from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from capture import parse_args
from frame import decode_preview, save_frame
from recording import Recording, saved_fingers


class CaptureToolsTest(unittest.TestCase):
    def test_inferred_default_preserves_pixel_boundaries(self):
        payload = struct.pack("<6080H", *([1000, 2000] * 3040))
        frame = bytes.fromhex("02 2f 81 04") + payload + b"\xff\xff"
        self.assertEqual(decode_preview(frame, invert=False),
                         (40, 152, bytes([0, 255]) * 3040))
        self.assertEqual(decode_preview(frame)[2], bytes([255, 0]) * 3040)
        constant = frame[:4] + struct.pack("<6080H", *([1200] * 6080)) + frame[-2:]
        self.assertEqual(decode_preview(constant), (40, 152, bytes([127]) * 6080))

    def test_rejects_incomplete_and_oversized_frames(self):
        for size in (0, 12165, 12167):
            with self.assertRaises(ValueError):
                decode_preview(bytes(size))

    def test_export_preserves_bytes_and_private_permissions(self):
        frame = bytes(i % 256 for i in range(12166))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "captures"
            stem = save_frame(output, frame, b"serialized-print")
            self.assertEqual(stem.with_suffix(".bin").read_bytes(), frame)
            self.assertEqual(stem.with_suffix(".fpprint").read_bytes(), b"serialized-print")
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertNotEqual(save_frame(output, frame), stem)
            with self.assertRaises(ValueError):
                save_frame(output, b"short")

    def test_entry_point_defaults_and_modes(self):
        args = parse_args([])
        self.assertEqual(args.output, Path(__file__).resolve().parents[1] / "build" / "captures")
        self.assertFalse(args.cli)
        self.assertIsNone(args.preview)
        self.assertEqual(parse_args(["--cli", "--count", "3"]).count, 3)
        self.assertEqual(parse_args(["--preview", "sample.bin"]).preview, Path("sample.bin"))

    def test_record_reopen_append_and_separate_fingers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            frame = bytes(i % 256 for i in range(12166))
            recording = Recording(directory, name="右手食指")
            path = recording.path
            recording.append(frame, 100, 200)
            recording.close()
            self.assertEqual(saved_fingers(directory), [("右手食指", path)])
            recording = Recording(directory, path=path)
            recording.append(frame[::-1], 300, 400)
            with self.assertRaises(ValueError):
                recording.append(b"short", 500, 600)
            recording.close()
            with sqlite3.connect(path) as db:
                rows = db.execute("SELECT session_id, received_unix_ns, received_monotonic_ns, raw "
                                  "FROM frames ORDER BY id").fetchall()
                self.assertEqual(rows, [(1, 100, 200, frame), (2, 300, 400, frame[::-1])])
                self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE ended_utc IS NOT NULL")
                                 .fetchone()[0], 2)
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            other = Recording(directory, name="左手食指")
            other.close()
            self.assertNotEqual(other.path, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
