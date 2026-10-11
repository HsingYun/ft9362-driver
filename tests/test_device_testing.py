# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Session ownership, evidence validity and biometric-free report exports."""

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from device_testing import TestSession, operation_summary, plan
from probe_device import ProbeError, bcc


class FingerDevice:
    """Deterministic transport evidence; never opens real hardware."""
    def __init__(self, report, verbose=False, stop=None):
        self.report, self.stop = report, stop
        self.usb = None
        self.closed = False

    def open(self):
        self.usb = object()
        self.report["connections"] = [{"release_errors": []}]

    def close(self):
        self.closed = True
        self.usb = None

    def health(self, label):
        pass

    def pause(self, seconds):
        if self.stop.is_set():
            raise ProbeError("operator stopped")

    def require(self, query, label):
        self.pause(0)
        if query.name in ("raw_frame", "extended_frame"):
            payload = struct.pack("<6080H", *([1000] * 6080))
        elif query.name == "extended_status":
            payload = b"\x05"
        else:
            payload = b"\x01" if label.split("/")[0] in ("hold", "repress", "compare") and label.endswith("/wait") else b"\0"
        body = (len(payload) + 1).to_bytes(2, "big") + b"\x04" + payload
        raw = b"\x02" + body + bytes([bcc(body), 0])
        op = {"query": query.name, "label": label, "outcome": "valid_data", "status": 4,
              "data_hex": payload.hex(" "), "response_hex": raw.hex(" "),
              "data_sha256": hashlib.sha256(payload).hexdigest(),
              "transfers": [{"endpoint": "81", "return_code": 0, "actual_length": len(raw),
                             "hex": raw.hex(), "elapsed_ms": 18}]}
        self.report["operations"].append(op)
        return op


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_plan_preserves_required_order_and_press_counts(self):
        steps = plan("full")
        self.assertEqual(len(steps), 20)
        self.assertEqual([s.phase for s in steps if s.phase in ("hold", "repress", "compare")],
                         ["hold", "repress", "compare"])
        self.assertLess(next(i for i, s in enumerate(steps) if s.phase == "usb_recovery"),
                        next(i for i, s in enumerate(steps) if s.phase == "capture_idle_after"))

    def test_finger_scopes_complete_validate_and_export_without_biometrics(self):
        for scope, frames, extended in (("movement", 30, 30), ("comparison", 35, 8)):
            instances = []

            def factory(*args, **kwargs):
                instances.append(FingerDevice(*args, **kwargs))
                return instances[-1]

            session = TestSession(self.root, scope, experiment_factory=factory)
            while not session.terminal:
                session.run_next()
            self.assertTrue(session.summary["completed"])
            self.assertTrue(instances[0].closed)
            summary = session.summary["steps"][-1]["analysis"]
            self.assertEqual(summary["normal_frames"], frames)
            self.assertEqual(summary["extended_frames"], extended)
            for file in ("summary.json", "report.md"):
                public = (session.directory / file).read_text()
                for forbidden in ("sha256", "pixel_min", "response_hex", "e8 03 e8 03", ".bin", "finger_name"):
                    self.assertNotIn(forbidden, public)
                self.assertEqual((session.directory / file).stat().st_mode & 0o777, 0o600)
            session.finish(invalidated=True)
            raw = json.loads((session.raw_dir / scope / "report.json").read_text())
            self.assertFalse(raw["valid_for_analysis"])
            self.assertFalse(session.summary["completed"])

    def test_stop_between_finger_steps_releases_interface_and_preserves_partial_report(self):
        session = TestSession(self.root, "movement", experiment_factory=FingerDevice)
        session.run_next()
        ex = session.experiment
        self.assertIsNotNone(ex.usb)
        session.stop.set()
        session.finish()
        self.assertTrue(ex.closed)
        self.assertFalse(session.summary["completed"])
        self.assertEqual(session.summary["status"], "cancelled")
        self.assertEqual(len(json.loads(session.finger_path.read_text())["samples"]), 3)

    def test_stop_during_capture_preserves_one_sample_without_continuing(self):
        session = TestSession(self.root, "movement", experiment_factory=FingerDevice)
        session.progress = lambda *args: session.stop.set()
        session.run_next()
        self.assertTrue(session.terminal)
        self.assertEqual(session.summary["status"], "cancelled")
        self.assertIsNone(session.experiment)
        self.assertEqual(len(json.loads(session.finger_path.read_text())["samples"]), 1)
        self.assertEqual(session.summary["steps"][1]["status"], "pending")

    def test_mode_gate_never_sends_mode_writes_even_with_wake_ack(self):
        for outcome in ("valid_data", "semantic_rejection"):
            ex = Mock()
            ex.query.return_value = {"outcome": outcome}
            session = TestSession(self.root, "no_finger", experiment_factory=lambda *a, **kw: ex)
            session.index = next(i for i, s in enumerate(session.steps) if s.phase == "modes")
            session.run_next()
            self.assertEqual(session.summary["steps"][5]["status"], "blocked")
            self.assertEqual(ex.query.call_count, 1)
            self.assertEqual(ex.query.call_args.args[0].body, bytes.fromhex("82 01 00"))
            ex.close.assert_called_once()
            ex.modes.assert_not_called()

    def test_transport_failure_stops_plan_and_still_writes_report(self):
        ex = Mock()
        ex.queries.side_effect = ProbeError("bad BCC")
        session = TestSession(self.root, "no_finger", experiment_factory=lambda *a, **kw: ex)
        session.run_next()
        session.run_next()
        self.assertTrue(session.terminal)
        self.assertEqual(session.summary["status"], "failed")
        self.assertEqual(ex.open.call_count, 1)
        ex.close.assert_called_once()
        self.assertEqual(session.summary["steps"][1]["status"], "pending")
        self.assertTrue((session.directory / "report.md").exists())

    def failed_session(self):
        ex = Mock()
        ex.queries.side_effect = ProbeError("bulk 81: LIBUSB_ERROR_PIPE (-9)")
        session = TestSession(self.root, "no_finger", experiment_factory=lambda *a, **kw: ex)
        session.run_next()
        return session

    def test_failed_recovery_cannot_advance_or_send_next_suite(self):
        session = self.failed_session()
        recovery = Mock()
        recovery.recover_transport.side_effect = ProbeError("recovery health failed")
        session.factory = lambda *a, **kw: recovery
        session.recover("skip")
        self.assertEqual(session.index, 0)
        self.assertTrue(session.can_recover)
        self.assertEqual(session.last_error, "recovery health failed")
        self.assertEqual(session.summary["steps"][1]["status"], "pending")
        recovery.close.assert_called_once()
        recovery.queries.assert_not_called()
        self.assertEqual(session.summary["recoveries"][-1]["status"], "failed")

    def test_reload_and_skip_preserves_failure_then_allows_next_step(self):
        original = self.failed_session()
        session = TestSession.load_failed(original.directory)
        self.assertTrue(session.can_recover)
        self.assertIn("LIBUSB_ERROR_PIPE", session.last_error)
        recovery = Mock()
        session.factory = lambda *a, **kw: recovery
        session.recover("skip")
        self.assertFalse(session.terminal)
        self.assertEqual(session.index, 1)
        self.assertEqual(session.summary["steps"][0]["status"], "failed")
        self.assertEqual(len(list(session.raw_dir.glob("attempt-*/queries.json"))), 1)
        self.assertIsNone(session.last_error)
        session.index = len(session.steps)
        session.complete_if_done()
        self.assertEqual(session.summary["status"], "observed")

    def test_retry_archives_old_attempt_without_overwriting_evidence(self):
        session = self.failed_session()
        original = (session.raw_dir / "queries.json").read_bytes()
        session.factory = lambda *a, **kw: Mock()
        session.recover("retry")
        self.assertEqual(session.index, 0)
        self.assertEqual(session.summary["steps"][0]["status"], "pending")
        self.assertFalse((session.raw_dir / "queries.json").exists())
        self.assertEqual(next(session.raw_dir.glob("attempt-*/queries.json")).read_bytes(), original)
        session.run_next()
        self.assertEqual(session.summary["steps"][0]["status"], "passed")
        self.assertEqual(session.summary["attempts"][0]["steps"][0]["status"], "failed")

    def test_finger_retry_restarts_group_and_invalidates_partial_samples(self):
        session = TestSession(self.root, "movement", experiment_factory=FingerDevice)
        session.run_next()
        def fail(*args):
            raise ProbeError("simulated transfer failure")
        session.progress = fail
        session.run_next()
        self.assertEqual(session.index, 1)
        session.factory = lambda *a, **kw: Mock()
        session.recover("retry")
        self.assertEqual(session.index, 0)
        self.assertTrue(all(r["status"] == "pending" for r in session.summary["steps"]))
        self.assertFalse((session.raw_dir / "movement").exists())
        archived = next(session.raw_dir.glob("attempt-*/movement/report.json"))
        report = json.loads(archived.read_text())
        self.assertEqual(len(report["samples"]), 4)
        self.assertFalse(report["valid_for_analysis"])

    def test_cancel_recovery_keeps_original_failure_and_position(self):
        session = self.failed_session()
        recovery = Mock()
        recovery.recover_transport.side_effect = lambda: session.stop.set()
        session.factory = lambda *a, **kw: recovery
        session.recover("skip")
        self.assertTrue(session.terminal)
        self.assertEqual(session.index, 0)
        self.assertEqual(session.summary["recoveries"][-1]["status"], "cancelled")
        recovery.close.assert_called_once()

    def test_frame_data_hashes_errors_and_stale_trailers_never_export(self):
        for query in ("raw_frame", "extended_frame", "unknown"):
            public = operation_summary({"query": query, "data_hex": "de ad be ef", "response_hex": "private",
                "trailing_hex": "ab", "data_sha256": "secret", "value": "private", "error": "private"})
            for key in ("data_hex", "response_hex", "trailing_hex", "data_sha256", "value", "error"):
                self.assertNotIn(key, public)
        self.assertNotIn("trailing_hex", operation_summary({"query": "finger_status", "trailing_hex": "ef"}))
        self.assertNotIn("data_hex", operation_summary({"query": "dimension_a", "outcome": "semantic_rejection",
                                                        "data_hex": "e8 03 " * 6080}))


if __name__ == "__main__":
    unittest.main()
