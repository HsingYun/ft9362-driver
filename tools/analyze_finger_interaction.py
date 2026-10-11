#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Validate and summarize one completed, non-invalidated finger experiment."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import struct

from probe_device import decode_response


def pixels(data):
    if len(data) != 12160:
        raise ValueError("expected 12160 bytes of pixel data")
    return struct.unpack("<6080H", data)


def mae(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def summarize(path):
    path = Path(path).resolve()
    if "invalidated" in path.parts:
        raise ValueError("invalidated experiment must not be analyzed")
    raw_report = path.read_bytes()
    report = json.loads(raw_report)
    if report.get("valid_for_analysis") is False:
        raise ValueError("invalidated experiment must not be analyzed")
    if not report.get("completed") or report.get("error"):
        raise ValueError("experiment is incomplete or failed")
    if any(c.get("release_errors") for c in report.get("connections", [])):
        raise ValueError("experiment did not release its interface cleanly")
    operations = report["operations"]
    for op in operations:
        parsed = decode_response(bytes.fromhex(op["response_hex"]), 12160)
        if parsed["status"] != 4 or parsed["data_hex"] != op["data_hex"]:
            raise ValueError("saved operation disagrees with validated wire response")
        for transfer in op["transfers"]:
            if transfer["return_code"] or transfer["actual_length"] != len(bytes.fromhex(transfer["hex"])):
                raise ValueError("failed or inconsistent transfer record")
        received = b"".join(bytes.fromhex(t["hex"]) for t in op["transfers"] if t["endpoint"] == "81")
        if received != bytes.fromhex(op["response_hex"]):
            raise ValueError("response differs from actual USB transfer bytes")
    frame_ops = [op for op in operations if op["query"] == "raw_frame"]
    samples = report["samples"]
    if len(frame_ops) != len(samples):
        raise ValueError("sample and frame-operation counts differ")
    for sample, op in zip(samples, frame_ops):
        file = (path.parent / sample["file"]).resolve()
        if file.parent != path.parent:
            raise ValueError("sample file is outside this experiment")
        raw = bytes.fromhex(op["response_hex"])
        data = bytes.fromhex(op["data_hex"])
        if file.read_bytes() != raw or hashlib.sha256(data).hexdigest() != sample["sha256"]:
            raise ValueError("sample file/hash mismatch")
        if op["label"] != sample["phase"] + "/frame":
            raise ValueError("sample phase mismatch")
    result = {"source_report": str(path), "source_sha256": hashlib.sha256(raw_report).hexdigest(),
              "analyzed_utc": datetime.now(timezone.utc).isoformat(),
              "validated_responses": len(operations), "normal_frames": len(samples), "phases": {}}
    for phase in dict.fromkeys(sample["phase"] for sample in samples):
        rows = [s for s in samples if s["phase"] == phase]
        ops = [o for o in frame_ops if o["label"] == phase + "/frame"]
        data = [pixels(bytes.fromhex(o["data_hex"])) for o in ops]
        intervals = [(b["received_monotonic_ns"] - a["received_monotonic_ns"]) / 1e9
                     for a, b in zip(rows, rows[1:])]
        deltas = [mae(a, b) for a, b in zip(data, data[1:])]
        result["phases"][phase] = {
            "frames": len(rows), "distinct_payloads": len({s["sha256"] for s in rows}),
            "status_before": dict(Counter(s["status_before"] for s in rows)),
            "status_after": dict(Counter(s["status_after"] for s in rows)),
            "extended_status": dict(Counter(s["extended_status"] for s in rows)),
            "adjacent_mean_absolute_pixel_difference": deltas,
            "median_interval_s": statistics.median(intervals) if intervals else None,
            "median_raw_transfer_ms": statistics.median(sum(t["elapsed_ms"] for t in o["transfers"]) for o in ops),
            "pixel_min": min(map(min, data)), "pixel_max": max(map(max, data)),
        }
    pairs = []
    previous_normal = None
    for op in operations:
        if op["query"] == "raw_frame":
            previous_normal = op
        elif op["query"] == "extended_frame":
            if previous_normal is None:
                raise ValueError("extended frame has no preceding normal frame")
            a = pixels(bytes.fromhex(previous_normal["data_hex"]))
            b = pixels(bytes.fromhex(op["data_hex"]))
            pairs.append({"phase": op["label"].split("/")[0], "identical": a == b,
                          "mean_absolute_pixel_difference": mae(a, b),
                          "transfer_ms": sum(t["elapsed_ms"] for t in op["transfers"])})
    result["extended_pairs"] = pairs
    if report.get("experiment") == "channel_comparison":
        phase_ops = {phase: [op for op in frame_ops if op["label"] == phase + "/frame"]
                     for phase in ("normal_before", "status_only", "with_extended", "normal_after")}
        if all(phase_ops.values()):
            a, b, c, d = phase_ops.values()
            result["channel_comparison"] = {
                "a_and_b_same_payload": len({op["data_hex"] for op in a + b}) == 1,
                "c_first_matches_b_last": c[0]["data_hex"] == b[-1]["data_hex"],
                "c_later_frames_differ_from_first": any(op["data_hex"] != c[0]["data_hex"] for op in c[1:]),
                "d_constant": len({op["data_hex"] for op in d}) == 1,
                "d_first_differs_from_c_last": d[0]["data_hex"] != c[-1]["data_hex"],
            }
    protocol_limit = ("Single fixed-order channel comparison; no randomized repeats or cold-start equivalence established."
                      if report.get("experiment") == "channel_comparison" else
                      "The sequence mixes normal and extended reads; it does not isolate the refresh trigger.")
    result["limitations"] = [protocol_limit,
                              "Operator confirmations describe intended actions, not independently measured physical contact.",
                              "Timing includes deliberate pacing and cannot determine maximum fresh-frame rate."]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    result = summarize(args.report)
    os.umask(0o077)
    output = args.report.resolve().parent / "analysis.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    for phase, values in result["phases"].items():
        print(f"{phase}: {values['frames']} frames, {values['distinct_payloads']} distinct; "
              f"status {values['status_before']} -> {values['status_after']}; "
              f"extended {values['extended_status']}")
    print(f"Validated {result['validated_responses']} responses; {len(result['extended_pairs'])} extended frames")
    print(f"Analysis: {output}")


if __name__ == "__main__":
    main()
