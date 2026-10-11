#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Query known FT9362 information commands; no capture, reset or mode writes.

Uses Python's standard library and libusb-1.0. Run inside `nix develop`.
The private JSON report includes exact USB bytes, including unknown trailers.
"""

import argparse
import ctypes as C
import ctypes.util
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import reduce
import json
import operator
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


@dataclass(frozen=True)
class Query:
    name: str
    body: bytes
    min_data: int
    max_data: int


QUERIES = (
    Query("bridge_status", bytes.fromhex("34"), 2, 2),
    Query("controller_version", bytes.fromhex("30"), 1, 63),
    Query("app_status", bytes.fromhex("80 01 02"), 2, 2),
    Query("dimension_a", bytes.fromhex("80 03 01"), 1, 1),
    Query("dimension_b", bytes.fromhex("80 04 01"), 1, 1),
    Query("image_encryption", bytes.fromhex("80 07 01"), 1, 1),
)
EXTENDED_QUERIES = (
    Query("bridge_uid", bytes.fromhex("3d"), 12, 12),
    Query("upgrade_count", bytes.fromhex("3f"), 2, 2),
)


class ProbeError(RuntimeError):
    pass


class USBError(ProbeError):
    def __init__(self, operation, code, name):
        super().__init__(f"{operation}: {name} ({code})")
        self.operation, self.code = operation, code


def bcc(data):
    return reduce(operator.xor, data, 0)


def encode_request(query):
    content = len(query.body).to_bytes(2, "big") + query.body
    return b"\x02" + content + bytes([bcc(content)])


def response_size(raw, max_data):
    if raw and raw[0] != 2:
        raise ProbeError("response does not start with 02")
    if len(raw) < 3:
        return None
    length = int.from_bytes(raw[1:3], "big")
    if not 1 <= length <= max_data + 1:
        raise ProbeError(f"invalid response content length: {length}")
    return length + 4


def decode_response(raw, max_data):
    size = response_size(raw, max_data)
    if size is None or len(raw) < size:
        raise ProbeError("truncated response")
    if bcc(raw[1:size - 1]) != raw[size - 1]:
        raise ProbeError("response BCC mismatch")
    data = raw[4:size - 1]
    return {"logical_length": size, "status": raw[3], "bcc_valid": True,
            "data_hex": data.hex(" "), "trailing_hex": raw[size:].hex(" ")}


def validate_response(result, query):
    if result["status"] != 4:
        raise ProbeError(f"device returned status 0x{result['status']:02x}")
    size = len(bytes.fromhex(result["data_hex"]))
    if not query.min_data <= size <= query.max_data:
        raise ProbeError(f"unexpected data length: {size}")


def parse_response(raw, query):
    result = decode_response(raw, query.max_data)
    validate_response(result, query)
    return result


def interpret(query, data):
    if query.name == "controller_version":
        return data.rstrip(b"\0").decode("ascii", errors="backslashreplace")
    if query.name in ("dimension_a", "dimension_b"):
        return data[0]
    if query.name == "bridge_status":
        return {"ready": data == b"\x55\xaa"}
    if query.name == "app_status":
        return {"ready": data == b"\xa5\x5a"}
    if query.name == "image_encryption":
        return {"flag": data[0], "enabled": {0: False, 1: True}.get(data[0])}
    # The bridge UID's uniqueness and counter byte order are not established.
    return {"raw_hex": data.hex(" ")}


class Descriptor(C.Structure):
    _fields_ = [("length", C.c_uint8), ("type", C.c_uint8),
                ("usb", C.c_uint16), ("device_class", C.c_uint8),
                ("subclass", C.c_uint8), ("protocol", C.c_uint8),
                ("max_packet", C.c_uint8), ("vendor", C.c_uint16),
                ("product", C.c_uint16), ("version", C.c_uint16),
                ("manufacturer", C.c_uint8), ("product_string", C.c_uint8),
                ("serial", C.c_uint8), ("configurations", C.c_uint8)]


def load_libusb():
    name = C.util.find_library("usb-1.0") or "libusb-1.0.so.0"
    try:
        return C.CDLL(name)
    except OSError as error:
        # Nix exposes build dependencies to pkg-config without necessarily
        # placing their shared libraries on the Python loader's search path.
        try:
            directory = subprocess.check_output(
                ["pkg-config", "--variable=libdir", "libusb-1.0"],
                text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            raise ProbeError(f"cannot load libusb; run inside nix develop: {error}") from error
        if not directory or not Path(directory).is_absolute():
            raise ProbeError("pkg-config did not return an absolute libusb directory")
        return C.CDLL(str(Path(directory) / "libusb-1.0.so.0"))


class USB:
    """Exclusive interface claim; never detach a kernel driver or reset USB."""

    def __init__(self, timeout_ms):
        self.timeout_ms = timeout_ms
        self.context = C.c_void_p()
        self.handle = C.c_void_p()
        self.claimed = False
        self.initialized = False
        self.lib = load_libusb()
        pointer = C.c_void_p
        specs = {
            "init": ([C.POINTER(pointer)], C.c_int),
            "exit": ([pointer], None),
            "get_device_list": ([pointer, C.POINTER(C.POINTER(pointer))], C.c_ssize_t),
            "free_device_list": ([C.POINTER(pointer), C.c_int], None),
            "get_device_descriptor": ([pointer, C.POINTER(Descriptor)], C.c_int),
            "get_bus_number": ([pointer], C.c_uint8),
            "get_device_address": ([pointer], C.c_uint8),
            "open": ([pointer, C.POINTER(pointer)], C.c_int),
            "close": ([pointer], None),
            "claim_interface": ([pointer, C.c_int], C.c_int),
            "release_interface": ([pointer, C.c_int], C.c_int),
            "bulk_transfer": ([pointer, C.c_uint8, C.POINTER(C.c_ubyte),
                               C.c_int, C.POINTER(C.c_int), C.c_uint], C.c_int),
            "error_name": ([C.c_int], C.c_char_p),
        }
        for name, (args, result) in specs.items():
            function = getattr(self.lib, "libusb_" + name)
            function.argtypes, function.restype = args, result

    def check(self, code, operation):
        if code < 0:
            name = self.lib.libusb_error_name(code).decode("ascii")
            raise USBError(operation, code, name)

    def open(self, metadata, expected_path=None):
        self.check(self.lib.libusb_init(C.byref(self.context)), "libusb_init")
        self.initialized = True
        devices = C.POINTER(C.c_void_p)()
        count = self.lib.libusb_get_device_list(self.context, C.byref(devices))
        self.check(count, "enumerate USB")
        try:
            matches = []
            for i in range(count):
                descriptor = Descriptor()
                code = self.lib.libusb_get_device_descriptor(devices[i], C.byref(descriptor))
                if code == 0 and (descriptor.vendor, descriptor.product) == (0x2808, 0x0752):
                    matches.append((devices[i], descriptor))
            if len(matches) != 1:
                raise ProbeError(f"expected exactly one 2808:0752 device, found {len(matches)}")
            device, descriptor = matches[0]
            bus = self.lib.libusb_get_bus_number(device)
            address = self.lib.libusb_get_device_address(device)
            metadata.update(bus=bus, address=address, vid="2808", pid="0752",
                            bcd_device=f"{descriptor.version:04x}",
                            serial_string_index=descriptor.serial)
            metadata["sysfs"] = read_sysfs(bus, address)
            if expected_path and (metadata["sysfs"] or {}).get("path") != expected_path:
                raise ProbeError("USB physical path changed during reopen")
            self.check(self.lib.libusb_open(device, C.byref(self.handle)), "open USB")
        finally:
            self.lib.libusb_free_device_list(devices, 1)
        self.check(self.lib.libusb_claim_interface(self.handle, 0), "claim interface 0")
        self.claimed = True

    def transfer(self, endpoint, payload, events):
        # OUT takes bytes, IN takes a capacity. Keep partial bytes even on errors.
        is_read = bool(endpoint & 0x80)
        capacity = payload if is_read else len(payload)
        buffer = (C.c_ubyte * capacity)()
        if not is_read:
            buffer[:] = payload
        actual = C.c_int()
        started = time.monotonic_ns()
        code = self.lib.libusb_bulk_transfer(self.handle, endpoint, buffer, capacity,
                                            C.byref(actual), self.timeout_ms)
        data = bytes(buffer[:actual.value])
        events.append({"endpoint": f"{endpoint:02x}", "requested_length": capacity,
                       "actual_length": actual.value, "return_code": code,
                       "elapsed_ms": (time.monotonic_ns() - started) / 1e6,
                       "hex": data.hex(" ")})
        self.check(code, f"bulk {endpoint:02x}")
        if not is_read and actual.value != capacity:
            raise ProbeError(f"short write: {actual.value}/{capacity}")
        return data

    def close(self):
        errors = []
        if self.claimed:
            code = self.lib.libusb_release_interface(self.handle, 0)
            if code < 0:
                errors.append(self.lib.libusb_error_name(code).decode("ascii"))
            self.claimed = False
        if self.handle:
            self.lib.libusb_close(self.handle)
            self.handle = C.c_void_p()
        if self.initialized:
            self.lib.libusb_exit(self.context)
            self.initialized = False
        return errors


def read_sysfs(bus, address):
    for path in Path("/sys/bus/usb/devices").iterdir():
        try:
            if (int((path / "busnum").read_text()), int((path / "devnum").read_text())) != (bus, address):
                continue
            info = {"path": str(path.resolve())}
            for key in ("manufacturer", "product", "serial", "speed", "version",
                        "bcdDevice", "bNumConfigurations", "bConfigurationValue", "bNumInterfaces",
                        "bmAttributes", "bMaxPower", "power/control", "power/runtime_status",
                        "power/autosuspend_delay_ms", "power/persist", "power/wakeup"):
                file = path / key
                info[key] = file.read_text().strip() if file.exists() else None
            info["descriptors_hex"] = (path / "descriptors").read_bytes().hex(" ")
            info["endpoints"] = []
            for ep in sorted(path.glob("*:*/ep_*")):
                info["endpoints"].append({key: (ep / key).read_text().strip()
                                          for key in ("bEndpointAddress", "bmAttributes", "wMaxPacketSize")})
            return info
        except (OSError, ValueError):
            continue
    return None


def exchange(usb, query, result):
    result.update(query=query.name, request_hex=encode_request(query).hex(" "), transfers=[])
    usb.transfer(0x03, encode_request(query), result["transfers"])
    raw = bytearray()
    # Maximum known response plus a full packet of possible padding. Bound both
    # allocation and fragment count. Never retry a command or drain the endpoint.
    capacity = ((query.max_data + 5 + 63) // 64) * 64
    for _ in range(4):
        chunk = usb.transfer(0x81, capacity, result["transfers"])
        raw.extend(chunk)
        result["response_hex"] = raw.hex(" ")
        if not chunk:
            raise ProbeError("zero-length USB response")
        if len(raw) > capacity:
            raise ProbeError("response exceeds bounded buffer capacity")
        size = response_size(raw, query.max_data)
        if size is not None and len(raw) >= size:
            result.update(decode_response(raw, query.max_data))
            validate_response(result, query)
            result["value"] = interpret(query, bytes.fromhex(result["data_hex"]))
            return
    raise ProbeError("response still incomplete after 4 fragments")


def run_queries(usb, queries, results):
    for query in queries:
        result = {}
        results.append(result)
        try:
            exchange(usb, query, result)
        except (ProbeError, OSError, KeyboardInterrupt) as error:
            result["error"] = str(error) or "interrupted"
            raise
        value = "<saved in private report>" if query.name == "bridge_uid" else result["value"]
        print(f"{query.name}: {value}; logical={result['logical_length']}, "
              f"trailer={result['trailing_hex'] or '(none)'}", flush=True)


def open_device(report, timeout_ms, factory=USB, pause=time.sleep):
    expected_path = None
    for attempt in range(2):
        usb = factory(timeout_ms)
        metadata = {}
        record = {"device": metadata}
        report.setdefault("open_attempts", []).append(record)
        try:
            usb.open(metadata, expected_path)
        except (ProbeError, OSError, KeyboardInterrupt) as error:
            record.update(error=str(error) or "interrupted", release_errors=usb.close())
            expected_path = (metadata.get("sysfs") or {}).get("path")
            # A suspended device was observed disconnecting during open().
            # Re-enumerate once at the same physical port, before any OUT query.
            if (attempt == 0 and isinstance(error, USBError)
                    and error.operation == "open USB" and error.code in (-1, -4)
                    and expected_path and not record["release_errors"]):
                print("USB open failed; re-enumerating the same port once in 600 ms", flush=True)
                pause(0.6)
                continue
            raise
        report["device"] = metadata
        return usb


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build" / "diagnostics")
    parser.add_argument("--timeout-ms", type=positive, default=2000)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--extended", action="store_true", help="also query bridge UID and upgrade count")
    selection.add_argument("--query", action="append",
                           choices=[q.name for q in QUERIES + EXTENDED_QUERIES],
                           help="run only this known query; repeat to specify an ordered sequence")
    args = parser.parse_args(argv)
    if args.timeout_ms > 10000:
        parser.error("--timeout-ms must not exceed 10000")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.output / f"probe-{uuid.uuid4().hex}.json"
    available = {q.name: q for q in QUERIES + EXTENDED_QUERIES}
    if args.query:
        selected = tuple(available[name] for name in args.query)
    elif args.extended:
        selected = QUERIES[:-1] + EXTENDED_QUERIES + QUERIES[-1:]
    else:
        selected = QUERIES
    report = {"schema_version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
              "timeout_ms": args.timeout_ms, "selected_queries": [q.name for q in selected],
              "device": {}, "queries": []}
    usb = None
    exit_code = 0
    # Reserve the report before touching hardware so output errors fail early.
    with path.open("x", encoding="utf-8") as output:
        try:
            usb = open_device(report, args.timeout_ms)
            run_queries(usb, selected, report["queries"])
            report["success"] = True
        except (ProbeError, OSError, KeyboardInterrupt) as error:
            report.update(success=False, error=str(error) or "interrupted")
            print(report["error"], file=sys.stderr)
            exit_code = 130 if isinstance(error, KeyboardInterrupt) else 1
        finally:
            report["release_errors"] = usb.close() if usb is not None else []
            if report["release_errors"]:
                report["success"] = False
                exit_code = 1
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    print(f"Report: {path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
