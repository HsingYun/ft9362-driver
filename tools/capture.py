#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Capture opaque FT9362 USB frames using the raw libfprint driver."""

import argparse
import os
from pathlib import Path
import signal
import sys

from frame import extract_frame, save_frame


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "build" / "captures")
    parser.add_argument("--count", type=positive_int, default=1,
                        help="CLI: number of frames to capture (default: 1)")
    parser.add_argument("--timeout", type=positive_int, default=30,
                        help="CLI: maximum seconds to wait for each frame (default: 30)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="capture individual files in the terminal")
    mode.add_argument("--preview", type=Path,
                        help="open a saved raw .bin in the GUI without accessing the sensor")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    os.umask(0o077)
    if not args.cli:
        from gui import run
        run(args)
        return

    import gi
    gi.require_version("FPrint", "2.0")
    from gi.repository import FPrint, Gio, GLib

    context = FPrint.Context.new()
    context.enumerate()
    devices = [d for d in context.get_devices() if d.get_driver() == "focaltech0752"]
    if len(devices) != 1:
        raise RuntimeError(f"Expected one FT9362 device, found {len(devices)}")
    device = devices[0]
    if device.get_nr_enroll_stages() != 1:
        raise RuntimeError("Loaded a legacy driver; enter this repository's nix develop shell")
    device.open_sync(None)
    try:
        for index in range(args.count):
            if index:
                input("Lift your finger completely, then press Enter for the next frame. ")
            print(f"Frame {index + 1}/{args.count}: place your finger on the sensor.", flush=True)
            cancellable = Gio.Cancellable.new()

            def cancel():
                cancellable.cancel()
                return GLib.SOURCE_CONTINUE

            timeout_id = GLib.timeout_add_seconds(args.timeout, cancel)
            signal_id = GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, cancel)
            try:
                enrolled = device.enroll_sync(FPrint.Print.new(device), cancellable, None, None)
            finally:
                GLib.source_remove(timeout_id)
                GLib.source_remove(signal_id)

            frame = extract_frame(enrolled)
            stem = save_frame(args.output, frame, bytes(enrolled.serialize()))
            print(f"Saved {len(frame)} bytes: {stem.with_suffix('.bin')}", flush=True)
            print("Capture complete; lift your finger.", flush=True)
    finally:
        device.close_sync(None)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f"Capture failed: {error}", file=sys.stderr)
        sys.exit(1)
