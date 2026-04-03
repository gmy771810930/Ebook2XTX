#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
电子书转换工具 - 图形化界面 (Ebook2XTX)
基于 tkinter，与 Ebook2XTX.py 共享核心处理逻辑
布局：
- 目录设置（固定顶部）
- 中间区域：PanedWindow 水平分割
  - 左侧：参数选项（双排，支持垂直滚动）
  - 右侧：日志区域（ScrolledText 自带滚动条）
- 转换进度（固定底部）
窗口最小尺寸保证关键控件可见
"""

import os
import sys
import threading
import queue
import logging
import webbrowser
from pathlib import Path
from datetime import datetime
from typing import Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 导入转换模块（修正模块名）
from ebook2xtx import convert_archives, parse_size_string
import ebook2xtx

# ========== 日志重定向到 GUI ==========
class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))

def setup_gui_logging(log_queue):
    handler = QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(handler)

# ========== 可滚动框架 ==========
class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def get_content_frame(self):
        return self.scrollable_frame

# ========== 主窗口类 ==========
class ConverterGUI:
    VERSION = "1.0"
    GITHUB_URL = "https://github.com/gmy771810930/Ebook2XTX"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Ebook2XTX")
        self.root.geometry("1100x750")
        self.root.minsize(900, 700)

        # 变量存储
        self.input_dir = tk.StringVar(value=str(Path.cwd()))
        self.output_dir = tk.StringVar(value=str(Path.cwd()))
        self.format_var = tk.IntVar(value=1)
        self.resolution_var = tk.IntVar(value=1)
        self.custom_width = tk.StringVar()
        self.custom_height = tk.StringVar()
        self.auto_crop_var = tk.BooleanVar(value=True)
        self.rotate_var = tk.IntVar(value=1)
        self.stretch_var = tk.BooleanVar(value=True)
        self.crop_mode_var = tk.IntVar(value=1)
        self.crop_sub_var = tk.IntVar(value=1)
        self.overlap_percent = tk.IntVar(value=100)
        self.dither_strength = tk.DoubleVar(value=0.7)
        self.max_workers = tk.IntVar(value=min(os.cpu_count() or 1, 61))
        self.filename_format_var = tk.IntVar(value=1)
        self.split_size_var = tk.IntVar(value=1)
        self.custom_split_size = tk.StringVar()
        self.gif_mode_var = tk.IntVar(value=1)

        # 动态控件引用
        self.custom_res_frame = None
        self.crop_sub_frame = None
        self.overlap_frame = None
        self.filename_frame = None
        self.split_frame = None
        self.split_custom_frame = None

        self.log_queue = queue.Queue()
        setup_gui_logging(self.log_queue)

        self.build_ui()
        self.update_logs()

        self.convert_thread = None
        self.stop_conversion = False

    def build_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 顶部：目录设置
        dir_frame = ttk.LabelFrame(main_frame, text="目录设置", padding="5")
        dir_frame.pack(fill=tk.X, padx=5, pady=5)
        dir_frame.columnconfigure(1, weight=1)
        ttk.Label(dir_frame, text="输入目录:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(dir_frame, textvariable=self.input_dir, width=50).grid(row=0, column=1, padx=5, sticky="ew")
        ttk.Button(dir_frame, text="浏览", command=self.browse_input).grid(row=0, column=2)
        ttk.Label(dir_frame, text="输出目录:").grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Entry(dir_frame, textvariable=self.output_dir, width=50).grid(row=1, column=1, padx=5, sticky="ew")
        ttk.Button(dir_frame, text="浏览", command=self.browse_output).grid(row=1, column=2)

        # 中间：PanedWindow
        paned = ttk.PanedWindow(main_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：参数选项（可滚动）
        left_scroll = ScrollableFrame(paned)
        left_content = left_scroll.get_content_frame()
        self._build_option_panels(left_content)
        paned.add(left_scroll, weight=2)

        # 右侧：日志区域
        log_frame = ttk.LabelFrame(paned, text="日志", padding="5")
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        paned.add(log_frame, weight=1)

        # 底部：转换进度
        progress_frame = ttk.LabelFrame(main_frame, text="转换进度", padding="5")
        progress_frame.pack(fill=tk.X, padx=5, pady=5)

        self.total_progress = ttk.Progressbar(progress_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.total_progress.pack(fill=tk.X, pady=2)
        self.total_label = ttk.Label(progress_frame, text="总进度: 0/0 (0%)")
        self.total_label.pack(anchor=tk.W)

        self.file_progress = ttk.Progressbar(progress_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.file_progress.pack(fill=tk.X, pady=2)
        self.file_label = ttk.Label(progress_frame, text="当前文件: 0/0 (0%)")
        self.file_label.pack(anchor=tk.W)

        btn_frame = ttk.Frame(progress_frame)
        btn_frame.pack(pady=5)
        self.start_btn = ttk.Button(btn_frame, text="开始转换", command=self.start_conversion)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_conversion_cmd, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        about_btn = ttk.Button(btn_frame, text="关于", command=self.show_about)
        about_btn.pack(side=tk.LEFT, padx=5)

        self.toggle_custom_res()
        self.toggle_crop_sub()
        self.toggle_filename_visibility()
        self.toggle_split_visibility()
        self.toggle_split_custom()

        self.format_var.trace_add('write', lambda *_: self.toggle_filename_visibility())
        self.format_var.trace_add('write', lambda *_: self.toggle_split_visibility())

    def _build_option_panels(self, parent):
        left_col = ttk.Frame(parent)
        left_col.grid(row=0, column=0, sticky="n", padx=5, pady=5)
        right_col = ttk.Frame(parent)
        right_col.grid(row=0, column=1, sticky="n", padx=5, pady=5)
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)

        # 左侧列
        format_frame = ttk.LabelFrame(left_col, text="输出格式", padding="5")
        format_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(format_frame, text="XTC (1-bit 黑白容器)", variable=self.format_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(format_frame, text="XTCH (2-bit 4级灰度容器)", variable=self.format_var, value=2).pack(anchor=tk.W)
        ttk.Radiobutton(format_frame, text="XTG (1-bit 黑白单页)", variable=self.format_var, value=3).pack(anchor=tk.W)
        ttk.Radiobutton(format_frame, text="XTH (2-bit 4级灰度单页)", variable=self.format_var, value=4).pack(anchor=tk.W)

        res_frame = ttk.LabelFrame(left_col, text="目标分辨率", padding="5")
        res_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(res_frame, text="X4 (480×800)", variable=self.resolution_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="X4 双倍 (960×1600)", variable=self.resolution_var, value=2).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="X3 (528×792)", variable=self.resolution_var, value=3).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="X3 双倍 (1056×1584)", variable=self.resolution_var, value=4).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="自定义", variable=self.resolution_var, value=5,
                        command=self.toggle_custom_res).pack(anchor=tk.W)
        self.custom_res_frame = ttk.Frame(res_frame)
        ttk.Label(self.custom_res_frame, text="宽:").pack(side=tk.LEFT)
        ttk.Entry(self.custom_res_frame, textvariable=self.custom_width, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Label(self.custom_res_frame, text="高:").pack(side=tk.LEFT)
        ttk.Entry(self.custom_res_frame, textvariable=self.custom_height, width=8).pack(side=tk.LEFT, padx=2)

        opt_frame = ttk.LabelFrame(left_col, text="图像处理", padding="5")
        opt_frame.pack(fill=tk.X, pady=5)
        ttk.Checkbutton(opt_frame, text="自动裁切黑白边", variable=self.auto_crop_var).pack(anchor=tk.W)
        ttk.Label(opt_frame, text="横版图片旋转方式:").pack(anchor=tk.W)
        rot_sub = ttk.Frame(opt_frame)
        rot_sub.pack(anchor=tk.W, padx=20)
        ttk.Radiobutton(rot_sub, text="顺时针90°", variable=self.rotate_var, value=1).pack(side=tk.LEFT)
        ttk.Radiobutton(rot_sub, text="逆时针90°", variable=self.rotate_var, value=2).pack(side=tk.LEFT)
        ttk.Radiobutton(rot_sub, text="不旋转", variable=self.rotate_var, value=3).pack(side=tk.LEFT)
        ttk.Checkbutton(opt_frame, text="拉伸至全屏", variable=self.stretch_var).pack(anchor=tk.W)

        # 右侧列
        crop_frame = ttk.LabelFrame(right_col, text="画面切割", padding="5")
        crop_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(crop_frame, text="不切割", variable=self.crop_mode_var, value=1,
                        command=self.toggle_crop_sub).pack(anchor=tk.W)
        ttk.Radiobutton(crop_frame, text="横切2图", variable=self.crop_mode_var, value=2,
                        command=self.toggle_crop_sub).pack(anchor=tk.W)
        ttk.Radiobutton(crop_frame, text="横切3图", variable=self.crop_mode_var, value=3,
                        command=self.toggle_crop_sub).pack(anchor=tk.W)
        self.crop_sub_frame = ttk.Frame(crop_frame)
        self.overlap_frame = ttk.Frame(crop_frame)

        other_frame = ttk.LabelFrame(right_col, text="高级选项", padding="5")
        other_frame.pack(fill=tk.X, pady=5)

        dither_frame = ttk.Frame(other_frame)
        dither_frame.pack(fill=tk.X, pady=2)
        ttk.Label(dither_frame, text="抖动强度 (0-1):").pack(side=tk.LEFT)
        self.dither_scale = ttk.Scale(dither_frame, from_=0, to=1, variable=self.dither_strength, orient=tk.HORIZONTAL, length=150)
        self.dither_scale.pack(side=tk.LEFT, padx=5)
        self.dither_label = ttk.Label(dither_frame, text=f"{self.dither_strength.get():.2f}")
        self.dither_label.pack(side=tk.LEFT)
        def on_dither_change(*args):
            self.dither_label.config(text=f"{self.dither_strength.get():.2f}")
        self.dither_strength.trace_add('write', on_dither_change)

        ttk.Label(other_frame, text="并发进程数:").pack(anchor=tk.W, pady=2)
        ttk.Spinbox(other_frame, from_=1, to=61, textvariable=self.max_workers, width=10).pack(anchor=tk.W)

        gif_frame = ttk.LabelFrame(right_col, text="GIF 处理", padding="5")
        gif_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(gif_frame, text="只处理第一帧", variable=self.gif_mode_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(gif_frame, text="处理所有帧", variable=self.gif_mode_var, value=2).pack(anchor=tk.W)

        self.filename_frame = ttk.LabelFrame(right_col, text="单页文件名格式", padding="5")
        ttk.Radiobutton(self.filename_frame, text="编号 (例如 1.xtg)", variable=self.filename_format_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(self.filename_frame, text="漫画名-编号 (例如 漫画名-1.xtg)", variable=self.filename_format_var, value=2).pack(anchor=tk.W)

        self.split_frame = ttk.LabelFrame(right_col, text="容器分包大小", padding="5")
        ttk.Radiobutton(self.split_frame, text="4GB (FAT32)", variable=self.split_size_var, value=1,
                        command=self.toggle_split_custom).pack(anchor=tk.W)
        ttk.Radiobutton(self.split_frame, text="自定义", variable=self.split_size_var, value=2,
                        command=self.toggle_split_custom).pack(anchor=tk.W)
        ttk.Radiobutton(self.split_frame, text="不分包", variable=self.split_size_var, value=3,
                        command=self.toggle_split_custom).pack(anchor=tk.W)
        self.split_custom_frame = ttk.Frame(self.split_frame)
        ttk.Label(self.split_custom_frame, text="大小:").pack(side=tk.LEFT)
        ttk.Entry(self.split_custom_frame, textvariable=self.custom_split_size, width=15).pack(side=tk.LEFT, padx=5)
        ttk.Label(self.split_custom_frame, text="(支持 k/KB/m/MB/g/GB，默认MB)").pack(side=tk.LEFT)

    def toggle_custom_res(self):
        if self.custom_res_frame:
            if self.resolution_var.get() == 5:
                self.custom_res_frame.pack(anchor=tk.W, padx=20, pady=2)
            else:
                self.custom_res_frame.pack_forget()

    def toggle_crop_sub(self):
        for widget in self.crop_sub_frame.winfo_children():
            widget.destroy()
        self.overlap_frame.pack_forget()
        mode = self.crop_mode_var.get()
        if mode == 2:
            if self.crop_sub_var.get() not in (1,2,3):
                self.crop_sub_var.set(1)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1.618 (黄金比例)", variable=self.crop_sub_var, value=1).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="1.618 : 1", variable=self.crop_sub_var, value=2).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1 (上下等分，滚动模式)", variable=self.crop_sub_var, value=3,
                            command=self.toggle_overlap).pack(anchor=tk.W)
            self.crop_sub_frame.pack(anchor=tk.W, padx=20, pady=2)
            self.toggle_overlap()
        elif mode == 3:
            if self.crop_sub_var.get() not in (1,2,3,4):
                self.crop_sub_var.set(1)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 2 : 1", variable=self.crop_sub_var, value=1).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="2 : 1 : 1", variable=self.crop_sub_var, value=2).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1 : 2", variable=self.crop_sub_var, value=3).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1 : 1 (5图滚动模式)", variable=self.crop_sub_var, value=4,
                            command=self.toggle_overlap).pack(anchor=tk.W)
            self.crop_sub_frame.pack(anchor=tk.W, padx=20, pady=2)
            self.toggle_overlap()
        else:
            self.crop_sub_frame.pack_forget()

    def toggle_overlap(self):
        for widget in self.overlap_frame.winfo_children():
            widget.destroy()
        self.overlap_frame.pack_forget()
        mode = self.crop_mode_var.get()
        sub = self.crop_sub_var.get()
        if (mode == 2 and sub == 3) or (mode == 3 and sub == 4):
            ttk.Label(self.overlap_frame, text="重叠比例 (0-100%):").pack(side=tk.LEFT)
            ttk.Spinbox(self.overlap_frame, from_=0, to=100, textvariable=self.overlap_percent, width=5).pack(side=tk.LEFT, padx=5)
            self.overlap_frame.pack(anchor=tk.W, padx=20, pady=2)

    def toggle_filename_visibility(self):
        if self.format_var.get() in (3, 4):
            self.filename_frame.pack(fill=tk.X, pady=5)
        else:
            self.filename_frame.pack_forget()

    def toggle_split_visibility(self):
        if self.format_var.get() in (1, 2):
            self.split_frame.pack(fill=tk.X, pady=5)
        else:
            self.split_frame.pack_forget()

    def toggle_split_custom(self):
        if self.split_size_var.get() == 2:
            self.split_custom_frame.pack(anchor=tk.W, padx=20)
        else:
            self.split_custom_frame.pack_forget()

    def browse_input(self):
        dirname = filedialog.askdirectory(title="选择包含压缩包的目录", initialdir=self.input_dir.get())
        if dirname:
            self.input_dir.set(dirname)

    def browse_output(self):
        dirname = filedialog.askdirectory(title="选择输出目录", initialdir=self.output_dir.get())
        if dirname:
            self.output_dir.set(dirname)

    def get_resolution(self):
        res = self.resolution_var.get()
        if res == 1:
            return 480, 800
        elif res == 2:
            return 960, 1600
        elif res == 3:
            return 528, 792
        elif res == 4:
            return 1056, 1584
        else:
            try:
                w = int(self.custom_width.get())
                h = int(self.custom_height.get())
                if w > 0 and h > 0:
                    return w, h
                else:
                    raise ValueError
            except:
                messagebox.showerror("错误", "自定义分辨率格式错误，使用默认 X4 (480x800)")
                return 480, 800

    def get_crop_settings(self):
        mode = self.crop_mode_var.get()
        if mode == 1:
            return {'mode': 0, 'ratio': None}
        if mode == 2:
            sub = self.crop_sub_var.get()
            if sub == 1:
                ratio = (1, 1.618)
            elif sub == 2:
                ratio = (1.618, 1)
            else:
                return {'mode': 4, 'overlap_percent': self.overlap_percent.get()}
            return {'mode': 2, 'ratio': ratio}
        elif mode == 3:
            sub = self.crop_sub_var.get()
            if sub == 1:
                ratio = (1, 2, 1)
            elif sub == 2:
                ratio = (2, 1, 1)
            elif sub == 3:
                ratio = (1, 1, 2)
            else:
                return {'mode': 5, 'overlap_percent': self.overlap_percent.get()}
            return {'mode': 3, 'ratio': ratio}
        return {'mode': 0, 'ratio': None}

    def get_split_size(self):
        choice = self.split_size_var.get()
        if choice == 1:
            return 4 * 1024 * 1024 * 1024
        elif choice == 2:
            size_str = self.custom_split_size.get().strip()
            if not size_str:
                return 0
            try:
                return parse_size_string(size_str)
            except ValueError as e:
                messagebox.showerror("错误", f"分包大小格式错误: {e}")
                return 0
        else:
            return 0

    def build_settings(self):
        width, height = self.get_resolution()
        rotate_map = {1: "clockwise", 2: "counterclockwise", 3: "none"}
        rotate_mode = rotate_map[self.rotate_var.get()]
        fmt_map = {1: "xtc", 2: "xtch", 3: "xtg", 4: "xth"}
        output_format = fmt_map[self.format_var.get()]
        return {
            'format': output_format,
            'width': width,
            'height': height,
            'auto_crop': self.auto_crop_var.get(),
            'rotate_mode': rotate_mode,
            'crop': self.get_crop_settings(),
            'stretch': self.stretch_var.get(),
            'dither_strength': self.dither_strength.get(),
            'max_workers': self.max_workers.get(),
            'filename_format': self.filename_format_var.get() - 1 if output_format in ('xtg', 'xth') else None,
            'split_size': self.get_split_size() if output_format in ('xtc', 'xtch') else None,
            'gif_mode': self.gif_mode_var.get()
        }

    def start_conversion(self):
        input_dir = self.input_dir.get().strip()
        output_dir = self.output_dir.get().strip()
        if not input_dir or not output_dir:
            messagebox.showerror("错误", "请选择输入和输出目录")
            return
        # 修正依赖检查调用
        if not ebook2xtx.check_and_install_dependencies():
            messagebox.showerror("错误", "依赖安装失败，请手动安装后再试")
            return

        settings = self.build_settings()
        self.stop_conversion = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.total_progress['value'] = 0
        self.total_label.config(text="总进度: 0/0 (0%)")
        self.file_progress['value'] = 0
        self.file_label.config(text="当前文件: 0/0 (0%)")

        def run():
            try:
                success = convert_archives(Path(input_dir), Path(output_dir), settings,
                                           overall_progress_callback=self.update_progress)
                self.root.after(0, self.conversion_finished, success)
            except Exception as e:
                logging.exception("转换过程异常")
                self.root.after(0, self.conversion_error, str(e))

        self.convert_thread = threading.Thread(target=run)
        self.convert_thread.start()

    def update_progress(self, archive_name, current, total):
        self.root.after(0, lambda: self._update_progress_ui(archive_name, current, total))

    def _update_progress_ui(self, archive_name, current, total):
        if total > 0:
            percent = current / total * 100
            self.total_progress['value'] = percent
            self.total_label.config(text=f"总进度: {current}/{total} ({percent:.1f}%)")
        else:
            self.total_progress['value'] = 0
            self.total_label.config(text="总进度: 准备中...")
        self.file_label.config(text=f"当前文件: {archive_name} ({current}/{total})")

    def conversion_finished(self, success_count):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.total_label.config(text=f"转换完成，成功处理 {success_count} 个压缩包")
        self.total_progress['value'] = 100
        self.file_label.config(text="完成")
        self.file_progress['value'] = 100
        messagebox.showinfo("完成", f"转换完成！成功处理 {success_count} 个压缩包。")

    def conversion_error(self, error_msg):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.total_label.config(text="转换出错")
        messagebox.showerror("错误", f"转换过程中发生错误:\n{error_msg}")

    def stop_conversion_cmd(self):
        self.stop_conversion = True
        self.stop_btn.config(state=tk.DISABLED)
        self.total_label.config(text="正在停止（可能需要等待当前任务完成）...")
        messagebox.showinfo("提示", "停止操作可能需要等待当前图片处理完成，请耐心等待。")

    def update_logs(self):
        try:
            while True:
                record = self.log_queue.get_nowait()
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, record + "\n")
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.root.after(500, self.update_logs)

    def show_about(self):
        about_window = tk.Toplevel(self.root)
        about_window.title("关于")
        about_window.geometry("400x180")
        about_window.resizable(False, False)
        about_window.transient(self.root)
        about_window.grab_set()
        about_window.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 180) // 2
        about_window.geometry(f"+{x}+{y}")

        frame = ttk.Frame(about_window, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Ebook2XTX", font=("微软雅黑", 16, "bold")).pack(pady=5)
        ttk.Label(frame, text=f"版本 {self.VERSION}").pack(pady=2)
        ttk.Label(frame, text="漫画电子书格式转换工具").pack(pady=2)
        link_frame = ttk.Frame(frame)
        link_frame.pack(pady=10)
        ttk.Label(link_frame, text="GitHub: ").pack(side=tk.LEFT)
        url_label = ttk.Label(link_frame, text=self.GITHUB_URL, foreground="blue", cursor="hand2")
        url_label.pack(side=tk.LEFT)
        url_label.bind("<Button-1>", lambda e: webbrowser.open(self.GITHUB_URL))

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if multiprocessing.current_process().name == 'MainProcess':
        app = ConverterGUI()
        app.run()