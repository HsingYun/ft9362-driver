# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""No hardware: cancellation timing, evidence, independent recovery and GUI plan."""

import ctypes as C
import json
from pathlib import Path
import sys
import tempfile
import struct
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from device_validation import (AsyncUSB, Transfer, CASES, CONTROLS, INTERRUPTIONS, STRATEGIES,
                               Case, NORMAL_FRAME, RESPONSE_CAPACITY, CancelExperiment, collect_frames,
                               injection_result, public_summary, residual_summary, run_case, run_suite)
from device_testing import TestSession, plan
from probe_device import ProbeError, bcc


def packet(payload):
    body = (len(payload) + 1).to_bytes(2, "big") + b"\x04" + payload
    return b"\x02" + body + bytes([bcc(body), 0])


class FakeLibusb:
    def __init__(self, cancel_code=0):
        self.transfer = Transfer()
        self.pointer = C.pointer(self.transfer)
        self.cancel_code = cancel_code
        self.cancel_calls = 0
        self.completed = False
        self.freed = False

    def libusb_alloc_transfer(self, iso):
        assert iso == 0
        return self.pointer

    def libusb_submit_transfer(self, transfer):
        return 0

    def libusb_cancel_transfer(self, transfer):
        self.cancel_calls += 1
        return self.cancel_code

    def libusb_handle_events_timeout_completed(self, context, timeout, done):
        self.transfer.status = 3 if self.cancel_calls and self.cancel_code == 0 else 0
        self.transfer.actual_length = 4
        for i, value in enumerate(b"data"):
            self.transfer.buffer[i] = value
        self.transfer.callback(self.pointer)
        self.completed = True
        return 0

    def libusb_free_transfer(self, transfer):
        assert self.completed
        self.freed = True


class QueuedUSB:
    """Commands queue replies; cancellation/close do not discard device input."""
    def __init__(self):
        self.timeout_ms = 2000
        self.pending = []
        self.read_capacities = []

    def check(self, code, operation):
        if code < 0:
            raise ProbeError(f"{operation}: {code}")

    def transfer_result(self, endpoint, payload, events, cancel_after_ms=None):
        if endpoint == 3:
            replies = {b"\x34": b"\x55\xaa", b"\x80\x01\x02": b"\xa5\x5a",
                       b"\x80\x03\x01": b"\x28", b"\x80\x04\x01": b"\x98",
                       b"\x81": bytes(12160)}
            self.pending.append(packet(replies[payload[3:-1]]))
            raw = payload
        else:
            self.read_capacities.append(payload)
            raw = self.pending[0][:payload] if self.pending else b""
            if self.pending:
                self.pending[0] = self.pending[0][payload:]
                if not self.pending[0]:
                    self.pending.pop(0)
        code = -10 if cancel_after_ms is not None else (-7 if not raw else 0)
        event = {"endpoint": f"{endpoint:02x}", "requested_length": payload if endpoint & 0x80 else len(payload),
                 "actual_length": len(raw), "hex": raw.hex(" "), "return_code": code,
                 "transfer_status": 3 if code == -10 else (2 if code == -7 else 0)}
        if cancel_after_ms is not None:
            event["cancel_code"] = 0
        events.append(event)
        return raw, event

    def transfer(self, endpoint, payload, events):
        raw, event = self.transfer_result(endpoint, payload, events)
        self.check(event["return_code"], "transfer")
        return raw


class ValidationTest(unittest.TestCase):
    def test_one_gui_step_contains_whole_matrix(self):
        self.assertEqual(len(plan("validation")), 1)
        self.assertEqual(len(CASES), len(CONTROLS) + len(INTERRUPTIONS) * len(STRATEGIES))
        self.assertEqual(len({(c.name, s) for c, s in CASES}), len(CASES))
        for case in INTERRUPTIONS:
            self.assertEqual({s for c, s in CASES if c == case}, set(STRATEGIES))
        self.assertTrue({"out", "in", "partial", "before", "complete", "deferred"} <= {c.point for c, _ in CASES})

    def usb(self, cancel_code=0):
        usb = AsyncUSB.__new__(AsyncUSB)
        usb.lib = FakeLibusb(cancel_code)
        usb.handle = C.c_void_p()
        usb.context = C.c_void_p()
        usb.timeout_ms = 2000
        usb.stop = threading.Event()
        usb.checkpoint = Mock()
        return usb

    def test_actual_cancel_preserves_partial_bytes_and_callback_lifetime(self):
        usb = self.usb()
        events = []
        raw, result = usb.transfer_result(0x81, 64, events, 0)
        self.assertEqual(raw, b"data")
        self.assertEqual(result["hex"], "64 61 74 61")
        self.assertEqual(result["transfer_status"], 3)
        self.assertEqual(result["return_code"], -10)
        self.assertTrue(injection_result(result, len(raw))["target_hit"])
        self.assertEqual(usb.lib.cancel_calls, 1)
        self.assertTrue(usb.lib.freed)

    def test_completion_before_timer_and_cancel_race_are_not_hits(self):
        for delay, cancel_code in ((100, 0), (0, -5)):
            usb = self.usb(cancel_code)
            _, result = usb.transfer_result(0x81, 64, [], delay)
            outcome = injection_result(result, 4)
            self.assertFalse(outcome["target_hit"])
            self.assertEqual(outcome["outcome"], "completed_before_cancel")
            self.assertTrue(usb.lib.freed)

    def test_operator_stop_cancels_pending_transfer_once(self):
        usb = self.usb()
        usb.stop.set()
        _, event = usb.transfer_result(0x81, 64, [])
        self.assertEqual(event["transfer_status"], 3)
        self.assertEqual(usb.lib.cancel_calls, 1)

    def test_residual_reconstruction_uses_partial_prefix_and_checksum(self):
        raw = packet(bytes(12160))
        ops = [{"label": "interruption", "response_hex": raw[:4096].hex()},
               {"label": "bounded_drain", "transfers": [{"hex": raw[4096:].hex()}]}]
        result = residual_summary(NORMAL_FRAME, ops, 1)
        self.assertTrue(result["reconstructed_response_valid"])
        self.assertEqual(result["residual_bytes"], len(raw) - 4096)
        ops[1]["transfers"][0]["hex"] = (raw[4096:-2] + b"\xff\0").hex()
        self.assertFalse(residual_summary(NORMAL_FRAME, ops, 1)["reconstructed_response_valid"])

    def test_case_recovery_is_independent_and_cleanup_is_always_attempted(self):
        for strategy in STRATEGIES:
            ex = Mock()
            ex.interrupt.return_value = {"target_hit": True, "outcome": "cancelled"}
            ex.followup.return_value = {"frames": 3, "distinct_payloads": 3, "health_valid": True}
            report = {"operations": [{"label": "interruption"}]}
            result = run_case(INTERRUPTIONS[0], strategy, report, threading.Event(), lambda: None,
                              factory=lambda *args: ex)
            self.assertEqual(result["status"], "passed")
            ex.recover.assert_called_once_with(strategy)
            self.assertEqual(ex.method_calls[:2], [unittest.mock.call.open(), unittest.mock.call.establish_baseline("baseline")])
            ex.cleanup.assert_called_once()
            ex.close.assert_called_once()

    def test_failed_recovery_does_not_get_an_unreported_second_method(self):
        ex = Mock()
        ex.interrupt.return_value = {"target_hit": True}
        ex.recover.side_effect = ProbeError("broken")
        result = run_case(INTERRUPTIONS[0], "clear_halt", {"operations": []}, threading.Event(), lambda: None,
                          factory=lambda *args: ex)
        self.assertEqual(result["status"], "recovery_failed")
        ex.recover.assert_called_once_with("clear_halt")
        ex.followup.assert_not_called()
        ex.close.assert_called_once()

    def test_missing_contact_is_blocked_and_does_not_inject(self):
        ex = Mock()
        ex.prepare.side_effect = ProbeError("no contact")
        result = run_case(INTERRUPTIONS[0], "none", {"operations": []}, threading.Event(), lambda: None,
                          factory=lambda *args: ex)
        self.assertEqual(result["status"], "blocked")
        ex.interrupt.assert_not_called()
        ex.recover.assert_not_called()

    def test_failed_baseline_never_sends_target_command(self):
        ex = Mock()
        ex.establish_baseline.side_effect = ProbeError("reset failed")
        result = run_case(INTERRUPTIONS[0], "reset", {"operations": []}, threading.Event(), lambda: None,
                          factory=lambda *args: ex)
        self.assertEqual(result["status"], "baseline_failed")
        ex.prepare.assert_not_called()
        ex.interrupt.assert_not_called()
        ex.close.assert_called_once()

    def test_stale_image_is_consumed_before_failure_and_next_case_gets_clean_baseline(self):
        usb = QueuedUSB()
        resets = []
        class Experiment(CancelExperiment):
            def open(self):
                self.usb = usb
                self.transport_healthy = True
            def reset(self):
                # This checks the test runner's ordering, not firmware reset semantics.
                if usb.pending:
                    raise AssertionError("reset attempted with queued input")
                resets.append(True)
            def close(self):
                self.usb = None
            def prepare(self, query):
                pass
        report = {"operations": []}
        result = run_case(Case("normal_out", NORMAL_FRAME, "out"), "none", report,
                          threading.Event(), lambda: None, factory=Experiment)
        self.assertEqual(result["status"], "recovery_failed")
        self.assertEqual(result["failure_stage"], "followup")
        self.assertEqual(result["injection"]["actual_length"], 5)
        self.assertEqual(result["cleanup"]["status"], "passed")
        probe = next(o for o in report["operations"] if o["label"] == "followup/probe")
        self.assertEqual(probe["outcome"], "unexpected_response")
        self.assertEqual(len(bytes.fromhex(probe["response_hex"])), 12166)
        self.assertEqual(probe["transfers"][-1]["requested_length"], RESPONSE_CAPACITY)
        self.assertEqual(usb.pending, [])
        next_result = run_case(CONTROLS[0], "none", {"operations": []}, threading.Event(),
                               lambda: None, factory=Experiment)
        self.assertTrue(next_result["baseline_valid"])
        self.assertTrue(next_result["followup"]["health_valid"])
        self.assertEqual(len(resets), 2)

    def test_probe_rejects_stale_status_partial_image_and_extra_trailer(self):
        for raw in (packet(b"\0"), packet(bytes(12160))[4096:], packet(b"\x55\xaa") + b"\0"):
            ex = CancelExperiment({"operations": []}, threading.Event(), lambda: None)
            ex.usb = QueuedUSB()
            ex.usb.pending.append(raw)
            with self.assertRaises(ProbeError):
                ex.probe_bridge("probe")
            self.assertFalse(ex.transport_healthy)
            self.assertEqual(ex.usb.read_capacities, [RESPONSE_CAPACITY])
            # The new bridge reply remains for cleanup, never searched for to claim success.
            self.assertEqual(ex.usb.pending, [packet(b"\x55\xaa")])

    def test_baseline_drains_existing_image_before_reset_and_requires_health(self):
        ex = CancelExperiment({"operations": []}, threading.Event(), lambda: None)
        ex.usb = QueuedUSB()
        ex.usb.pending.append(packet(bytes(12160)))
        ex.reset = Mock(side_effect=lambda: self.assertEqual(ex.usb.pending, []))
        ex.establish_baseline("baseline")
        ex.reset.assert_called_once()
        self.assertEqual(ex.usb.pending, [])
        ex.usb = Mock(timeout_ms=2000)
        ex.usb.transfer_result.return_value = (b"x", {"return_code": 0})
        ex.reset.reset_mock()
        with self.assertRaises(ProbeError):
            ex.establish_baseline("baseline")
        ex.reset.assert_not_called()

    def test_cleanup_failure_preserves_measured_result_and_close_is_attempted(self):
        ex = Mock()
        ex.interrupt.return_value = {"target_hit": True}
        ex.followup.side_effect = ProbeError("stale image")
        ex.cleanup.side_effect = ProbeError("device still unresponsive")
        result = run_case(INTERRUPTIONS[0], "none", {"operations": []}, threading.Event(),
                          lambda: None, factory=lambda *args: ex)
        self.assertEqual(result["status"], "recovery_failed")
        self.assertEqual(result["failure_stage"], "followup")
        self.assertEqual(result["cleanup"]["status"], "failed")
        ex.close.assert_called_once()

    def test_operator_stop_skips_cleanup_commands_but_closes_interface(self):
        ex = Mock()
        stop = threading.Event()
        def interrupt(case):
            stop.set()
            raise ProbeError("operator stopped")
        ex.interrupt.side_effect = interrupt
        ex.check_stop.side_effect = ProbeError("operator stopped")
        result = run_case(INTERRUPTIONS[0], "none", {"operations": []}, stop,
                          lambda: None, factory=lambda *args: ex)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cleanup"]["status"], "skipped")
        ex.cleanup.assert_not_called()
        ex.close.assert_called_once()

    def test_bridge_probe_accepts_fragmented_reply_but_preserves_transfer_errors(self):
        for last_code in (0, -7):
            ex = CancelExperiment({"operations": []}, threading.Event(), lambda: None)
            ex.usb = Mock(timeout_ms=2000)
            raw = packet(b"\x55\xaa")
            ex.usb.transfer_result.side_effect = [(raw[:2], {"return_code": 0}),
                                                   (raw[2:], {"return_code": last_code})]
            ex.usb.check.side_effect = lambda code, label: QueuedUSB().check(code, label)
            if last_code:
                with self.assertRaises(ProbeError):
                    ex.probe_bridge("probe")
                self.assertEqual(ex.report["operations"][0]["outcome"], "failed")
            else:
                ex.probe_bridge("probe")
                self.assertTrue(ex.transport_healthy)
            self.assertEqual(ex.usb.timeout_ms, 2000)
            self.assertEqual(ex.report["operations"][0]["response_hex"], raw.hex(" "))

    def test_public_report_does_not_copy_raw_images_errors_or_hashes(self):
        report = {"completed": True, "cases": [{"case": "a", "strategy": "drain", "status": "passed",
                  "error": "secret", "injection": {"target_hit": True, "hex": "secret"},
                  "followup": {"frames": 3, "sha256": "secret"},
                  "cleanup": {"status": "failed", "error": "secret"}}],
                  "sampling": [{"phase": "hold", "frames": 3, "normal_metrics": "secret", "sha256": "secret"}]}
        public = json.dumps(public_summary(report))
        self.assertNotIn("secret", public)
        self.assertNotIn("sha256", public)
        self.assertNotIn("normal_metrics", public)

    def test_deferred_case_consumes_whole_response_without_cancelling_transport(self):
        report = {"operations": []}
        ex = CancelExperiment(report, threading.Event(), lambda: None)
        raw = packet(bytes(12160))
        ex.usb = Mock()
        ex.usb.transfer.side_effect = [b"command", raw[:4096], raw[4096:]]
        result = ex.interrupt(Case("deferred", NORMAL_FRAME, "deferred"))
        self.assertEqual(result["consumed_bytes"], 12166)
        self.assertTrue(result["target_hit"])
        ex.usb.transfer_result.assert_not_called()
        self.assertEqual(report["operations"][0]["cancellation_requested_after_bytes"], 4096)

    def test_drain_is_bounded_and_restores_transfer_timeout(self):
        for quiet in (True, False):
            ex = CancelExperiment({"operations": []}, threading.Event(), lambda: None)
            ex.usb = Mock(timeout_ms=2000)
            ex.usb.transfer_result.return_value = (b"", {"return_code": -7}) if quiet else (b"x", {"return_code": 0})
            if quiet:
                ex.drain()
            else:
                with self.assertRaises(ProbeError):
                    ex.drain()
            self.assertLessEqual(ex.usb.transfer_result.call_count, 4)
            self.assertEqual(ex.usb.timeout_ms, 2000)

    def test_frame_sampling_checkpoints_separate_wire_records(self):
        ex = Mock(report={"operations": []}, checkpoint=Mock())
        def require(query, label):
            payload = struct.pack("<6080H", *range(6080)) if query.max_data == 12160 else (b"\x05" if query.name == "extended_status" else b"\x01")
            result = {"data_hex": payload.hex(), "response_hex": packet(payload).hex()}
            ex.report["operations"].append({"label": label, **result})
            return result
        ex.require.side_effect = require
        with tempfile.TemporaryDirectory() as directory:
            result = collect_frames(ex, 3, sample_dir=Path(directory))
            self.assertEqual(result["frames"], 3)
            self.assertEqual(result["extended_frames"], 3)
            self.assertEqual(result["distinct_payloads"], 1)
            self.assertEqual(len(list(Path(directory).glob("sample-*.json"))), 3)
            self.assertEqual(ex.report["operations"], [])
            saved = json.loads((Path(directory) / "sample-0001.json").read_text())
            self.assertEqual(len(saved["operations"]), 5)
            self.assertEqual(saved["operations"][saved["sample"]["normal_operation"]]["label"], "sample/normal")
            self.assertEqual((Path(directory) / "sample-0001.json").stat().st_mode & 0o777, 0o600)

    def test_whole_suite_runs_baseline_guided_sampling_and_all_cases_automatically(self):
        instances = []
        def factory(report, stop, checkpoint):
            ex = Mock(report=report, checkpoint=checkpoint, usb=Mock())
            ex.open.side_effect = lambda: report.setdefault("connections", [{"device": {"sysfs": {"path": "/physical/port"}}}])
            def interrupt(case):
                report["operations"].append({"label": "interruption"})
                return {"outcome": "cancelled", "target_hit": True}
            ex.interrupt.side_effect = interrupt
            ex.followup.return_value = {"frames": 3, "distinct_payloads": 3, "health_valid": True}
            instances.append(ex)
            return ex
        sampled = []
        def collect(ex, count, *args):
            sampled.append(ex.report.get("phase", "lift"))
            return {"frames": count, "distinct_payloads": count}
        with tempfile.TemporaryDirectory() as directory:
            with patch("device_validation.CancelExperiment", side_effect=factory), \
                 patch("device_validation.run_case", side_effect=lambda case, strategy, *a: {
                     "case": case.name, "strategy": strategy, "status": "passed"}), \
                 patch("device_testing.TestSession.run_no_finger"), \
                 patch("device_validation.collect_frames", side_effect=collect):
                result = run_suite(Path(directory) / "report.json", threading.Event())
            self.assertTrue(result["completed"], result.get("error"))
            self.assertEqual(len(result["cases"]), len(CASES))
            self.assertEqual(len(result["baseline"]["steps"]), 11)
            self.assertEqual(sampled[-5:], ["light_contact", "normal_pressure", "rolling", "steady_stream", "lift"])
            self.assertEqual(sorted(result["channel_order"]), ["data", "data", "none", "none", "status", "status"])
            self.assertTrue(all(ex.close.called for ex in instances))

    def test_suite_stops_after_cleanup_failure_and_lists_unattempted_cases(self):
        def factory(report, stop, checkpoint):
            ex = Mock(report=report, checkpoint=checkpoint, usb=Mock())
            ex.open.side_effect = lambda: report.setdefault("connections", [{"device": {"sysfs": {"path": "/physical/port"}}}])
            return ex
        failed = {"case": CASES[0][0].name, "strategy": CASES[0][1], "status": "recovery_failed",
                  "failure_stage": "followup", "cleanup": {"status": "failed"}}
        with tempfile.TemporaryDirectory() as directory:
            with patch("device_validation.CancelExperiment", side_effect=factory), \
                 patch("device_validation.run_case", return_value=failed) as run, \
                 patch("device_testing.TestSession.run_no_finger"), \
                 patch("device_validation.collect_frames", return_value={"frames": 3}):
                result = run_suite(Path(directory) / "report.json", threading.Event())
            self.assertFalse(result["completed"])
            run.assert_called_once()
            self.assertEqual(result["cases"], [failed])
            public = public_summary(result)
            self.assertEqual(public["cases"][0]["status"], "recovery_failed")
            self.assertEqual(public["cases"][0]["cleanup"]["status"], "failed")
            self.assertEqual(len(public["unattempted_cases"]), 49)
            self.assertEqual(public["schema_version"], 2)

    def test_parent_stop_escalates_only_after_grace_period_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            session = TestSession(root, "validation")
            session.stop.set()
            process = Mock(returncode=-9)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("worker", 8), -9]
            def start(*args, **kwargs):
                path = session.raw_dir / "validation" / "report.json"
                path.write_text(json.dumps({"completed": False, "cases": [{"case": "normal_in", "status": "cancelled"}]}))
                return process
            row = {}
            with patch("device_testing.subprocess.Popen", start), self.assertRaises(ProbeError):
                session.run_validation(row)
            process.send_signal.assert_called_once()
            process.kill.assert_called_once()
            self.assertEqual(process.wait.call_args_list[0].kwargs, {"timeout": 8})
            self.assertFalse(row["validation"]["completed"])
            self.assertEqual(row["validation"]["cases"][0]["status"], "cancelled")

    def test_session_exports_partial_worker_report_on_failure(self):
        class Process:
            returncode = 1
            def poll(self):
                return 1
        with tempfile.TemporaryDirectory() as root:
            session = TestSession(root, "validation")
            def start(*args, **kwargs):
                path = session.raw_dir / "validation" / "report.json"
                path.write_text(json.dumps({"completed": False, "cases": [{"case": "status_in", "status": "not_hit"}]}))
                return Process()
            with patch("device_testing.subprocess.Popen", start):
                session.run_next()
            self.assertTrue(session.terminal)
            self.assertEqual(session.summary["status"], "failed")
            self.assertEqual(session.summary["steps"][0]["validation"]["cases"][0]["status"], "not_hit")


if __name__ == "__main__":
    unittest.main()
