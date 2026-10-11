# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Serial device-test sessions for the preview GUI.

Only this worker owns USB. GTK only signals stop and displays progress. Detailed
wire evidence is private; the exported summary uses explicit field allowlists.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from analyze_finger_interaction import summarize
from device_capabilities import Experiment, INFO, WAKE, command
from frame import write_private
from probe_device import ProbeError, interpret


def utc():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@dataclass(frozen=True)
class Step:
    group: str
    phase: str
    title: str
    instruction: str


NO_FINGER = (
    ("queries", "设备信息和能力查询"),
    ("tail", "查询顺序与额外尾字节对照"),
    ("usb_status", "USB 标准状态"),
    ("ownership", "接口独占"),
    ("wake", "桥接心跳与应用唤醒"),
    ("modes", "运行模式的恢复前提"),
    ("capture_idle", "无手指图像缓冲"),
    ("reopen", "关闭重开与自动挂起（约 12 秒）"),
    ("usb_recovery", "端点恢复与单设备 USB 复位"),
    ("lifecycle", "libfprint 取消及重开（5 次）"),
    ("capture_idle_after", "复位和取消后的图像缓冲"),
)
MOVEMENT = (
    ("baseline", "无手指基线", "完全抬起手指，点击后读取 3 帧。"),
    ("hold", "第 1 次按压：保持", "点击执行后放上手指，保持不动约 4 秒；等提示后再改变动作。"),
    ("move", "持续接触：轻微滚动", "不要抬手。点击后轻微左右滚动约 4 秒，直到提示抬手。"),
    ("lift", "抬手", "现在完全抬起手指。确认离开后点击，读取 3 帧。"),
    ("repress", "第 2 次按压", "点击执行后再次放上同一根手指，保持不动约 4 秒。"),
    ("finish", "再次抬手并分析", "现在完全抬起手指，点击完成动作测试并分析本轮数据。"),
)
COMPARISON = (
    ("baseline", "通道对照基线", "完全抬起手指，点击后读取 3 帧。"),
    ("compare", "一次按压、四阶段对照", "点击执行后放上手指，保持不动约 15 秒。四阶段自动切换，提示抬手前不要移动或抬起。"),
    ("finish", "抬手并分析对照", "四个阶段已结束。现在完全抬起手指，点击完成对照与分析。"),
)
COMPARISON_PHASES = (("normal_before", "none"), ("status_only", "status"),
                     ("with_extended", "data"), ("normal_after", "none"))
STATUS = {"pending": "待执行", "running": "执行中", "passed": "通过", "observed": "完成，含拒绝或不确定项",
          "blocked": "前置条件阻断", "failed": "失败", "cancelled": "已停止", "invalidated": "已作废"}


def plan(scope):
    steps = []
    if scope in ("full", "no_finger"):
        for phase, title in NO_FINGER:
            note = "保持手指完全离开传感器。"
            if phase == "usb_recovery":
                note += "本步骤会清除端点 halt 并复位这一个指纹 USB 设备，地址可能变化。"
            if phase == "modes":
                note += "只核验已知恢复前提，不切换深睡、手势或未知运行模式。"
            steps.append(Step("no_finger", phase, title, note))
    if scope in ("full", "movement"):
        steps.extend(Step("movement", *values) for values in MOVEMENT)
    if scope in ("full", "comparison"):
        steps.extend(Step("comparison", *values) for values in COMPARISON)
    if not steps:
        raise ValueError("unknown test scope")
    return steps


def operation_summary(op):
    # Never copy arbitrary data_hex, response_hex, hashes, values or errors.
    result = {k: op[k] for k in ("label", "query", "request_hex", "outcome", "status",
                                "bcc_valid", "logical_length", "actual_length", "return_code") if k in op}
    if "data_hex" in op:
        result["data_bytes"] = len(bytes.fromhex(op["data_hex"]))
    query = INFO.get(op.get("query"))
    if query is not None and query.name != "main_key_status":
        size = len(bytes.fromhex(op.get("data_hex", "")))
        if op.get("outcome") == "valid_data" and query.min_data <= size <= query.max_data:
            result["data_hex"] = op.get("data_hex", "")
            result["value"] = interpret(query, bytes.fromhex(result["data_hex"]))
    if op.get("label") in {"device_status", "configuration", "interface_alt_setting",
                            "out_endpoint_status", "in_endpoint_status"}:
        result["data_hex"] = op.get("data_hex", "")
    # A prior frame could leave image bytes in a query's extra trailer.
    result["transfer_ms"] = round(sum(t.get("elapsed_ms", 0) for t in op.get("transfers", [])), 3)
    return result


def analysis_summary(analysis):
    result = {"validated_responses": analysis["validated_responses"],
            "normal_frames": analysis["normal_frames"],
            "extended_frames": len(analysis["extended_pairs"]),
            "phases": {name: {key: values[key] for key in (
                "frames", "distinct_payloads", "status_before", "status_after", "extended_status",
                "median_interval_s", "median_raw_transfer_ms")} for name, values in analysis["phases"].items()},
            "limitations": analysis["limitations"]}
    if "channel_comparison" in analysis:
        result["channel_comparison"] = analysis["channel_comparison"]
    return result


class TestSession:
    def __init__(self, root, scope="full", progress=None, experiment_factory=Experiment):
        self.directory = Path(root) / ("device-test-" + uuid.uuid4().hex)
        self.directory.mkdir(parents=True, mode=0o700)
        self.raw_dir = self.directory / "private"
        self.raw_dir.mkdir(mode=0o700)
        self.steps = plan(scope)
        self.stop = threading.Event()
        self.progress = progress or (lambda *args: None)
        self.factory = experiment_factory
        self.experiment = None
        self.finger_report = None
        self.finger_path = None
        self.index = 0
        self.terminal = False
        self.idle_payloads = None
        self.summary = {"schema_version": 1, "started_utc": utc(), "scope": scope,
                        "valid_for_analysis": True, "completed": False, "status": "pending",
                        "steps": [{"group": s.group, "phase": s.phase, "title": s.title, "status": "pending"}
                                  for s in self.steps],
                        "excluded": ["固件/bootloader", "EC/GPIO", "未知间接寄存器", "运行模式写入", "整机电源切换"]}
        self.save()

    def save(self):
        save_json(self.directory / "summary.json", self.summary)
        lines = ["# FT9362 设备测试报告", "", f"开始：{self.summary['started_utc']}",
                 f"状态：{STATUS[self.summary['status']]}；流程完成：{self.summary['completed']}；"
                 f"数据有效：{self.summary['valid_for_analysis']}", "",
                 "本报告不包含指纹图像、像素、模板、帧摘要或手指名字。完整 USB 记录仅保存在 private 子目录。",
                 "完成流程不表示全部能力受支持；拒绝、阻断和未执行项目分别列出。", "",
                 "| 步骤 | 结果 |", "| --- | --- |"]
        lines.extend(f"| {row['title']} | {STATUS[row['status']]} |" for row in self.summary["steps"])
        lines += ["", "排除项：" + "、".join(self.summary["excluded"]), ""]
        if "device" in self.summary:
            lines += ["## 设备信息", "", "```json", json.dumps(self.summary["device"], ensure_ascii=False, indent=2), "```", ""]
        for row in self.summary["steps"]:
            if row["status"] == "pending":
                continue
            lines += [f"## {row['title']}", "", f"结果：{STATUS[row['status']]}", row.get("note", ""), ""]
            for key in ("operations", "analysis", "driver", "actions", "buffer_comparison"):
                if key in row:
                    lines += ["```json", json.dumps(row[key], ensure_ascii=False, indent=2), "```", ""]
        path = self.directory / "report.md"
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("\n".join(lines))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    def close_finger(self):
        if self.experiment is not None:
            try:
                self.experiment.close()
            finally:
                self.experiment = None
                save_json(self.finger_path, self.finger_report)

    def finish(self, invalidated=False):
        """Called by the worker only, after any active step has returned."""
        try:
            self.close_finger()
        except Exception:
            self.summary["status"] = "failed"
            self.summary["cleanup_failed"] = True
        if invalidated:
            self.summary.update(valid_for_analysis=False, status="invalidated", completed=False)
            for path in self.raw_dir.glob("*/report.json"):
                report = json.loads(path.read_text())
                report["valid_for_analysis"] = False
                report["invalidated_reason"] = "operator discarded this session"
                save_json(path, report)
        elif not self.terminal:
            self.summary["status"] = "failed" if self.summary.get("cleanup_failed") else "cancelled"
        self.terminal = True
        self.summary["finished_utc"] = utc()
        self.save()

    def run_next(self):
        if self.terminal:
            return
        step = self.steps[self.index]
        row = self.summary["steps"][self.index]
        row.update(status="running", confirmed_utc=utc(), instruction=step.instruction)
        self.summary["status"] = "running"
        self.save()
        try:
            if self.stop.is_set():
                raise ProbeError("stopped")
            if step.group == "no_finger":
                self.run_no_finger(step.phase, row)
            else:
                self.run_finger(step, row)
            row.setdefault("note", "步骤执行完成；具体能力状态见记录。")
            if row["status"] == "running":
                row["status"] = "passed"
            self.index += 1
            if self.index == len(self.steps):
                self.terminal = True
                observed = any(r["status"] in ("observed", "blocked") for r in self.summary["steps"])
                self.summary.update(completed=True, status="observed" if observed else "passed", finished_utc=utc())
        except Exception as error:
            row["status"] = "cancelled" if self.stop.is_set() else "failed"
            row["note"] = "测试已停止，详情见本轮 private 目录的错误记录。"
            save_json(self.raw_dir / "error.json", {"phase": step.phase, "error": str(error), "at": utc()})
            self.summary["status"] = row["status"]
            self.terminal = True
            self.summary["finished_utc"] = utc()
            if self.finger_report is not None:
                self.finger_report.update(error=str(error), completed=False, failed_phase=step.phase)
            try:
                self.close_finger()
            except Exception as close_error:
                self.summary["cleanup_failed"] = True
                save_json(self.raw_dir / "close-error.json", {"error": str(close_error)})
        finally:
            row["finished_utc"] = utc()
            self.save()

    def run_no_finger(self, phase, row):
        path = self.raw_dir / (phase + ".json")
        if phase == "lifecycle":
            # GI synchronous APIs drive a GLib loop: isolate them from GTK's loop.
            with (self.raw_dir / "lifecycle.log").open("x", encoding="utf-8") as log:
                os.chmod(log.name, 0o600)
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("test_driver_lifecycle.py")),
                                            "--report", str(path)], stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 45
                interrupted = False
                while process.poll() is None:
                    if self.stop.wait(0.1) or time.monotonic() > deadline:
                        interrupted = True
                        process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
                if interrupted or process.returncode:
                    raise ProbeError("lifecycle interrupted or failed; see private/lifecycle.log")
            report = json.loads(path.read_text())
            if not report.get("success") or not report.get("closed"):
                raise ProbeError("lifecycle did not finish and release device")
            row["driver"] = report["driver"]
            row["actions"] = [{k: a[k] for k in ("cancel_after_ms", "cancel_called_ms", "elapsed_ms",
                                                 "cancelled", "device_still_open", "cancellation_latency_ms")}
                              for a in report["actions"]]
            return
        report = {"schema_version": 1, "suite": phase, "started_utc": utc(), "operations": []}
        experiment = self.factory(report, verbose=False, stop=self.stop)
        try:
            experiment.open()
            if phase == "tail":
                for _ in range(2):
                    for name in ("controller_version", "dimension_a", "bridge_status", "dimension_a",
                                 "app_status", "dimension_a", "dimension_b", "bridge_uid"):
                        experiment.query(INFO[name])
            elif phase == "modes":
                experiment.health("baseline")
                result = experiment.query(WAKE, "restore/wake")
                row["status"] = "blocked"
                row["note"] = ("唤醒前提未通过，未发送任何 87 模式写入。" if result["outcome"] != "valid_data"
                               else "唤醒有 ACK；安全向导仍不切换模式，完整恢复和物理效果未验证。")
            else:
                getattr(experiment, "capture_idle" if phase == "capture_idle_after" else phase)()
            report["completed"] = True
        except ProbeError as error:
            report.update(completed=False, error=str(error))
            last = report["operations"][-1] if report["operations"] else {}
            if phase == "wake" and experiment.transport_healthy and last.get("outcome") == "semantic_rejection":
                row.update(status="blocked", note="设备返回完整的语义拒绝，唤醒后续流程被阻断。")
            else:
                raise
        finally:
            try:
                experiment.close()
            finally:
                report["finished_utc"] = utc()
                save_json(path, report)
                row["operations"] = [operation_summary(op) for op in report["operations"]]
                if phase == "tail":
                    # These selected responses follow only known non-image queries.
                    for public, raw in zip(row["operations"], report["operations"]):
                        if raw.get("query") in ("dimension_a", "bridge_uid"):
                            public["trailing_hex"] = raw.get("trailing_hex", "")
                for connection in report.get("connections", []):
                    if "device" in connection:
                        self.summary["device"] = connection["device"]
        if phase in ("capture_idle", "capture_idle_after"):
            payloads = [op["data_hex"] for op in report["operations"] if op.get("query") == "raw_frame"
                        and op.get("outcome") == "valid_data"]
            row["buffer_comparison"] = {"frames": len(payloads), "distinct_payloads": len(set(payloads))}
            if self.idle_payloads is not None:
                row["buffer_comparison"]["same_as_before_recovery"] = set(payloads) == set(self.idle_payloads)
            self.idle_payloads = payloads
        if row["status"] == "running" and any(op.get("outcome") in ("semantic_rejection", "control_request_stalled")
                                                for op in report["operations"]):
            row["status"] = "observed"

    def wait_status(self, expected, phase, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.experiment.require(INFO["finger_status"], phase + "/wait")
            if value["data_hex"] == expected:
                return
            self.experiment.pause(0.1)
        raise ProbeError(f"{phase}: status {expected} not observed within {seconds}s")

    def sample(self, phase, index, extension):
        ex = self.experiment
        before = ex.require(INFO["finger_status"], phase + "/before")
        frame = ex.require(command("raw_frame", "81", 12160), phase + "/frame")
        received_ns = time.monotonic_ns()
        after = ex.require(INFO["finger_status"], phase + "/after")
        extended = {"data_hex": "not_queried"}
        if extension != "none":
            extended = ex.require(INFO["extended_status"], phase + "/extended_status")
        if extension == "data" and extended["data_hex"] == "05":
            ex.require(command("extended_frame", "82 76 01", 12160), phase + "/extended_frame")
        raw = bytes.fromhex(frame["response_hex"])
        name = f"{phase}-{index:02d}.bin"
        write_private(self.finger_path.parent / name, raw)
        self.finger_report["samples"].append({"phase": phase, "index": index, "file": name,
            "received_monotonic_ns": received_ns, "sha256": frame["data_sha256"],
            "status_before": before["data_hex"], "status_after": after["data_hex"],
            "extended_status": extended["data_hex"], "extension_protocol": extension})
        save_json(self.finger_path, self.finger_report)
        return raw

    def run_finger(self, step, row):
        if self.experiment is None:
            directory = self.raw_dir / step.group
            directory.mkdir(mode=0o700)
            self.finger_path = directory / "report.json"
            self.finger_report = {"schema_version": 1, "started_utc": utc(), "operations": [],
                "samples": [], "operator_steps": [], "completed": False,
                "experiment": "channel_comparison" if step.group == "comparison" else "finger_movement"}
            if step.group == "comparison":
                self.finger_report["comparison_order"] = COMPARISON_PHASES
            self.experiment = self.factory(self.finger_report, verbose=False, stop=self.stop)
            self.experiment.open()
            self.experiment.health("initial")
        self.finger_report["operator_steps"].append({"phase": step.phase, "confirmed_utc": utc(),
                                                      "instruction": step.instruction})
        if step.phase in ("baseline", "lift", "finish"):
            self.wait_status("00", step.phase, 15)
        elif step.phase in ("hold", "repress", "compare"):
            self.wait_status("01", step.phase, 45)
        if step.phase == "finish":
            self.experiment.health("final")
            self.close_finger()
            self.finger_report.update(completed=True, finished_utc=utc())
            save_json(self.finger_path, self.finger_report)
            analysis = summarize(self.finger_path)
            save_json(self.finger_path.parent / "analysis.json", analysis)
            row["analysis"] = analysis_summary(analysis)
            return
        phases = COMPARISON_PHASES if step.phase == "compare" else (
            (step.phase, "none" if step.group == "comparison" else "data"),)
        for phase, extension in phases:
            count = 3 if phase in ("baseline", "lift") else 8
            for index in range(count):
                raw = self.sample(phase, index, extension)
                self.progress(raw, phase, index + 1, count)
                self.experiment.pause(0.350)
        save_json(self.finger_path, self.finger_report)
