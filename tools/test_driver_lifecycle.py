#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Exercise libfprint open, no-finger enrollment cancellation and reopening.

Run with no finger on the sensor. Does not install a service or enroll a user.
"""

from datetime import datetime, timezone
import argparse
import gc
import json
import os
from pathlib import Path
import sys
import time
import uuid

import gi
gi.require_version("FPrint", "2.0")
gi.require_version("GUsb", "1.0")
from gi.repository import FPrint, Gio, GLib, GUsb


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    output_dir = Path(__file__).resolve().parents[1] / "build" / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.report or output_dir / f"driver-lifecycle-{uuid.uuid4().hex}.json"
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "actions": [], "opens": []}
    device = None
    context = None
    devices = []
    code = 0
    with path.open("x", encoding="utf-8") as output:
        try:
            for attempt in range(2):
                device = None
                devices = []
                gc.collect()
                context = None
                gc.collect()
                context = FPrint.Context.new()
                context.enumerate()
                devices = [d for d in context.get_devices() if d.get_driver() == "focaltech0752"]
                if len(devices) != 1:
                    raise RuntimeError(f"expected one FT9362, found {len(devices)}")
                device = devices[0]
                try:
                    device.open_sync(None)
                    report["opens"].append({"success": True})
                    break
                except GLib.Error as error:
                    report["opens"].append({"success": False, "error": str(error)})
                    if attempt or not any(error.matches(GUsb.Device.error_quark(), value)
                                          for value in (GUsb.DeviceError.IO, GUsb.DeviceError.NO_DEVICE)):
                        raise
                    time.sleep(0.6)
            report["driver"] = {"name": device.get_name(), "id": device.get_device_id(),
                                "features": str(device.get_features()),
                                "feature_names": [name for name in dir(FPrint.DeviceFeature)
                                                  if name.isupper() and
                                                  int(getattr(FPrint.DeviceFeature, name)) & int(device.get_features())],
                                "scan_type": str(device.get_scan_type()),
                                "enroll_stages": device.get_nr_enroll_stages()}
            if device.get_nr_enroll_stages() != 1:
                raise RuntimeError("expected the current single-frame raw driver")
            for delay_ms in (0, 250, 750, 1500, 250):
                cancellable = Gio.Cancellable.new()
                result = {"cancel_after_ms": delay_ms}
                report["actions"].append(result)
                started = time.monotonic_ns()
                timer = None

                def cancel():
                    nonlocal timer
                    timer = None
                    result["cancel_called_ms"] = (time.monotonic_ns() - started) / 1e6
                    cancellable.cancel()
                    return GLib.SOURCE_REMOVE

                if delay_ms:
                    timer = GLib.timeout_add(delay_ms, cancel)
                else:
                    cancel()
                try:
                    device.enroll_sync(FPrint.Print.new(device), cancellable, None, None)
                    raise RuntimeError("enrollment completed before cancellation; no-finger precondition failed")
                except GLib.Error as error:
                    result.update(error=str(error), domain=error.domain, error_code=error.code)
                    result["cancelled"] = error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
                    if not result["cancelled"]:
                        raise
                finally:
                    if timer is not None:
                        GLib.source_remove(timer)
                    result["elapsed_ms"] = (time.monotonic_ns() - started) / 1e6
                    result["device_still_open"] = device.is_open()
                result["cancellation_latency_ms"] = result["elapsed_ms"] - result["cancel_called_ms"]
                print(f"cancel after {delay_ms} ms: {result['cancelled']}; "
                      f"latency={result['cancellation_latency_ms']:.3f} ms", flush=True)
                if delay_ms == 1500:
                    device.close_sync(None)
                    device.open_sync(None)
                    report["reopen_after_cancel"] = True
            report["success"] = True
        except (Exception, KeyboardInterrupt) as error:
            report.update(success=False, error=str(error) or "interrupted")
            print(report["error"], file=sys.stderr)
            code = 1
        finally:
            if device is not None and device.is_open():
                try:
                    device.close_sync(None)
                    report["closed"] = True
                except GLib.Error as error:
                    report.update(success=False, close_error=str(error))
                    code = 1
            # Release Python device wrappers before their FPrint/GUsb context.
            # Otherwise interpreter teardown can warn of still-referenced USB
            # devices even though the interface was correctly closed.
            device = None
            devices = []
            gc.collect()
            context = None
            gc.collect()
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
    print(f"Report: {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
