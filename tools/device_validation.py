# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""One-shot protocol validation. Invoked by the device-test worker only.

The asynchronous transport runs in a subprocess: a wedged USB backend cannot
prevent the GUI watchdog from stopping the experiment. Existing baseline tests
gate documented sensor modes on successful initialization/recovery prerequisites.
No firmware or system power setting is changed.
"""

import argparse
import ctypes as C
from dataclasses import dataclass
import json
import hashlib
import random
import statistics
import struct
import os
from pathlib import Path
import signal
import threading
import time

from device_capabilities import Experiment, INFO, command
from probe_device import USB, Query, ProbeError, encode_request, open_device, parse_response


NORMAL_FRAME = command("raw_frame", "81", 12160)
EXTENDED_FRAME = command("extended_frame", "82 76 01", 12160)
STRATEGIES = ("none", "drain", "clear_halt", "reopen", "reset")
STRATEGY_NAMES = {"none": "直接重新开始", "drain": "有界读取残留", "clear_halt": "仅清除端点 halt",
                  "reopen": "仅关闭重开", "reset": "单设备 USB 复位"}


@dataclass(frozen=True)
class Case:
    name: str
    query: Query
    point: str
    delay_ms: int = 0


INTERRUPTIONS = (
    Case("status_out", INFO["finger_status"], "out"),
    Case("status_in", INFO["finger_status"], "in"),
    Case("normal_out", NORMAL_FRAME, "out"),
    Case("normal_in_1ms", NORMAL_FRAME, "in", 1),
    Case("normal_partial", NORMAL_FRAME, "partial"),
    Case("extended_out", EXTENDED_FRAME, "out"),
    Case("extended_in_1ms", EXTENDED_FRAME, "in", 1),
    Case("extended_in_50ms", EXTENDED_FRAME, "in", 50),
    Case("extended_partial", EXTENDED_FRAME, "partial"),
)
CONTROLS = (
    Case("before_submit", NORMAL_FRAME, "before"),
    Case("normal_complete", NORMAL_FRAME, "complete"),
    Case("extended_complete", EXTENDED_FRAME, "complete"),
    Case("normal_deferred", NORMAL_FRAME, "deferred"),
    Case("extended_deferred", EXTENDED_FRAME, "deferred"),
)
CASES = tuple((case, "none") for case in CONTROLS) + tuple(
    (case, strategy) for case in INTERRUPTIONS for strategy in STRATEGIES)


def save(path, value):
    # Lazy import avoids a circular dependency when TestSession loads this plan.
    from device_testing import save_json
    save_json(path, value)


class Transfer(C.Structure):
    pass


Callback = C.CFUNCTYPE(None, C.POINTER(Transfer))
Transfer._fields_ = [("dev_handle", C.c_void_p), ("flags", C.c_uint8), ("endpoint", C.c_uint8),
                    ("type", C.c_uint8), ("timeout", C.c_uint), ("status", C.c_int),
                    ("length", C.c_int), ("actual_length", C.c_int), ("callback", Callback),
                    ("user_data", C.c_void_p), ("buffer", C.POINTER(C.c_ubyte)),
                    ("num_iso_packets", C.c_int)]


class Timeval(C.Structure):
    _fields_ = [("seconds", C.c_long), ("microseconds", C.c_long)]


class AsyncUSB(USB):
    def __init__(self, timeout_ms):
        super().__init__(timeout_ms)
        pointer = C.POINTER(Transfer)
        specs = {"alloc_transfer": ([C.c_int], pointer), "free_transfer": ([pointer], None),
                 "submit_transfer": ([pointer], C.c_int), "cancel_transfer": ([pointer], C.c_int),
                 "handle_events_timeout_completed": ([C.c_void_p, C.POINTER(Timeval), C.POINTER(C.c_int)], C.c_int)}
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, "libusb_" + name)
            fn.argtypes, fn.restype = args, result
        self.stop = threading.Event()
        self.checkpoint = lambda: None

    def transfer_result(self, endpoint, payload, events, cancel_after_ms=None):
        capacity = payload if endpoint & 0x80 else len(payload)
        buffer = (C.c_ubyte * capacity)()
        if not endpoint & 0x80:
            buffer[:] = payload
        done = C.c_int()
        callback = Callback(lambda _: setattr(done, "value", 1))
        transfer = self.lib.libusb_alloc_transfer(0)
        if not transfer:
            raise ProbeError("cannot allocate USB transfer")
        event = {"endpoint": f"{endpoint:02x}", "requested_length": capacity,
                 "cancel_after_ms": cancel_after_ms, "timeout_ms": self.timeout_ms}
        events.append(event)
        submitted = False
        try:
            transfer.contents.dev_handle = self.handle
            transfer.contents.endpoint = endpoint
            transfer.contents.type = 2  # LIBUSB_TRANSFER_TYPE_BULK
            transfer.contents.timeout = self.timeout_ms
            transfer.contents.length = capacity
            transfer.contents.buffer = buffer
            transfer.contents.callback = callback
            self.checkpoint()
            started = time.monotonic()
            event["submitted_monotonic_ns"] = int(started * 1e9)
            code = self.lib.libusb_submit_transfer(transfer)
            event["submit_code"] = code
            self.check(code, "submit asynchronous transfer")
            submitted = True
            cancel_at = None if cancel_after_ms is None else started + cancel_after_ms / 1000
            while not done.value:
                now = time.monotonic()
                if "cancel_code" not in event and (self.stop.is_set() or (cancel_at is not None and now >= cancel_at)):
                    event["cancel_called_ms"] = (now - started) * 1000
                    event["cancel_code"] = self.lib.libusb_cancel_transfer(transfer)
                remaining = 0.01 if cancel_at is None or "cancel_code" in event else max(0.0001, cancel_at - now)
                timeout = Timeval(0, int(min(0.01, remaining) * 1e6))
                code = self.lib.libusb_handle_events_timeout_completed(self.context, C.byref(timeout), C.byref(done))
                if code < 0 and code != -10:
                    errors = event.setdefault("event_errors", [])
                    if len(errors) < 64:
                        errors.append(code)
                    time.sleep(.001)
                    cancel_at = time.monotonic()
                    # Keep the transfer and callback alive until completion. The
                    # parent process has a hard watchdog if event handling stalls.
            size = transfer.contents.actual_length
            if not 0 <= size <= capacity:
                raise ProbeError("invalid libusb actual_length")
            data = bytes(buffer[:size])
            status = transfer.contents.status
            code = {0: 0, 1: -1, 2: -7, 3: -10, 4: -9, 5: -4, 6: -8}.get(status, -99)
            event.update(actual_length=size, transfer_status=status, return_code=code,
                         elapsed_ms=(time.monotonic() - started) * 1000, hex=data.hex(" "))
            return data, event
        finally:
            if not submitted or done.value:
                self.lib.libusb_free_transfer(transfer)
            self.checkpoint()

    def transfer(self, endpoint, payload, events):
        data, event = self.transfer_result(endpoint, payload, events)
        self.check(event["return_code"], f"bulk {endpoint:02x}")
        if not endpoint & 0x80 and len(data) != len(payload):
            raise ProbeError("short command write")
        return data


class CancelExperiment(Experiment):
    def __init__(self, report, stop, checkpoint, usb_factory=AsyncUSB):
        super().__init__(report, verbose=False, stop=stop)
        self.checkpoint = checkpoint
        self.usb_factory = usb_factory

    def open(self, expected_path=None):
        self.check_stop()
        record = {}
        self.report.setdefault("connections", []).append(record)
        self.usb = open_device(record, 2000, factory=self.usb_factory,
                               expected_path=expected_path or self.report.get("expected_path"))
        self.usb.stop, self.usb.checkpoint = self.stop, self.checkpoint
        self.transport_healthy = True

    def query(self, query, label=None):
        try:
            return super().query(query, label)
        finally:
            self.checkpoint()

    def reset(self):
        self.check_stop()
        path = (self.report["connections"][-1]["device"].get("sysfs") or {}).get("path")
        if not path:
            raise ProbeError("physical USB port is unknown")
        reset = self.usb.lib.libusb_reset_device
        reset.argtypes, reset.restype = [C.c_void_p], C.c_int
        code = reset(self.usb.handle)
        self.report["operations"].append({"label": "reset_selected_device", "return_code": code})
        self.checkpoint()
        if code == -4:
            self.usb.claimed = False
        else:
            self.usb.check(code, "reset selected device")
        self.close()
        self.pause(0.6)
        self.open(expected_path=path)

    def drain(self):
        self.check_stop()
        op = {"label": "bounded_drain", "transfers": []}
        self.report["operations"].append(op)
        old_timeout = self.usb.timeout_ms
        deadline = time.monotonic() + 1
        try:
            self.usb.timeout_ms = 200
            total = 0
            for _ in range(4):
                self.check_stop()
                if time.monotonic() >= deadline:
                    break
                data, event = self.usb.transfer_result(0x81, 12224, op["transfers"])
                total += len(data)
                if event["return_code"] == -7 and not data:
                    op.update(bytes_read=total, quiet_timeout=True)
                    return
                if event["return_code"] not in (0, -7):
                    self.usb.check(event["return_code"], "drain")
            raise ProbeError("drain did not reach a quiet timeout within its bounds")
        finally:
            self.usb.timeout_ms = old_timeout
            self.checkpoint()

    def recover(self, strategy):
        self.check_stop()
        if strategy == "drain":
            self.drain()
        elif strategy == "clear_halt":
            clear = self.usb.lib.libusb_clear_halt
            clear.argtypes, clear.restype = [C.c_void_p, C.c_ubyte], C.c_int
            for endpoint in (0x81, 0x03):
                self.check_stop()
                code = clear(self.usb.handle, endpoint)
                self.report["operations"].append({"label": f"clear_halt_{endpoint:02x}", "return_code": code})
                self.checkpoint()
                self.usb.check(code, "clear halt")
        elif strategy == "reopen":
            path = self.report["connections"][-1]["device"]["sysfs"]["path"]
            self.close()
            self.pause(0.1)
            self.open(expected_path=path)
        elif strategy == "reset":
            self.reset()
        elif strategy != "none":
            raise ValueError("unknown recovery strategy")
        # Deliberate experiment boundary: try exactly one chosen recovery, then
        # probe it. No automatic drain or fallback is inserted here.
        self.transport_healthy = True

    def wait_contact(self, present, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.require(INFO["extended_status"], "contact_gate")["data_hex"]
            if value == ("05" if present else "06"):
                return
            if value not in ("05", "06"):
                raise ProbeError("unknown extended status")
            self.pause(0.1)
        raise ProbeError("contact gate not reached before timeout")

    def prepare(self, query):
        self.wait_contact(True, 8)
        if query == NORMAL_FRAME:
            self.require(EXTENDED_FRAME, "prepare/refresh")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = self.require(INFO["finger_status"], "prepare/normal_ready")["data_hex"]
                if state == "01":
                    return
                if state != "00":
                    raise ProbeError("unknown normal status")
                self.pause(0.05)
            raise ProbeError("normal frame did not become ready")

    def interrupt(self, case):
        self.check_stop()
        op = {"label": "interruption", "query": case.query.name,
              "request_hex": encode_request(case.query).hex(" "), "transfers": []}
        self.report["operations"].append(op)
        if case.point == "before":
            return {"outcome": "not_submitted", "target_hit": True, "consumed_bytes": 0}
        request = encode_request(case.query)
        if case.point == "out":
            _, event = self.usb.transfer_result(3, request, op["transfers"], case.delay_ms)
            return injection_result(event, 0)
        self.usb.transfer(3, request, op["transfers"])
        if case.point == "in":
            capacity = 12224 if case.query.max_data == 12160 else 64
            raw, event = self.usb.transfer_result(0x81, capacity, op["transfers"], case.delay_ms)
            op["response_hex"] = raw.hex(" ")
            return injection_result(event, len(raw))
        capacity = 4096 if case.point in ("partial", "deferred") else 12224
        raw = bytearray(self.usb.transfer(0x81, capacity, op["transfers"]))
        op["response_hex"] = raw.hex(" ")
        if case.point == "partial":
            hit = len(raw) == 4096 and raw[:4] == bytes.fromhex("02 2f 81 04")
            return {"outcome": "between_reads" if hit else "partial_boundary_not_reached",
                    "target_hit": hit, "consumed_bytes": len(raw)}
        if case.point == "deferred":
            op["cancellation_requested_after_bytes"] = len(raw)
        deadline = time.monotonic() + 5
        while len(raw) < 12165:
            self.check_stop()
            if time.monotonic() >= deadline:
                raise ProbeError("transaction completion deadline expired")
            capacity = ((12166 - len(raw) + 63) // 64) * 64
            chunk = self.usb.transfer(0x81, capacity, op["transfers"])
            if not chunk or len(raw) + len(chunk) > 12166:
                raise ProbeError("invalid frame fragment")
            raw.extend(chunk)
            op["response_hex"] = raw.hex(" ")
        parsed = parse_response(raw, case.query)
        if len(bytes.fromhex(parsed["trailing_hex"])) > 1:
            raise ProbeError("too many frame trailer bytes")
        op.update(parsed)
        return {"outcome": case.point, "target_hit": True, "consumed_bytes": len(raw)}

    def followup(self):
        self.health("followup/health")
        frames = []
        for _ in range(3):
            self.prepare(NORMAL_FRAME)
            frames.append(self.require(NORMAL_FRAME, "followup/frame")["data_hex"])
        self.health("followup/final_health")
        return {"frames": len(frames), "distinct_payloads": len(set(frames)), "health_valid": True}


def injection_result(event, received):
    hit = event.get("transfer_status") == 3 and event.get("cancel_code") == 0
    outcome = "cancelled" if hit else ("completed_before_cancel" if event.get("transfer_status") == 0 else "transport_error")
    return {"outcome": outcome, "target_hit": hit, "consumed_bytes": received,
            **{k: event[k] for k in ("transfer_status", "cancel_code", "cancel_called_ms", "elapsed_ms", "actual_length") if k in event}}


def run_case(case, strategy, report, stop, checkpoint, factory=CancelExperiment):
    ex = factory(report, stop, checkpoint)
    result = {"case": case.name, "strategy": strategy, "status": "running"}
    report["result"] = result
    try:
        ex.open()
        ex.reset()
        ex.drain()
        ex.health("baseline")
        result["baseline_valid"] = True
        ex.prepare(case.query)
        result["precondition_valid"] = True
        try:
            result["injection"] = ex.interrupt(case)
        except ProbeError as error:
            report["injection_error"] = str(error)
            result["injection"] = {"outcome": "injection_error", "target_hit": False}
        checkpoint()
        ex.check_stop()
        recovery_start = len(report["operations"])
        ex.recover(strategy)
        if strategy == "drain":
            result["residual"] = residual_summary(case.query, report["operations"], recovery_start)
        result["followup"] = ex.followup()
        result["status"] = ("passed" if result["injection"]["target_hit"] else "not_hit")
        if result["injection"]["outcome"] == "injection_error":
            result["status"] = "injection_failed"
        elif result["injection"]["target_hit"] and result["followup"]["distinct_payloads"] < 2:
            result["status"] = "observed"
    except Exception as error:
        report["error"] = str(error)
        result["status"] = "cancelled" if stop.is_set() else (
            "baseline_failed" if not result.get("baseline_valid") else
            "blocked" if not result.get("precondition_valid") else "recovery_failed")
    finally:
        try:
            ex.close()
        except Exception as error:
            report["close_error"] = str(error)
            result["status"] = "cleanup_failed"
        checkpoint()
    return result


def residual_summary(query, operations, recovery_start):
    injected = next(o for o in operations if o.get("label") == "interruption")
    consumed = bytes.fromhex(injected.get("response_hex", ""))
    residual = b"".join(bytes.fromhex(t.get("hex", "")) for o in operations[recovery_start:]
                        if o.get("label") == "bounded_drain" for t in o.get("transfers", []))
    result = {"received_before_recovery": len(consumed), "residual_bytes": len(residual),
              "reconstructed_response_valid": False}
    try:
        parsed = parse_response(consumed + residual, query)
        result["reconstructed_response_valid"] = len(bytes.fromhex(parsed["trailing_hex"])) <= 1
    except ProbeError:
        pass
    return result


def frame_metrics(raw):
    values = struct.unpack("<6080H", bytes.fromhex(raw["data_hex"]))
    ordered = sorted(values)
    return {"mean": statistics.mean(values), "sd": statistics.pstdev(values),
            "p05": ordered[303], "p95": ordered[5775],
            "horizontal_difference": statistics.mean(abs(values[y * 40 + x + 1] - values[y * 40 + x])
                                                     for y in range(152) for x in range(39)),
            "vertical_difference": statistics.mean(abs(a - b) for a, b in zip(values[:-40], values[40:]))}


def collect_frames(ex, count, extension="data", interval=0, duration=None, sample_dir=None):
    rows = []
    started = time.monotonic()
    while len(rows) < count and (duration is None or time.monotonic() - started < duration):
        before = ex.require(INFO["finger_status"], "sample/before")
        normal = ex.require(NORMAL_FRAME, "sample/normal")
        after = ex.require(INFO["finger_status"], "sample/after")
        row = {"received_ns": time.monotonic_ns(), "normal_operation": len(ex.report["operations"]) - 2,
               "status_before": before["data_hex"], "status_after": after["data_hex"],
               "normal_sha256": hashlib.sha256(bytes.fromhex(normal["data_hex"])).hexdigest(),
               "normal_metrics": frame_metrics(normal)}
        if extension != "none":
            status = ex.require(INFO["extended_status"], "sample/extended_status")
            row["extended_status"] = status["data_hex"]
            if status["data_hex"] not in ("05", "06"):
                raise ProbeError("unknown extended status while sampling")
            if extension == "data" and status["data_hex"] == "05":
                extended = ex.require(EXTENDED_FRAME, "sample/extended")
                row["extended_operation"] = len(ex.report["operations"]) - 1
                row["extended_metrics"] = frame_metrics(extended)
                row["extended_sha256"] = hashlib.sha256(bytes.fromhex(extended["data_hex"])).hexdigest()
        if sample_dir is not None:
            filename = f"sample-{len(rows) + 1:04d}.json"
            save(sample_dir / filename, {"sample": row, "operations": ex.report["operations"]})
            row["file"] = filename
            ex.report["operations"] = []
        rows.append(row)
        ex.report["samples"] = rows
        ex.checkpoint()
        if interval:
            ex.pause(interval)
    elapsed = time.monotonic() - started
    return {"frames": len(rows), "distinct_payloads": len({r["normal_sha256"] for r in rows}),
            "elapsed_s": elapsed, "delivered_fps": len(rows) / elapsed if elapsed else None,
            "extended_frames": sum("extended_sha256" in r for r in rows),
            "status_before": {v: sum(r["status_before"] == v for r in rows) for v in ("00", "01")},
            "extended_status": {v: sum(r.get("extended_status") == v for r in rows) for v in ("05", "06")}}


def public_summary(report):
    # Explicit projections: never export USB bytes, frame hashes, metrics or exceptions.
    result = {"completed": bool(report.get("completed")), "cases": [], "sampling": [],
              "limitations": report.get("limitations", [])}
    for row in report.get("cases", []):
        item = {k: row[k] for k in ("case", "strategy", "status", "baseline_valid", "precondition_valid") if k in row}
        for key, fields in (("injection", ("outcome", "target_hit", "consumed_bytes", "transfer_status",
                                          "cancel_code", "cancel_called_ms", "elapsed_ms", "actual_length")),
                            ("followup", ("frames", "distinct_payloads", "health_valid")),
                            ("residual", ("received_before_recovery", "residual_bytes", "reconstructed_response_valid"))):
            if key in row:
                item[key] = {k: row[key][k] for k in fields if k in row[key]}
        result["cases"].append(item)
    for row in report.get("sampling", []):
        result["sampling"].append({k: row[k] for k in ("phase", "extension", "frames", "distinct_payloads",
                                                      "elapsed_s", "delivered_fps", "extended_frames",
                                                      "status_before", "extended_status") if k in row})
    if "baseline" in report:
        result["baseline"] = report["baseline"]
    return result


def run_suite(path, stop):
    from device_testing import TestSession
    report = {"schema_version": 1, "experiment": "protocol_validation", "completed": False,
              "started_unix_ns": time.time_ns(),
              "cases": [], "sampling": [], "limitations": [
                  "USB cancellation matrix tests libusb directly, not phase instrumentation of the libfprint driver.",
                  "A completed transfer is not counted as a successful cancellation.",
                  "Partial-read interruption has no pending transfer; it is distinct from in-flight cancellation.",
                  "Guided gestures record instructions, not independent contact, force or motion measurements.",
                  "Status 05/06 is an observed gate, not an independent contact measurement.",
                  "A valid response and changing frames cannot prove freshness without a device sequence number.",
                  "Reported frame rate includes Python, USB queries and report checkpoints; not the hardware ceiling.",
                  "No DPI, ADC precision, calibration, internal processing or matching accuracy measurement.",
                  "Baseline mode tests require successful initialization/recovery prerequisites; rejection leaves modes unresolved.",
                  "No firmware, EC/GPIO, unknown register writes or whole-system suspend.",
                  "No hardware temperature measurement; timed capture tests continuity only."]}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    def progress(message):
        report["progress"] = {"message": message, "completed": len(report["cases"]), "total": len(CASES),
                              "updated_ns": time.monotonic_ns()}
        save(path, report)
    prelude = {"operations": []}
    ex = CancelExperiment(prelude, stop, lambda: save(path.parent / "prelude.json", prelude))
    try:
        progress("先保持抬手，正在建立通信基线……")
        ex.open()
        ex.reset()
        ex.drain()
        ex.health("initial")
        ex.wait_contact(False, 30)
        selected_path = prelude["connections"][-1]["device"]["sysfs"]["path"]
        prelude["expected_path"] = selected_path
        ex.close()
        def baseline_factory(raw, verbose=False, stop=None):
            raw["expected_path"] = selected_path
            return CancelExperiment(raw, stop, lambda: None)
        baseline = TestSession(path.parent / "baseline", "no_finger", experiment_factory=baseline_factory)
        baseline.stop = stop
        while not baseline.terminal:
            progress("保持抬手：" + baseline.steps[baseline.index].title)
            baseline.run_next()
            if baseline.can_recover and not stop.is_set():
                progress("基线项目失败，保留证据；复位并检查后跳过该项继续。")
                baseline.recover("skip")
        report["baseline"] = {"status": baseline.summary["status"], "completed": baseline.summary["completed"],
                              "steps": baseline.summary["steps"], "recoveries": baseline.summary.get("recoveries", [])}
        if not baseline.summary["completed"]:
            raise ProbeError("baseline tests could not finish or recover")
        save(path, report)
        ex.open()
        progress("现在放上同一根手指并保持接触；后续按提示改变动作，无需点击。")
        ex.wait_contact(True, 60)
        # Counterbalance the three access patterns with a recorded fixed seed.
        order = ["none", "status", "data"] * 2
        random.Random(9362).shuffle(order)
        report["channel_order"] = order
        phases = [(f"channel_{i + 1}", "保持不动，正在比较普通/扩展访问顺序", mode, 10, 0, None)
                  for i, mode in enumerate(order)]
        phases += [("light_contact", "请轻触传感器，保持接触但减小压力", "data", 12, .2, None),
                   ("normal_pressure", "请恢复平常按压力度，保持不动", "data", 12, .2, None),
                   ("rolling", "保持接触，轻微左右滚动手指", "data", 16, .2, None),
                   ("steady_stream", "请保持不动，进行约 60 秒连续采集", "data", 600, .1, 60)]
        for phase, instruction, extension, count, interval, duration in phases:
            progress(instruction)
            ex.pause(2)
            ex.prepare(NORMAL_FRAME)
            data = {"operations": [], "phase": phase, "instruction": instruction,
                    "instruction_utc_ns": time.time_ns(), "operator_action_confirmed": False,
                    "extension": extension, "samples": []}
            sample_path = path.parent / (phase + ".json")
            sample_dir = path.parent / phase
            sample_dir.mkdir(mode=0o700)
            # Keep one USB owner but checkpoint each section separately.
            original_report, original_checkpoint = ex.report, ex.checkpoint
            ex.report = data
            def checkpoint():
                save(sample_path, data)
                if duration is not None:
                    progress(instruction + f"（已保存 {len(data['samples'])} 帧）")
            ex.checkpoint = checkpoint
            ex.usb.checkpoint = checkpoint
            try:
                result = collect_frames(ex, count, extension, interval, duration, sample_dir)
                report["sampling"].append({"phase": phase, "extension": extension, **result})
            finally:
                ex.report, ex.checkpoint = original_report, original_checkpoint
                ex.usb.checkpoint = original_checkpoint
                save(sample_path, data)
            save(path, report)
        progress("现在完全抬手，正在检查离开后的状态及缓存。")
        ex.wait_contact(False, 60)
        lift = {"operations": [], "samples": []}
        original_report, original_checkpoint = ex.report, ex.checkpoint
        ex.report = lift
        ex.checkpoint = lambda: save(path.parent / "lift.json", lift)
        ex.usb.checkpoint = ex.checkpoint
        try:
            report["sampling"].append({"phase": "lift", "extension": "status", **collect_frames(ex, 3, "status", .2)})
        finally:
            ex.report, ex.checkpoint = original_report, original_checkpoint
            ex.usb.checkpoint = original_checkpoint
        progress("再次放上同一根手指，保持接触；接下来自动执行所有取消/恢复组合。")
        ex.wait_contact(True, 60)
        ex.close()
        for index, (case, strategy) in enumerate(CASES):
            if stop.is_set():
                raise ProbeError("operator stopped")
            progress(f"保持接触：{index + 1}/{len(CASES)} · {case.name} / {STRATEGY_NAMES[strategy]}")
            case_report = {"operations": [], "case": case.name, "strategy": strategy, "expected_path": selected_path}
            case_path = path.parent / f"case-{index + 1:02d}-{case.name}-{strategy}.json"
            checkpoint = lambda: save(case_path, case_report)
            checkpoint()
            result = run_case(case, strategy, case_report, stop, checkpoint)
            report["cases"].append(result)
            save(path, report)
            if result["status"] in ("baseline_failed", "cleanup_failed", "blocked", "cancelled"):
                raise ProbeError("cannot continue from failed baseline, contact gate, cleanup or operator stop")
        progress("全部组合已完成。现在抬起手指，正在进行最终恢复和检查。")
        final = {"operations": [], "expected_path": selected_path}
        ex = CancelExperiment(final, stop, lambda: save(path.parent / "final.json", final))
        ex.open()
        ex.reset()
        ex.drain()
        ex.health("final")
        ex.wait_contact(False, 60)
        ex.close()
        report["completed"] = True
        progress("综合验证完成，可以保持抬手并查看报告。")
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            ex.close()
        except Exception as error:
            report["close_error"] = str(error)
            report["completed"] = False
        report["finished_unix_ns"] = time.time_ns()
        save(path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    report = run_suite(args.report, stop)
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
