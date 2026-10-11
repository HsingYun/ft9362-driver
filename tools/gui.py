# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""GTK live preview using asynchronous libfprint capture."""

from collections import deque
import signal
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("FPrint", "2.0")
from gi.repository import FPrint, GdkPixbuf, Gio, GLib, Gtk

from frame import decode_preview, extract_frame, save_frame, write_private
from recording import Recording, saved_fingers


class PreviewWindow(Gtk.Window):
    def __init__(self, args):
        super().__init__(title="FT9362 指纹实时预览")
        self.args = args
        self.context = None
        self.device = None
        self.pending = False
        self.running = False
        self.closing = False
        self.cancellable = None
        self.next_id = 0
        self.frame = None
        self.enrolled = None
        self.pixbuf = None
        self.frames = 0
        self.last_frame_time = None
        self.frame_times = deque()
        self.first_frame_time = None
        self.recording = None
        self.dirty = False
        self.last_stats_log = 0
        self.set_default_size(720, 650)
        self.set_border_width(16)
        self.connect("delete-event", self.on_delete)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)
        title = Gtk.Label(label="FT9362 · 原始数据预览")
        title.set_xalign(0)
        box.pack_start(title, False, False, 0)
        controls = Gtk.Box(spacing=8)
        box.pack_start(controls, False, False, 0)
        self.start_button = Gtk.Button(label="恢复预览")
        self.start_button.connect("clicked", self.start)
        controls.pack_start(self.start_button, False, False, 0)
        self.stop_button = Gtk.Button(label="暂停预览")
        self.stop_button.connect("clicked", self.stop)
        controls.pack_start(self.stop_button, False, False, 0)
        self.save_button = Gtk.Button(label="保存当前帧")
        self.save_button.connect("clicked", self.save)
        controls.pack_start(self.save_button, False, False, 0)
        self.record_button = Gtk.Button(label="录制…")
        self.record_button.connect("clicked", self.record)
        controls.pack_start(self.record_button, False, False, 0)
        self.end_record_button = Gtk.Button(label="停止录制")
        self.end_record_button.connect("clicked", self.end_record)
        controls.pack_start(self.end_record_button, False, False, 0)

        self.invert = Gtk.CheckButton(label="反色")
        self.invert.set_active(True)
        self.invert.connect("toggled", self.render)
        options = Gtk.Box(spacing=12)
        options.pack_start(self.invert, False, False, 0)
        box.pack_start(options, False, False, 0)

        self.picture = Gtk.Image()
        box.pack_start(self.picture, True, True, 0)
        self.info = Gtk.Label(label="尚未采集")
        self.info.set_xalign(0)
        box.pack_start(self.info, False, False, 0)
        self.record_info = Gtk.Label(label="仅预览，不保存。点击“录制”选择手指后开始保存。")
        self.record_info.set_xalign(0)
        self.record_info.set_line_wrap(True)
        box.pack_start(self.record_info, False, False, 0)
        self.status = Gtk.Label(label="正在启动预览…")
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)
        self.status.set_selectable(True)
        box.pack_start(self.status, False, False, 0)
        self.note = Gtk.Label()
        self.note.set_xalign(0)
        self.note.set_line_wrap(True)
        box.pack_start(self.note, False, False, 0)
        self.render()
        if args.preview:
            self.frame = args.preview.read_bytes()
            self.render()
            self.info.set_text(f"离线预览：{args.preview.name}")
            self.record_info.set_text("仅查看，不修改原始文件。")
            self.status.set_text("离线预览，可调整反色并导出图片。")
        self.update_buttons()
        self.refresh_id = GLib.timeout_add(100, self.refresh)

    def update_buttons(self):
        self.start_button.set_sensitive(not self.running and not self.pending and not self.args.preview)
        self.stop_button.set_sensitive(self.running)
        self.save_button.set_sensitive(self.frame is not None)
        self.record_button.set_sensitive(self.running and self.recording is None)
        self.end_record_button.set_sensitive(self.recording is not None)

    def start(self, *_):
        if self.pending or self.running:
            return
        try:
            if self.device is None:
                self.context = FPrint.Context.new()
                self.context.enumerate()
                devices = [d for d in self.context.get_devices() if d.get_driver() == "focaltech0752"]
                if len(devices) != 1:
                    raise RuntimeError(f"需要一个 FT9362 设备，当前找到 {len(devices)} 个")
                self.device = devices[0]
                if self.device.get_nr_enroll_stages() != 1:
                    self.device = None
                    raise RuntimeError("加载了旧驱动，请通过本仓库的 nix develop 启动")
            self.running = True
            self.frames = 0
            self.last_frame_time = None
            self.first_frame_time = None
            self.frame_times.clear()
            self.status.set_text("正在打开传感器…")
            self.pending = True
            self.cancellable = Gio.Cancellable.new()
            self.device.open(self.cancellable, self.opened, None)
        except Exception as error:
            self.running = False
            self.pending = False
            self.status.set_text(f"无法开始：{error}")
        self.update_buttons()

    def opened(self, device, result, _=None):
        self.pending = False
        try:
            device.open_finish(result)
        except GLib.Error as error:
            self.running = False
            self.status.set_text(f"无法打开设备：{error}\n请检查 USB 读写权限，以及是否有其他程序占用。")
            self.device = None
        if self.running:
            self.capture()
        else:
            self.release()
        self.update_buttons()

    def capture(self):
        self.next_id = 0
        if not self.running:
            return GLib.SOURCE_REMOVE
        self.pending = True
        self.cancellable = Gio.Cancellable.new()
        self.status.set_text("请放上手指；若不再出帧，请抬手后重新放上。等待手指不会自动结束录制。")
        self.device.enroll(template_print=FPrint.Print.new(self.device),
                           cancellable=self.cancellable, callback=self.captured, user_data=None)
        return GLib.SOURCE_REMOVE

    def captured(self, device, result, _=None):
        self.pending = False
        try:
            enrolled = device.enroll_finish(result)
            monotonic_ns = time.monotonic_ns()
            unix_ns = time.time_ns()
            frame = extract_frame(enrolled)
            self.frame, self.enrolled = frame, enrolled
            self.frames += 1
            now = monotonic_ns / 1e9
            if self.first_frame_time is None:
                self.first_frame_time = now
            self.last_frame_time = now
            self.frame_times.append(now)
            self.dirty = True
            if self.recording is not None:
                self.recording.append(frame, unix_ns, monotonic_ns)
        except Exception as error:
            if (self.running and isinstance(error, GLib.Error)
                    and error.matches(FPrint.device_error_quark(), FPrint.DeviceError.TOO_HOT)):
                self.status.set_text("libfprint 请求冷却，稍后自动继续；录制会话保持开启。")
                self.next_id = GLib.timeout_add_seconds(2, self.capture)
                return
            if self.running:
                self.status.set_text(f"采集失败：{error}")
            self.running = False
        if self.running:
            # No deliberate frame interval; rendering has its own 10 Hz timer.
            self.next_id = GLib.idle_add(self.capture)
        else:
            self.end_record()
            self.release()
        self.update_buttons()

    def refresh(self):
        now = time.monotonic()
        while self.frame_times and now - self.frame_times[0] > 5:
            self.frame_times.popleft()
        fps = 0.0
        if len(self.frame_times) > 1:
            fps = (len(self.frame_times) - 1) / max(now - self.frame_times[0], 0.001)
        if self.frames:
            average = ((self.frames - 1) / (self.last_frame_time - self.first_frame_time)
                       if self.frames > 1 else 0.0)
            self.info.set_text(f"{self.frames} 帧 · 近期 {fps:.2f} FPS · 帧间平均 {average:.2f} FPS"
                               f" · 距上一帧 {now - self.last_frame_time:.1f} 秒")
            if self.running and now - self.last_stats_log >= 5:
                print(f"Capture: frames={self.frames}, recent_fps={fps:.2f}, "
                      f"mean_fps={average:.2f}, last_frame_age={now - self.last_frame_time:.1f}s", flush=True)
                self.last_stats_log = now
        if self.recording:
            self.record_info.set_text(f"● 正在录制：{self.recording.name} · 本次 {self.recording.count} 帧\n"
                                      f"{self.recording.path}")
        if self.dirty:
            self.render()
            self.dirty = False
        return GLib.SOURCE_CONTINUE

    def record(self, *_):
        dialog = Gtk.Dialog(title="选择要录制的手指", transient_for=self, modal=True)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "开始录制", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_border_width(16)
        choice = Gtk.ComboBoxText()
        choice.append_text("新建手指数据库")
        fingers = saved_fingers(self.args.output)
        for name, path in fingers:
            choice.append_text(f"追加：{name} ({path.stem[-8:]})")
        choice.set_active(0)
        content.pack_start(choice, False, False, 0)
        entry = Gtk.Entry()
        entry.set_placeholder_text("给手指起名字，例如：右手食指")
        content.pack_start(entry, False, False, 0)
        validation = Gtk.Label(label="每根手指一个数据库；停止录制后仍可继续预览。")
        content.pack_start(validation, False, False, 0)
        choice.connect("changed", lambda widget: entry.set_sensitive(widget.get_active() == 0))
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            if not self.running:
                validation.set_text("预览已停止，请关闭对话框并恢复预览。")
                continue
            name = entry.get_text().strip()
            selected = choice.get_active()
            if selected == 0 and not name:
                validation.set_text("请先输入手指名字。")
                continue
            if selected == 0 and any(saved_name == name for saved_name, _ in fingers):
                validation.set_text("该名字已存在，请在上方选择追加。")
                continue
            try:
                self.recording = Recording(self.args.output, name=name,
                                            path=fingers[selected - 1][1] if selected else None)
            except Exception as error:
                validation.set_text(f"无法开始录制：{error}")
                continue
            break
        dialog.destroy()
        self.update_buttons()

    def end_record(self, *_):
        if self.recording is not None:
            recording, self.recording = self.recording, None
            try:
                recording.close()
                self.record_info.set_text(f"已保存：{recording.name} · 本次 {recording.count} 帧\n"
                                          f"{recording.path}")
            except Exception as error:
                self.record_info.set_text(f"数据库写入失败：{error}")
        self.update_buttons()

    def render(self, *_):
        self.note.set_text("40 × 152 灰度预览 · 仅调整显示对比度，保存的原始数据不变。")
        if self.frame is None:
            return
        width, height, pixels = decode_preview(self.frame, self.invert.get_active())
        rgb = bytes(channel for pixel in pixels for channel in (pixel, pixel, pixel))
        self.pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(rgb), GdkPixbuf.Colorspace.RGB,
                                                    False, 8, width, height, width * 3)
        scale = max(1, min(6, 640 // width, 456 // height))
        self.picture.set_from_pixbuf(self.pixbuf.scale_simple(width * scale, height * scale,
                                                              GdkPixbuf.InterpType.NEAREST))

    def save(self, *_):
        try:
            self.render()
            serialized = bytes(self.enrolled.serialize()) if self.enrolled is not None else None
            stem = save_frame(self.args.output, self.frame, serialized)
            success, png = self.pixbuf.save_to_bufferv("png", [], [])
            if not success:
                raise RuntimeError("PNG 编码失败")
            write_private(stem.with_suffix(".png"), bytes(png))
            self.status.set_text(f"已保存：{stem.resolve()}（原始帧与预览 PNG）")
        except Exception as error:
            self.status.set_text(f"保存失败：{error}")

    def stop(self, *_):
        self.running = False
        self.end_record()
        if self.next_id:
            GLib.source_remove(self.next_id)
            self.next_id = 0
        self.status.set_text("已停止，保留最后一帧。")
        if self.pending:
            self.cancellable.cancel()
        else:
            self.release()
        self.update_buttons()

    def release(self):
        if self.device is not None and self.device.is_open():
            self.pending = True
            self.device.close(None, self.closed, None)
        elif self.closing:
            Gtk.main_quit()

    def closed(self, device, result, _=None):
        self.pending = False
        try:
            device.close_finish(result)
        except GLib.Error as error:
            self.status.set_text(f"关闭设备失败：{error}")
        self.device = None
        self.update_buttons()
        if self.closing:
            Gtk.main_quit()

    def on_delete(self, *_):
        self.closing = True
        if self.refresh_id:
            GLib.source_remove(self.refresh_id)
            self.refresh_id = 0
        self.hide()
        self.stop()
        return True


def run(args):
    if not Gtk.init_check()[0]:
        raise RuntimeError("无法连接桌面显示服务，请在图形桌面的终端中启动")
    window = PreviewWindow(args)
    window.show_all()
    if not args.preview:
        GLib.idle_add(window.start)

    def quit_window():
        window.on_delete()
        return GLib.SOURCE_CONTINUE

    signal_id = GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, quit_window)
    try:
        Gtk.main()
    finally:
        GLib.source_remove(signal_id)
        window.destroy()
