# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Guards for capability experiments; no hardware access."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from device_capabilities import Experiment, INFO
from probe_device import ProbeError, bcc


def packet(data=b"", status=4):
    body = (len(data) + 1).to_bytes(2, "big") + bytes([status]) + data
    return b"\x02" + body + bytes([bcc(body)])


class FakeUSB:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.writes = []

    def transfer(self, endpoint, payload, events):
        if endpoint == 3:
            self.writes.append(payload)
            return payload
        value = next(self.replies)
        if isinstance(value, BaseException):
            raise value
        return value


class CapabilitiesTest(unittest.TestCase):
    def experiment(self, replies):
        e = Experiment({"operations": []})
        e.usb = FakeUSB(replies)
        e.health = lambda label: None
        return e

    def test_complete_semantic_rejection_allows_next_query(self):
        e = self.experiment([packet(status=9), packet(b"\x55\xbb")])
        with redirect_stdout(io.StringIO()):
            first = e.query(INFO["bridge_uid"])
            second = e.query(INFO["heartbeat"])
        self.assertEqual(first["outcome"], "semantic_rejection")
        self.assertTrue(first["bcc_valid"])
        self.assertEqual(second["data_hex"], "55 bb")

    def test_bad_checksum_blocks_all_subsequent_commands(self):
        e = self.experiment([bytes.fromhex("02 00 01 09 00")])
        with self.assertRaises(ProbeError):
            e.query(INFO["bridge_uid"])
        with self.assertRaises(ProbeError):
            e.query(INFO["heartbeat"])
        self.assertEqual(len(e.usb.writes), 1)

    def test_no_mode_write_if_wake_prerequisite_is_rejected(self):
        e = self.experiment([packet(status=9)])
        with self.assertRaises(ProbeError), redirect_stdout(io.StringIO()):
            e.modes()
        self.assertEqual([p[3] for p in e.usb.writes], [0x82])
        self.assertTrue(all(v == "blocked_recovery_prerequisite_failed"
                            for v in e.report["mode_results"].values()))

    def test_interrupted_transfer_blocks_subsequent_commands(self):
        for error in (KeyboardInterrupt(), OSError("device disappeared")):
            e = self.experiment([error])
            with self.assertRaises(type(error)):
                e.query(INFO["bridge_uid"])
            with self.assertRaises(ProbeError):
                e.query(INFO["heartbeat"])
            self.assertEqual(len(e.usb.writes), 1)

    def test_transport_error_during_mode_does_not_send_blind_restore(self):
        e = self.experiment([packet()] * 4 + [bytes.fromhex("02 00 01 04 ff")])
        with self.assertRaises(ProbeError), patch("device_capabilities.time.sleep"), redirect_stdout(io.StringIO()):
            e.modes()
        self.assertEqual(len(e.usb.writes), 5)
        self.assertEqual(e.report["recovery_not_attempted"], "transport_or_framing_error")

    def test_finger_appearing_aborts_idle_capture_before_frame_request(self):
        e = self.experiment([packet(b"\0"), packet(b"\1")])
        with self.assertRaises(ProbeError), redirect_stdout(io.StringIO()):
            e.capture_idle()
        self.assertEqual([p[3] for p in e.usb.writes], [0x80, 0x80])

    def test_extended_frame_requires_exact_ready_status(self):
        for state, expected_frame in (("06", False), ("05", True), ("", False)):
            e = self.experiment([])
            names = []

            def query(q, label=None):
                names.append(q.name)
                return {"data_hex": state, "outcome": "valid_data" if state else "semantic_rejection"}

            e.query = query
            e.queries()
            self.assertEqual("extended_frame" in names, expected_frame)

    def test_control_stall_does_not_abort_independent_standard_requests(self):
        class Control:
            def __call__(self, handle, request_type, request, value, index, data, size, timeout):
                if request == 10:
                    return -9
                if request == 8:
                    data[0] = 1
                return size

        e = self.experiment([])
        e.usb.handle = None
        e.usb.lib = SimpleNamespace(libusb_control_transfer=Control())
        e.usb.check = lambda code, label: self.assertGreaterEqual(code, 0)
        with redirect_stdout(io.StringIO()):
            e.usb_status()
        ops = e.report["operations"]
        self.assertEqual(len(ops), 5)
        self.assertEqual(ops[2]["outcome"], "control_request_stalled")
        self.assertEqual(ops[-1]["data_hex"], "00 00")

    def test_fault_recovery_resets_before_bulk_and_reopens_same_port(self):
        calls = []
        e = Experiment({"operations": [], "connections": [{"device": {"sysfs": {"path": "/physical/port"}}}]})
        reset = Mock(side_effect=lambda handle: calls.append("reset") or 0)
        usb = Mock()
        usb.lib.libusb_reset_device = reset
        e.usb = usb
        e.close = lambda: calls.append("close")
        e.pause = lambda delay: calls.append("pause")
        e.open = lambda expected_path: calls.append(("open", expected_path))
        e.health = lambda label: calls.append("health")
        e.recover_transport()
        self.assertEqual(calls, ["reset", "close", "pause", ("open", "/physical/port"), "health"])
        usb.transfer.assert_not_called()

    def test_failed_reset_never_sends_health_queries(self):
        e = Experiment({"operations": [], "connections": [{"device": {"sysfs": {"path": "/physical/port"}}}]})
        e.usb = Mock()
        e.usb.lib.libusb_reset_device.return_value = -1
        e.usb.check.side_effect = ProbeError("reset failed")
        e.health = Mock()
        with self.assertRaises(ProbeError):
            e.recover_transport()
        e.health.assert_not_called()
        self.assertFalse(e.transport_healthy)


if __name__ == "__main__":
    unittest.main()
