# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Reject unusable/tampered experiment evidence before drawing conclusions."""

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from analyze_finger_interaction import summarize
from probe_device import bcc


class AnalysisTest(unittest.TestCase):
    def make_report(self, root):
        payload = struct.pack("<6080H", *([1000] * 6080))
        body = bytes.fromhex("2f 81 04") + payload
        raw = b"\x02" + body + bytes([bcc(body), 0])
        (root / "baseline-00.bin").write_bytes(raw)
        report = {"completed": True, "operations": [
            {"query": "raw_frame", "label": "baseline/frame", "response_hex": raw.hex(),
             "data_hex": payload.hex(" "), "transfers": [
                 {"endpoint": "81", "return_code": 0, "actual_length": len(raw),
                  "hex": raw.hex(), "elapsed_ms": 18}]}],
            "samples": [{"phase": "baseline", "file": "baseline-00.bin",
                         "sha256": hashlib.sha256(payload).hexdigest(), "received_monotonic_ns": 1,
                         "status_before": "00", "status_after": "00", "extended_status": "06"}]}
        path = root / "report.json"
        path.write_text(json.dumps(report))
        return path, report

    def test_validated_constant_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self.make_report(Path(tmp))
            result = summarize(path)
            self.assertEqual(result["normal_frames"], 1)
            self.assertEqual(result["phases"]["baseline"]["pixel_min"], 1000)

    def test_invalidated_and_incomplete_runs_are_rejected(self):
        for fields in ({"valid_for_analysis": False}, {"completed": False}):
            with tempfile.TemporaryDirectory() as tmp:
                path, report = self.make_report(Path(tmp))
                report.update(fields)
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    summarize(path)

    def test_tampered_sample_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self.make_report(Path(tmp))
            (path.parent / "baseline-00.bin").write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                summarize(path)

    def test_transfer_bytes_must_match_saved_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, report = self.make_report(Path(tmp))
            transfer = report["operations"][0]["transfers"][0]
            raw = bytearray.fromhex(transfer["hex"])
            raw[-1] ^= 1
            transfer["hex"] = raw.hex()
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "actual USB"):
                summarize(path)


if __name__ == "__main__":
    unittest.main()
