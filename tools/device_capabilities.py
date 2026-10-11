#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Bounded FT9362 capability experiments. Reports are private and may contain images.

Only documented commands are exposed. No bootloader, firmware, EC/GPIO writes,
arbitrary register access or system power changes are implemented. The explicit
usb-recovery suite clears endpoint halts and resets only the selected USB device.
"""

import argparse
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from probe_device import (EXTENDED_QUERIES, QUERIES, ProbeError, Query, USB, USBError,
                          exchange, open_device)


def command(name, body, size=0):
    return Query(name, bytes.fromhex(body), size, size)


INFO = {q.name: q for q in QUERIES + EXTENDED_QUERIES}
INFO.update({q.name: q for q in (
    command("heartbeat", "35", 2),
    command("main_key_status", "40", 17),
    command("finger_status", "80 02 01", 1),
    command("extended_status", "82 73 01", 1),
    command("sfr_1b", "84 1b", 1),
    command("sfr_1c", "84 1c", 1),
)})
WAKE = command("app_wake", "82 01 00")
INIT_01 = command("init_01", "87 01 01 01")
INIT_BB = command("init_bb", "87 bb 01 bb")
SCAN = command("scan_mode", "87 6c 01 6c")
GESTURE = command("gesture_mode", "87 40 01 40")
DEEP_SLEEP = command("deep_sleep", "87 73 01 73")


class Experiment:
    def __init__(self, report, verbose=True, stop=None):
        self.report = report
        self.verbose = verbose
        self.usb = None
        self.transport_healthy = True
        self.stop = stop

    def check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise ProbeError("operator stopped the experiment")

    def pause(self, seconds):
        if self.stop is None:
            time.sleep(seconds)
        else:
            self.stop.wait(seconds)
            self.check_stop()

    def open(self, expected_path=None):
        self.check_stop()
        record = {}
        self.report.setdefault("connections", []).append(record)
        self.usb = open_device(record, 2000, expected_path=expected_path)
        self.transport_healthy = True

    def close(self):
        if self.usb is not None:
            errors = self.usb.close()
            self.usb = None
            self.report["connections"][-1]["release_errors"] = errors
            if errors:
                raise ProbeError(f"interface release failed: {errors}")

    def query(self, query, label=None):
        self.check_stop()
        if not self.transport_healthy:
            raise ProbeError("transport state is uncertain; no further command is permitted")
        result = {"label": label or query.name,
                  "started_utc": datetime.now(timezone.utc).isoformat()}
        self.report["operations"].append(result)
        try:
            exchange(self.usb, query, result)
            result["outcome"] = "valid_data"
        except ProbeError as error:
            result.update(error=str(error), outcome="failed")
            # A fully received, checksum-valid semantic rejection is evidence
            # about this command. Transport/framing errors stop the experiment.
            if not result.get("bcc_valid"):
                self.transport_healthy = False
                raise
            result["outcome"] = "semantic_rejection"
        except (OSError, KeyboardInterrupt) as error:
            self.transport_healthy = False
            result.update(error=str(error) or "interrupted", outcome="failed")
            raise
        finally:
            result["finished_utc"] = datetime.now(timezone.utc).isoformat()
        data = bytes.fromhex(result.get("data_hex", ""))
        if len(data) > 100:
            result["data_sha256"] = hashlib.sha256(data).hexdigest()
            # Full hex stays in the private report; don't print raw biometrics.
            result.pop("value", None)
        if self.verbose:
            print(f"{result['label']}: {result['outcome']} status={result.get('status')} "
                  f"data={len(data)} bytes trailer={result.get('trailing_hex')} "
                  f"{result.get('error', '')}", flush=True)
        return result

    def require(self, query, label=None):
        result = self.query(query, label)
        if result["outcome"] != "valid_data":
            raise ProbeError(f"required operation failed: {result['label']}")
        return result

    def raw_wake(self):
        self.check_stop()
        result = {"label": "bridge_wake_raw_00", "transfers": []}
        self.report["operations"].append(result)
        self.usb.transfer(3, b"\0", result["transfers"])
        self.pause(0.002)
        result["outcome"] = "sent_no_ack_expected"

    def health(self, label):
        bridge = self.require(INFO["bridge_status"], label + "/bridge")
        app = self.require(INFO["app_status"], label + "/app")
        a = self.require(INFO["dimension_a"], label + "/a")
        b = self.require(INFO["dimension_b"], label + "/b")
        if (bridge["data_hex"], app["data_hex"], a["data_hex"], b["data_hex"]) != (
                "55 aa", "a5 5a", "28", "98"):
            raise ProbeError("baseline readiness/dimensions changed")

    def queries(self):
        self.health("baseline")
        for query in INFO.values():
            self.query(query)
        # The Windows GetMoreData entry only reads a frame for status 05.
        status = self.query(INFO["extended_status"], "extended_capture/gate")
        if status.get("data_hex") == "05" and status["outcome"] == "valid_data":
            self.query(command("extended_frame", "82 76 01", 12160))
        else:
            self.report["extended_capture"] = "not_run_status_gate_not_05"
        self.health("after_queries")

    def wake(self):
        self.health("baseline")
        self.raw_wake()
        result = self.query(INFO["heartbeat"], "heartbeat_with_documented_preamble")
        result["expected_heartbeat"] = result.get("data_hex") == "55 bb"
        self.health("after_bridge_wake")
        self.require(WAKE)
        self.pause(0.005)
        self.require(WAKE, "app_wake_second")
        self.pause(0.010)
        self.health("after_app_wake")
        self.query(INFO["sfr_1b"], "awake_sfr_1b")
        self.query(INFO["sfr_1c"], "awake_sfr_1c")

    def restore(self):
        # The documented chip-init primitives. This does not claim to perform
        # Windows SENSOR mode: its indirect-read index remains unknown.
        self.require(WAKE, "restore/wake")
        self.pause(0.010)
        self.require(INIT_01, "restore/init_01")
        self.require(INIT_BB, "restore/init_bb")
        self.health("restore/health")

    def modes(self):
        self.health("baseline")
        self.report["mode_results"] = {q.name: "not_run" for q in (SCAN, GESTURE, DEEP_SLEEP)}
        try:
            self.restore()  # Check recovery primitives before entering any mode.
        except ProbeError:
            self.report["mode_results"] = {q.name: "blocked_recovery_prerequisite_failed"
                                           for q in (SCAN, GESTURE, DEEP_SLEEP)}
            raise
        for query in (SCAN, GESTURE, DEEP_SLEEP):
            try:
                self.require(WAKE, query.name + "/pre_wake")
                self.require(query)
                self.pause(0.100)
                self.query(INFO["app_status"], query.name + "/app")
                self.query(INFO["finger_status"], query.name + "/finger")
                self.query(INFO["extended_status"], query.name + "/extended")
                self.report["mode_results"][query.name] = "acknowledged_effect_not_proven"
            finally:
                if self.transport_healthy:
                    self.restore()
                else:
                    self.report["recovery_not_attempted"] = "transport_or_framing_error"

    def capture_idle(self):
        self.health("baseline")
        status = self.require(INFO["finger_status"])
        if status["data_hex"] != "00":
            raise ProbeError("no-finger experiment requires status 00; no frame command sent")
        for index in range(3):
            status = self.require(INFO["finger_status"], f"idle_{index}/finger")
            if status["data_hex"] != "00":
                raise ProbeError("finger status changed; stopping the idle capture experiment")
            self.query(command("raw_frame", "81", 12160), f"idle_{index}/frame")
            self.pause(0.100)
        self.health("after_idle_capture")

    def reopen(self):
        for delay in (0, 0.5, 3, 6):
            self.health(f"before_close_{delay}s")
            self.close()
            self.pause(delay)
            self.open()
            self.health(f"after_reopen_{delay}s")

    def usb_recovery(self):
        self.health("baseline")
        clear = self.usb.lib.libusb_clear_halt
        clear.argtypes, clear.restype = [C.c_void_p, C.c_ubyte], C.c_int
        for endpoint in (0x81, 0x03):
            self.check_stop()
            code = clear(self.usb.handle, endpoint)
            self.report["operations"].append({"label": f"clear_halt_{endpoint:02x}", "return_code": code})
            self.usb.check(code, "clear endpoint halt")
            self.health(f"after_clear_{endpoint:02x}")
        reset = self.usb.lib.libusb_reset_device
        self.check_stop()
        reset.argtypes, reset.restype = [C.c_void_p], C.c_int
        code = reset(self.usb.handle)
        self.report["operations"].append({"label": "reset_selected_usb_device", "return_code": code})
        if code == -4:
            # A reset can invalidate the old handle. No interface remains to
            # release in that case; close it and discover the device afresh.
            self.usb.claimed = False
        else:
            self.usb.check(code, "reset selected USB device")
        self.close()
        self.pause(0.6)
        self.open()
        self.health("after_usb_reset")
        self.query(INFO["controller_version"], "after_usb_reset/version")
        self.query(INFO["finger_status"], "after_usb_reset/finger")

    def recover_transport(self):
        """Explicit recovery after a failed transfer; no bulk command before reset."""
        self.check_stop()
        self.transport_healthy = False
        path = (self.report["connections"][-1]["device"].get("sysfs") or {}).get("path")
        if not path:
            raise ProbeError("cannot verify physical USB path for recovery")
        reset = self.usb.lib.libusb_reset_device
        reset.argtypes, reset.restype = [C.c_void_p], C.c_int
        code = reset(self.usb.handle)
        self.report["operations"].append({"label": "recovery/reset_selected_usb_device", "return_code": code})
        if code == -4:
            self.usb.claimed = False
        else:
            self.usb.check(code, "reset selected USB device for recovery")
        self.close()
        self.pause(0.6)
        self.open(expected_path=path)
        self.health("recovery/health")

    def usb_status(self):
        self.health("baseline")
        control = self.usb.lib.libusb_control_transfer
        control.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8, C.c_uint16, C.c_uint16,
                            C.POINTER(C.c_ubyte), C.c_uint16, C.c_uint]
        control.restype = C.c_int
        # USB standard GET_STATUS, GET_CONFIGURATION and GET_INTERFACE only.
        for label, request_type, request, index, size in (
                ("device_status", 0x80, 0, 0, 2),
                ("configuration", 0x80, 8, 0, 1),
                ("interface_alt_setting", 0x81, 10, 0, 1),
                ("out_endpoint_status", 0x82, 0, 3, 2),
                ("in_endpoint_status", 0x82, 0, 0x81, 2)):
            data = (C.c_ubyte * size)()
            code = control(self.usb.handle, request_type, request, 0, index, data, size, 2000)
            result = {"label": label, "request_type": request_type, "request": request,
                      "index": index, "return_code": code,
                      "data_hex": bytes(data[:max(0, code)]).hex(" ")}
            self.report["operations"].append(result)
            if code == -9:
                # Endpoint-zero STALL ends this control request. A new SETUP
                # starts a separate control transaction; no bulk reply is pending.
                result["outcome"] = "control_request_stalled"
                print(f"{label}: control request STALL", flush=True)
                continue
            self.usb.check(code, label)
            if code != size:
                raise ProbeError(f"short USB standard response for {label}: {code}/{size}")
            print(f"{label}: {result['data_hex']}", flush=True)
        self.health("after_standard_requests")

    def ownership(self):
        self.health("baseline")
        second = USB(2000)
        metadata = {}
        result = {"label": "second_handle_claim", "device": metadata}
        self.report["operations"].append(result)
        try:
            second.open(metadata, self.report["connections"][-1]["device"]["sysfs"]["path"])
            raise ProbeError("a second USB handle unexpectedly claimed the interface")
        except USBError as error:
            result.update(error=str(error), return_code=error.code)
            if error.operation != "claim interface 0" or error.code != -6:
                raise
            result["outcome"] = "exclusive_claim_confirmed"
        finally:
            result["release_errors"] = second.close()
        self.health("original_handle_after_conflicting_claim")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("queries", "wake", "modes", "capture-idle", "reopen",
                                          "usb-recovery", "usb-status", "ownership"))
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build" / "diagnostics")
    args = parser.parse_args(argv)
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.output / f"capabilities-{args.suite}-{uuid.uuid4().hex}.json"
    report = {"schema_version": 1, "suite": args.suite,
              "started_utc": datetime.now(timezone.utc).isoformat(), "operations": []}
    experiment = Experiment(report)
    code = 0
    with path.open("x", encoding="utf-8") as output:
        try:
            experiment.open()
            getattr(experiment, args.suite.replace("-", "_"))()
            report["completed"] = True
        except (ProbeError, OSError, KeyboardInterrupt) as error:
            report.update(completed=False, error=str(error) or "interrupted")
            print(report["error"], file=sys.stderr)
            code = 130 if isinstance(error, KeyboardInterrupt) else 1
        finally:
            try:
                experiment.close()
            except ProbeError as error:
                report.update(completed=False, close_error=str(error))
                code = 1
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    print(f"Report: {path}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
