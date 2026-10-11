# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Protocol boundary/error tests for the hardware probe; no sensor required."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from probe_device import (EXTENDED_QUERIES, QUERIES, ProbeError, USBError, bcc,
                          encode_request, exchange, interpret, parse_response,
                          open_device, run_queries)


def response(data, status=4, trailer=b""):
    content = (len(data) + 1).to_bytes(2, "big") + bytes([status]) + data
    return b"\x02" + content + bytes([bcc(content)]) + trailer


class FakeUSB:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.writes = []
        self.read_count = 0

    def transfer(self, endpoint, payload, events):
        if endpoint == 3:
            self.writes.append(payload)
            return payload
        self.read_count += 1
        value = next(self.chunks)
        if isinstance(value, Exception):
            raise value
        return value


class ProbeTest(unittest.TestCase):
    def test_requests_match_recovered_windows_commands(self):
        expected = ["0200013435", "0200013031", "02000380010280",
                    "02000380030181", "02000380040186", "02000380070185",
                    "0200013d3c", "0200013f3e"]
        self.assertEqual([encode_request(q).hex() for q in QUERIES + EXTENDED_QUERIES], expected)

    def test_logical_packet_and_trailer_are_separate(self):
        for trailer in (b"", b"\xf8", bytes(57)):
            result = parse_response(response(b"\x28", trailer=trailer), QUERIES[3])
            self.assertEqual(result["logical_length"], 6)
            self.assertEqual(result["data_hex"], "28")
            self.assertEqual(result["trailing_hex"], trailer.hex(" "))
        # A checksum failure cannot be hidden by an extra correct byte at the end.
        bad = bytearray(response(b"\x28"))
        bad[-1] ^= 1
        with self.assertRaisesRegex(ProbeError, "BCC"):
            parse_response(bad + b"\x2e", QUERIES[3])

    def test_rejects_malformed_headers_lengths_and_device_errors(self):
        good = response(b"\x55\xaa")
        bad_packets = [b"", good[:2], good[:-1], b"\x03" + good[1:],
                       bytes.fromhex("02 00 00 04 00"),
                       bytes.fromhex("02 ff ff 04 00"), response(b"\x55"),
                       response(b"", status=0xe0)]
        for packet in bad_packets:
            with self.subTest(packet=packet.hex()), self.assertRaises(ProbeError):
                parse_response(packet, QUERIES[0])

    def test_fragmented_packet_and_bounded_reads(self):
        packet = response(b"\x55\xaa", trailer=b"\x99")
        usb = FakeUSB([packet[:2], packet[2:5], packet[5:]])
        result = {}
        exchange(usb, QUERIES[0], result)
        self.assertEqual(result["value"], {"ready": True})
        self.assertEqual(result["trailing_hex"], "99")
        self.assertEqual(usb.read_count, 3)
        for chunks in ([b""], [b"\x02", b"\x00", b"\x03", b"\x04"],
                       [response(b"\x55\xaa") + bytes(64)]):
            with self.assertRaises(ProbeError):
                exchange(FakeUSB(chunks), QUERIES[0], {})

    def test_failure_aborts_without_sending_next_query(self):
        for failure in (ProbeError("USB timeout"), response(b"", status=0xe0),
                        bytes.fromhex("02 00 03 04 55 aa 00")):
            usb = FakeUSB([failure])
            results = []
            with self.assertRaises(ProbeError), redirect_stdout(io.StringIO()):
                run_queries(usb, QUERIES, results)
            self.assertEqual(usb.writes, [encode_request(QUERIES[0])])
            self.assertEqual(len(results), 1)
            self.assertIn("error", results[0])

    def test_unrecognized_flags_are_not_treated_as_unencrypted(self):
        self.assertEqual(interpret(QUERIES[5], b"\0"), {"flag": 0, "enabled": False})
        self.assertEqual(interpret(QUERIES[5], b"\1"), {"flag": 1, "enabled": True})
        self.assertEqual(interpret(QUERIES[5], b"\2"), {"flag": 2, "enabled": None})
        self.assertEqual(interpret(EXTENDED_QUERIES[1], b"\x01\x02"), {"raw_hex": "01 02"})

    def test_error_response_still_records_valid_checksum_and_trailer(self):
        for query, packet, status in (
                (QUERIES[5], bytes.fromhex("02 00 01 04 05 93"), 4),
                (EXTENDED_QUERIES[0], bytes.fromhex("02 00 01 09 08 9e"), 9)):
            result = {}
            with self.assertRaises(ProbeError):
                exchange(FakeUSB([packet]), query, result)
            self.assertTrue(result["bcc_valid"])
            self.assertEqual(result["status"], status)
            self.assertEqual(result["logical_length"], 5)
            self.assertEqual(result["data_hex"], "")
            self.assertNotIn("value", result)

    def test_reopen_is_bounded_and_only_before_queries(self):
        class FakeOpen:
            def __init__(self, error):
                self.error, self.closed, self.expected_path = error, False, None

            def open(self, metadata, expected_path):
                self.expected_path = expected_path
                metadata["sysfs"] = {"path": "/physical/port"}
                if self.error:
                    raise self.error

            def close(self):
                self.closed = True
                return []

        for error, retried in ((USBError("open USB", -1, "IO"), True),
                               (USBError("open USB", -4, "NO_DEVICE"), True),
                               (USBError("open USB", -3, "ACCESS"), False),
                               (USBError("claim interface 0", -6, "BUSY"), False)):
            first, second = FakeOpen(error), FakeOpen(None)
            instances = iter([first, second])
            report, pauses = {}, []
            with redirect_stdout(io.StringIO()):
                if retried:
                    self.assertIs(open_device(report, 2000, lambda _: next(instances), pauses.append), second)
                    self.assertEqual(second.expected_path, "/physical/port")
                else:
                    with self.assertRaises(USBError):
                        open_device(report, 2000, lambda _: next(instances), pauses.append)
            self.assertTrue(first.closed)
            self.assertEqual(pauses, [0.6] if retried else [])
        instances = [FakeOpen(USBError("open USB", -1, "IO")) for _ in range(2)]
        source = iter(instances)
        with self.assertRaises(USBError), redirect_stdout(io.StringIO()):
            open_device({}, 2000, lambda _: next(source), lambda _: None)
        self.assertTrue(all(instance.closed for instance in instances))


if __name__ == "__main__":
    unittest.main()
