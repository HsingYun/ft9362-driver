# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Device-test wizard opened from the live preview after its USB close finishes."""

from pathlib import Path
import threading

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk

from device_testing import STATUS, TestSession, plan
from frame import decode_preview


class DeviceTestWindow(Gtk.Window):
    def __init__(self, parent=None, on_closed=None, scope="full", root=None):
        super().__init__(title="FT9362 · 设备测试")
        if parent is not None:
            self.set_transient_for(parent)
            self.set_modal(True)
        self.on_closed = on_closed or (lambda: None)
        self.root = root or Path(__file__).resolve().parents[1] / "build" / "diagnostics"
        self.session = None
        self.busy = False
        self.closing = False
        self.restart = False
        self.set_default_size(940, 740)
        self.set_border_width(16)
        self.connect("delete-event", self.on_close)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)
        self.scope = Gtk.ComboBoxText()
        for key, label in (("full", "完整复现：无手指 + 动作 + 通道对照（共按压 3 次）"),
                           ("no_finger", "仅无手指测试"), ("movement", "仅手指动作（按压 2 次）"),
                           ("comparison", "仅通道对照（按压 1 次）")):
            self.scope.append(key, label)
        self.scope.set_active_id(scope)
        self.scope.connect("changed", lambda _: self.refresh())
        box.pack_start(self.scope, False, False, 0)
        middle = Gtk.Box(spacing=18)
        box.pack_start(middle, True, True, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        middle.pack_start(scroll, True, True, 0)
        self.rows = Gtk.ListStore(str, str)
        tree = Gtk.TreeView(model=self.rows)
        tree.append_column(Gtk.TreeViewColumn("步骤", Gtk.CellRendererText(), text=0))
        tree.append_column(Gtk.TreeViewColumn("结果", Gtk.CellRendererText(), text=1))
        scroll.add(tree)
        self.picture = Gtk.Image()
        self.picture.set_size_request(120, 456)
        middle.pack_start(self.picture, False, False, 0)
        self.prompt = Gtk.Label(xalign=0)
        self.prompt.set_line_wrap(True)
        box.pack_start(self.prompt, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_selectable(True)
        box.pack_start(self.status, False, False, 0)
        self.progress = Gtk.ProgressBar()
        box.pack_start(self.progress, False, False, 0)
        controls = Gtk.Box(spacing=8)
        box.pack_start(controls, False, False, 0)
        self.next_button = Gtk.Button(label="创建本轮测试")
        self.next_button.connect("clicked", self.start_step)
        controls.pack_start(self.next_button, False, False, 0)
        self.stop_button = Gtk.Button(label="停止并生成报告")
        self.stop_button.connect("clicked", self.stop_session)
        controls.pack_start(self.stop_button, False, False, 0)
        self.restart_button = Gtk.Button(label="作废本轮并重来")
        self.restart_button.connect("clicked", self.restart_session)
        controls.pack_start(self.restart_button, False, False, 0)
        self.report_button = Gtk.Button(label="打开报告目录")
        self.report_button.connect("clicked", self.open_report)
        controls.pack_start(self.report_button, False, False, 0)
        self.refresh()

    def refresh(self):
        self.rows.clear()
        if self.session is None:
            for step in plan(self.scope.get_active_id()):
                self.rows.append([step.title, STATUS["pending"]])
            self.prompt.set_text("选择测试范围并创建本轮；随后每一步都由你点击执行。\n"
                                 "全程串行占用设备。包含单设备 USB 复位；不发送固件、EC/GPIO 或运行模式写入。")
            self.status.set_text("预览已暂停。手指动作与抬手时机会明确显示在这里。")
            self.next_button.set_label("创建本轮测试")
        else:
            session = self.session
            for row in session.summary["steps"]:
                self.rows.append([row["title"], STATUS[row["status"]]])
            if session.terminal:
                self.prompt.set_text("本轮已结束，请保持手指离开传感器。关闭测试窗口后可手动恢复预览。")
            else:
                step = session.steps[session.index]
                self.prompt.set_text(f"{session.index + 1}/{len(session.steps)} · {step.title}\n{step.instruction}")
            self.status.set_text(f"{STATUS[session.summary['status']]}\n报告：{session.directory / 'report.md'}")
            self.next_button.set_label("确认动作，执行这一步")
        active = self.session is not None and not self.session.terminal
        self.scope.set_sensitive(self.session is None and not self.busy)
        self.next_button.set_sensitive(not self.busy and not self.closing and (self.session is None or active))
        self.stop_button.set_sensitive(active and not self.closing)
        self.restart_button.set_sensitive(self.session is not None and not self.busy and not self.closing)
        self.report_button.set_sensitive(self.session is not None)

    def worker(self, operation):
        self.busy = True
        self.refresh()

        def execute():
            error = None
            try:
                operation()
            except Exception as exception:
                error = str(exception)
                if self.session:
                    self.session.stop.set()
                    try:
                        self.session.finish()
                    except Exception:
                        pass
            GLib.idle_add(self.finished, error)
        threading.Thread(target=execute, daemon=False).start()

    def start_step(self, *_):
        if self.busy or self.closing:
            return
        if self.session is None:
            try:
                self.session = TestSession(self.root, self.scope.get_active_id(),
                    progress=lambda *args: GLib.idle_add(self.show_frame, *args))
            except Exception as error:
                self.status.set_text(f"无法创建报告目录：{error}")
                return
            self.refresh()
        else:
            self.progress.set_fraction(0)
            self.worker(self.session.run_next)
            step = self.session.steps[self.session.index] if not self.session.terminal else None
            if step and step.phase in ("hold", "repress", "compare"):
                self.status.set_text("现在请放上手指，保持接触，等待下一步提示。")

    def show_frame(self, raw, phase, index, count):
        if self.closing:
            return False
        if len(raw) == 12166:
            width, height, pixels = decode_preview(raw)
            rgb = bytes(channel for pixel in pixels for channel in (pixel, pixel, pixel))
            pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(rgb), GdkPixbuf.Colorspace.RGB,
                                                   False, 8, width, height, width * 3)
            self.picture.set_from_pixbuf(pixbuf.scale_simple(120, 456, GdkPixbuf.InterpType.NEAREST))
        names = {"normal_before": "A：只读普通帧", "status_only": "B：加入扩展状态",
                 "with_extended": "C：加入扩展取图", "normal_after": "D：回到普通帧"}
        self.progress.set_fraction(index / count)
        self.status.set_text(f"{names.get(phase, phase)} · {index}/{count} 帧\n保持当前动作，等下一步提示。")
        return False

    def finished(self, error):
        self.busy = False
        if self.closing:
            self.worker_close()
            return False
        if self.session and self.session.stop.is_set() and not self.session.terminal:
            self.worker(self.session.finish)
            return False
        if self.restart:
            self.restart = False
            self.session = None
            self.picture.clear()
        self.refresh()
        if error:
            self.status.set_text(f"执行或保存报告失败：{error}")
        return False

    def stop_session(self, *_):
        if self.session is None:
            return
        self.session.stop.set()
        self.status.set_text("正在结束当前传输、释放设备并保存报告……")
        self.stop_button.set_sensitive(False)
        if not self.busy:
            self.worker(self.session.finish)

    def restart_session(self, *_):
        if self.busy or self.session is None:
            return
        self.restart = True
        self.session.stop.set()
        self.worker(lambda: self.session.finish(invalidated=True))

    def open_report(self, *_):
        if self.session is not None:
            try:
                Gtk.show_uri_on_window(self, self.session.directory.as_uri(), 0)
            except GLib.Error as error:
                self.status.set_text(f"无法打开目录：{error}\n{self.session.directory}")

    def worker_close(self):
        if self.session is None:
            self.destroy()
            self.on_closed()
            return
        # Clear closing while the final cleanup worker runs to avoid rescheduling it.
        session = self.session
        self.busy = True

        def close():
            error = None
            try:
                session.finish()
            except Exception as exception:
                error = str(exception)
            GLib.idle_add(self.closed, error)
        threading.Thread(target=close, daemon=False).start()

    def closed(self, error):
        self.busy = False
        if error:
            self.closing = False
            self.refresh()
            self.status.set_text(f"报告保存失败：{error}。设备释放已尝试，可再次关闭。")
            return False
        self.destroy()
        self.on_closed()
        return False

    def on_close(self, *_):
        if self.closing:
            return True
        self.closing = True
        if self.session:
            self.session.stop.set()
        self.next_button.set_sensitive(False)
        self.status.set_text("正在释放设备并保存报告……")
        if not self.busy:
            self.worker_close()
        return True
