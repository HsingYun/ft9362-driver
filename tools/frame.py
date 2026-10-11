# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Raw frame extraction, export and display conversion shared by GUI and CLI."""

import os
import struct
from uuid import uuid4

RAW_FORMAT = "FT9362-RAW-v1"
RAW_FRAME_SIZE = 12166


def extract_frame(print_):
    payload = print_.get_property("fpi-data")
    if payload is None or payload.get_type_string() != "(say)":
        raise ValueError("Expected a raw capture print")
    tag, frame = payload.unpack()
    if tag != RAW_FORMAT or len(frame) != RAW_FRAME_SIZE:
        raise ValueError("Unexpected raw capture format or frame length")
    return bytes(frame)


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def save_frame(directory, frame, serialized=None):
    if len(frame) != RAW_FRAME_SIZE:
        raise ValueError("原始帧必须为 12,166 字节")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    stem = directory / f"frame-{uuid4().hex}"
    write_private(stem.with_suffix(".bin"), frame)
    if serialized is not None:
        write_private(stem.with_suffix(".fpprint"), serialized)
    return stem


def decode_preview(frame, invert=True):
    """Return a 40×152 grayscale preview without modifying the raw frame."""
    if len(frame) != RAW_FRAME_SIZE:
        raise ValueError("原始帧必须为 12,166 字节")
    values = struct.unpack_from("<6080H", frame, 4)
    ordered = sorted(values)
    low, high = ordered[len(ordered) * 5 // 100], ordered[len(ordered) * 95 // 100]
    if high == low:
        low, high = ordered[0], ordered[-1]
    if high == low:
        return 40, 152, bytes([127]) * len(values)
    pixels = [max(0, min(255, (v - low) * 255 // (high - low))) for v in values]
    if invert:
        pixels = [255 - p for p in pixels]
    return 40, 152, bytes(pixels)
