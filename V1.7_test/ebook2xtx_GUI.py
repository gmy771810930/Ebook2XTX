#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
电子书转换工具 - 图形化界面 (Ebook2XTX v1.7)
基于 tkinter，与 Ebook2XTX.py 共享核心处理逻辑
支持输出图片格式、原分辨率、电子书输入等
支持输出电子书格式（EPUB/PDF）
新增：独立预览窗口、独立日志窗口、预览实时渲染
"""

import os
import sys
import subprocess
import threading
import queue
import logging
import webbrowser
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ========== 前置依赖检查 ==========
def ensure_dependencies():
    required = [
        ('Pillow', 'from PIL import Image'),
        ('py7zr', 'import py7zr'),
        ('rarfile', 'import rarfile'),
        ('natsort', 'from natsort import natsorted'),
        ('numpy', 'import numpy as np'),
        ('numba', 'from numba import njit'),
        ('PyMuPDF', 'import fitz'),
        ('ebooklib', 'import ebooklib'),
        ('BeautifulSoup4', 'from bs4 import BeautifulSoup'),
        ('mobi', 'import mobi'),
        ('img2pdf', 'import img2pdf'),
        ('opencv-python', 'import cv2')
    ]
    missing = []
    for pkg, stmt in required:
        try:
            exec(stmt)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"正在安装缺失的依赖: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
        for pkg, stmt in required:
            exec(stmt)
        print("依赖安装完成。")

ensure_dependencies()

from ebook2xtx import (
    parse_size_string, check_and_install_dependencies, scan_input_items,
    InputItem, sanitize_filename, process_images, process_images_to_ebook
)

# ========== 检测预览模块是否存在 ==========
PREVIEW_AVAILABLE = False
try:
    # 尝试导入，同时检测文件是否存在
    from ebook2xtx_gui_viewer import PreviewWindow
    PREVIEW_AVAILABLE = True
except ImportError:
    PreviewWindow = None
    print("提示: 未找到 ebook2xtx_gui_viewer.py，预览功能不可用")

# ========== 日志重定向 ==========
class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))

def setup_gui_logging(log_queue, enable_file_log=False):
    for handler in logging.getLogger().handlers[:]:
        logging.getLogger().removeHandler(handler)
    handler = QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(handler)
    if enable_file_log:
        log_dir = Path.cwd() / "log"
        log_dir.mkdir(exist_ok=True)
        log_filename = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        file_handler = logging.FileHandler(log_filename, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(file_handler)

# ========== 独立日志窗口 ==========
class LogWindow(tk.Toplevel):
    def __init__(self, parent, log_queue):
        super().__init__(parent)
        self.title("Ebook2XTX 日志")
        self.geometry("800x500")
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.log_text = scrolledtext.ScrolledText(self, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_queue = log_queue
        self.after(500, self.update_logs)

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
        self.after(500, self.update_logs)

    def hide(self):
        self.withdraw()

    def show(self):
        self.deiconify()

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
    VERSION = "1.7"
    GITHUB_URL = "https://github.com/gmy771810930/Ebook2XTX"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Ebook2XTX")
        self.root.geometry("1200x900")
        self.root.minsize(1000, 800)

        # 变量存储
        self.input_dir = tk.StringVar(value=str(Path.cwd()))
        self.output_dir = tk.StringVar(value=str(Path.cwd()))
        self.format_var = tk.IntVar(value=1)
        self.image_format_var = tk.StringVar(value="jpg")
        self.ebook_format_var = tk.StringVar(value="epub")
        self.resolution_var = tk.IntVar(value=1)
        self.custom_width = tk.StringVar()
        self.custom_height = tk.StringVar()
        self.auto_crop_var = tk.BooleanVar(value=True)
        self.rotate_var = tk.IntVar(value=1)
        self.stretch_var = tk.BooleanVar(value=True)
        self.crop_mode_var = tk.IntVar(value=1)
        self.crop_sub_var = tk.IntVar(value=1)
        self.overlap_percent = tk.IntVar(value=100)
        self.dither_strength = tk.DoubleVar(value=70)  # 0-100
        self.max_workers = tk.IntVar(value=min(os.cpu_count() or 1, 61))
        self.filename_format_var = tk.IntVar(value=1)
        self.split_size_var = tk.IntVar(value=1)
        self.custom_split_size = tk.StringVar()
        self.gif_mode_var = tk.IntVar(value=1)
        self.enable_file_log = tk.BooleanVar(value=False)
        # 新增参数
        self.sharpen_var = tk.IntVar(value=0)
        self.contrast_var = tk.IntVar(value=0)
        self.clahe_var = tk.IntVar(value=0)
        self.dither_algo_var = tk.StringVar(value="Floyd-Steinberg")
        self.output_bits_var = tk.IntVar(value=8)

        # 动态控件引用
        self.custom_res_frame = None
        self.crop_sub_frame = None
        self.overlap_frame = None
        self.filename_frame = None
        self.split_frame = None
        self.split_custom_frame = None
        self.image_format_frame = None
        self.ebook_format_frame = None

        self.log_queue = queue.Queue()
        setup_gui_logging(self.log_queue, enable_file_log=False)

        # 独立窗口引用（先初始化为 None）
        self.log_window = None
        self.preview_window = None

        self.build_ui()
        self.update_logs()

        self.convert_thread = None
        self.stop_conversion = False

        self.text_choice_memory = None
        self.mixed_choice_memory = None

        # 日志提示预览模块状态
        if not PREVIEW_AVAILABLE:
            logging.warning("未找到 ebook2xtx_gui_viewer.py，预览功能不可用，预览按钮已禁用")

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

        # 中间：参数选项（可滚动）
        self.param_scroll = ScrollableFrame(main_frame)
        self.param_scroll.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        param_content = self.param_scroll.get_content_frame()
        self._build_option_panels(param_content)

        # 底部：转换进度和控制按钮
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

        # 预览按钮（根据模块可用性决定状态）
        self.preview_btn = ttk.Button(btn_frame, text="预览", command=self.toggle_preview_window)
        if not PREVIEW_AVAILABLE:
            self.preview_btn.config(state=tk.DISABLED)
        self.preview_btn.pack(side=tk.LEFT, padx=5)

        self.log_window_btn = ttk.Button(btn_frame, text="打开日志窗口", command=self.toggle_log_window)
        self.log_window_btn.pack(side=tk.LEFT, padx=5)
        about_btn = ttk.Button(btn_frame, text="关于", command=self.show_about)
        about_btn.pack(side=tk.LEFT, padx=5)

        log_check_frame = ttk.Frame(progress_frame)
        log_check_frame.pack(pady=2)
        self.log_check = ttk.Checkbutton(log_check_frame, text="输出日志到 log 文件", variable=self.enable_file_log)
        self.log_check.pack(side=tk.LEFT)

        self.toggle_custom_res()
        self.toggle_crop_sub()
        self.toggle_filename_visibility()
        self.toggle_split_visibility()
        self.toggle_split_custom()
        self.toggle_image_format_visibility()
        self.toggle_ebook_format_visibility()

        self.format_var.trace_add('write', lambda *_: self.toggle_filename_visibility())
        self.format_var.trace_add('write', lambda *_: self.toggle_split_visibility())
        self.format_var.trace_add('write', lambda *_: self.toggle_image_format_visibility())
        self.format_var.trace_add('write', lambda *_: self.toggle_ebook_format_visibility())
        self.resolution_var.trace_add('write', lambda *_: self.toggle_stretch_visibility())
        self.format_var.trace_add('write', lambda *_: self.update_bits_default())

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
        ttk.Radiobutton(format_frame, text="图片格式", variable=self.format_var, value=5).pack(anchor=tk.W)
        self.image_format_frame = ttk.Frame(format_frame)
        ttk.Label(self.image_format_frame, text="格式:").pack(side=tk.LEFT)
        ttk.Radiobutton(self.image_format_frame, text="JPEG", variable=self.image_format_var, value="jpg").pack(side=tk.LEFT)
        ttk.Radiobutton(self.image_format_frame, text="PNG", variable=self.image_format_var, value="png").pack(side=tk.LEFT)
        ttk.Radiobutton(self.image_format_frame, text="WebP", variable=self.image_format_var, value="webp").pack(side=tk.LEFT)
        ttk.Radiobutton(self.image_format_frame, text="BMP", variable=self.image_format_var, value="bmp").pack(side=tk.LEFT)
        ttk.Radiobutton(format_frame, text="电子书格式", variable=self.format_var, value=6).pack(anchor=tk.W)
        self.ebook_format_frame = ttk.Frame(format_frame)
        ttk.Label(self.ebook_format_frame, text="格式:").pack(side=tk.LEFT)
        ttk.Radiobutton(self.ebook_format_frame, text="EPUB", variable=self.ebook_format_var, value="epub").pack(side=tk.LEFT)
        ttk.Radiobutton(self.ebook_format_frame, text="PDF", variable=self.ebook_format_var, value="pdf").pack(side=tk.LEFT)

        res_frame = ttk.LabelFrame(left_col, text="目标分辨率", padding="5")
        res_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(res_frame, text="X4 (480×800)", variable=self.resolution_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="X4 双倍 (960×1600)", variable=self.resolution_var, value=2).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="X3 (528×792)", variable=self.resolution_var, value=3).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="X3 双倍 (1056×1584)", variable=self.resolution_var, value=4).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="原分辨率", variable=self.resolution_var, value=5).pack(anchor=tk.W)
        ttk.Radiobutton(res_frame, text="自定义", variable=self.resolution_var, value=6, command=self.toggle_custom_res).pack(anchor=tk.W)
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
        self.stretch_check = ttk.Checkbutton(opt_frame, text="拉伸至全屏", variable=self.stretch_var)
        self.stretch_check.pack(anchor=tk.W)

        # 右侧列：抖动算法与增强
        dither_frame = ttk.LabelFrame(right_col, text="抖动算法与增强", padding="5")
        dither_frame.pack(fill=tk.X, pady=5)

        # 抖动算法
        algo_row = ttk.Frame(dither_frame)
        algo_row.pack(fill=tk.X, pady=2)
        ttk.Label(algo_row, text="抖动算法:").pack(side=tk.LEFT)
        algo_combo = ttk.Combobox(algo_row, textvariable=self.dither_algo_var, values=["Floyd-Steinberg", "Atkinson", "None"], state="readonly")
        algo_combo.pack(side=tk.LEFT, padx=5)
        algo_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview_if_open())

        # 位深度
        bits_row = ttk.Frame(dither_frame)
        bits_row.pack(fill=tk.X, pady=2)
        ttk.Label(bits_row, text="位深度 (1-16):").pack(side=tk.LEFT)
        self.bits_spin = ttk.Spinbox(bits_row, from_=1, to=16, textvariable=self.output_bits_var, width=5)
        self.bits_spin.pack(side=tk.LEFT, padx=5)
        self.bits_spin.bind("<KeyRelease>", lambda e: self.validate_bits())
        self.bits_spin.bind("<<Increment>>", lambda e: self.validate_bits())
        self.bits_spin.bind("<<Decrement>>", lambda e: self.validate_bits())

        # 抖动强度
        dither_int_row = ttk.Frame(dither_frame)
        dither_int_row.pack(fill=tk.X, pady=2)
        ttk.Label(dither_int_row, text="抖动强度 (0-100%):").pack(side=tk.LEFT)
        self.dither_scale = ttk.Scale(dither_int_row, from_=0, to=100, variable=self.dither_strength, orient=tk.HORIZONTAL, length=150)
        self.dither_scale.pack(side=tk.LEFT, padx=5)
        self.dither_label = ttk.Label(dither_int_row, text=f"{self.dither_strength.get():.0f}%")
        self.dither_label.pack(side=tk.LEFT)
        self.dither_strength.trace_add('write', lambda *_: self.dither_label.config(text=f"{self.dither_strength.get():.0f}%"))

        # 锐化
        sharpen_row = ttk.Frame(dither_frame)
        sharpen_row.pack(fill=tk.X, pady=2)
        ttk.Label(sharpen_row, text="锐化强度 (0-100%):").pack(side=tk.LEFT)
        self.sharpen_scale = ttk.Scale(sharpen_row, from_=0, to=100, variable=self.sharpen_var, orient=tk.HORIZONTAL, length=150)
        self.sharpen_scale.pack(side=tk.LEFT, padx=5)
        self.sharpen_label = ttk.Label(sharpen_row, text=f"{self.sharpen_var.get():.0f}%")
        self.sharpen_label.pack(side=tk.LEFT)
        self.sharpen_var.trace_add('write', lambda *_: self.sharpen_label.config(text=f"{self.sharpen_var.get():.0f}%"))

        # 对比度
        contrast_row = ttk.Frame(dither_frame)
        contrast_row.pack(fill=tk.X, pady=2)
        ttk.Label(contrast_row, text="对比度强度 (0-100%):").pack(side=tk.LEFT)
        self.contrast_scale = ttk.Scale(contrast_row, from_=0, to=100, variable=self.contrast_var, orient=tk.HORIZONTAL, length=150)
        self.contrast_scale.pack(side=tk.LEFT, padx=5)
        self.contrast_label = ttk.Label(contrast_row, text=f"{self.contrast_var.get():.0f}%")
        self.contrast_label.pack(side=tk.LEFT)
        self.contrast_var.trace_add('write', lambda *_: self.contrast_label.config(text=f"{self.contrast_var.get():.0f}%"))

        # 局部对比度增强
        clahe_row = ttk.Frame(dither_frame)
        clahe_row.pack(fill=tk.X, pady=2)
        ttk.Label(clahe_row, text="局部对比度增强 (0-100%):").pack(side=tk.LEFT)
        self.clahe_scale = ttk.Scale(clahe_row, from_=0, to=100, variable=self.clahe_var, orient=tk.HORIZONTAL, length=150)
        self.clahe_scale.pack(side=tk.LEFT, padx=5)
        self.clahe_label = ttk.Label(clahe_row, text=f"{self.clahe_var.get():.0f}%")
        self.clahe_label.pack(side=tk.LEFT)
        self.clahe_var.trace_add('write', lambda *_: self.clahe_label.config(text=f"{self.clahe_var.get():.0f}%"))

        # 画面切割
        crop_frame = ttk.LabelFrame(right_col, text="画面切割", padding="5")
        crop_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(crop_frame, text="不切割", variable=self.crop_mode_var, value=1, command=self.toggle_crop_sub).pack(anchor=tk.W)
        ttk.Radiobutton(crop_frame, text="横切2图", variable=self.crop_mode_var, value=2, command=self.toggle_crop_sub).pack(anchor=tk.W)
        ttk.Radiobutton(crop_frame, text="横切3图", variable=self.crop_mode_var, value=3, command=self.toggle_crop_sub).pack(anchor=tk.W)
        self.crop_sub_frame = ttk.Frame(crop_frame)
        self.overlap_frame = ttk.Frame(crop_frame)

        # 高级选项
        other_frame = ttk.LabelFrame(right_col, text="高级选项", padding="5")
        other_frame.pack(fill=tk.X, pady=5)
        ttk.Label(other_frame, text="并发进程数:").pack(anchor=tk.W, pady=2)
        ttk.Spinbox(other_frame, from_=1, to=61, textvariable=self.max_workers, width=10).pack(anchor=tk.W)

        # GIF 处理
        gif_frame = ttk.LabelFrame(right_col, text="GIF 处理", padding="5")
        gif_frame.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(gif_frame, text="只处理第一帧", variable=self.gif_mode_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(gif_frame, text="处理所有帧", variable=self.gif_mode_var, value=2).pack(anchor=tk.W)
        ttk.Radiobutton(gif_frame, text="跳过 GIF 文件（不转换）", variable=self.gif_mode_var, value=3).pack(anchor=tk.W)

        self.filename_frame = ttk.LabelFrame(right_col, text="单页文件名格式", padding="5")
        self.split_frame = ttk.LabelFrame(right_col, text="容器分包大小", padding="5")
        ttk.Radiobutton(self.split_frame, text="4GB (FAT32)", variable=self.split_size_var, value=1, command=self.toggle_split_custom).pack(anchor=tk.W)
        ttk.Radiobutton(self.split_frame, text="自定义", variable=self.split_size_var, value=2, command=self.toggle_split_custom).pack(anchor=tk.W)
        ttk.Radiobutton(self.split_frame, text="不分包", variable=self.split_size_var, value=3, command=self.toggle_split_custom).pack(anchor=tk.W)
        self.split_custom_frame = ttk.Frame(self.split_frame)
        ttk.Label(self.split_custom_frame, text="大小:").pack(side=tk.LEFT)
        ttk.Entry(self.split_custom_frame, textvariable=self.custom_split_size, width=15).pack(side=tk.LEFT, padx=5)
        ttk.Label(self.split_custom_frame, text="(支持 k/KB/m/MB/g/GB，默认MB)").pack(side=tk.LEFT)

        self.update_filename_example()

        # 绑定参数变化刷新预览
        self.bind_preview_refresh()

    def bind_preview_refresh(self):
        vars_to_trace = [
            self.format_var, self.resolution_var, self.auto_crop_var, self.rotate_var,
            self.stretch_var, self.crop_mode_var, self.crop_sub_var, self.overlap_percent,
            self.dither_strength, self.sharpen_var, self.contrast_var, self.clahe_var,
            self.dither_algo_var, self.output_bits_var, self.image_format_var, self.ebook_format_var,
            self.custom_width, self.custom_height
        ]
        for var in vars_to_trace:
            var.trace_add('write', lambda *_: self.refresh_preview_if_open())

    def refresh_preview_if_open(self):
        if hasattr(self, 'preview_window') and self.preview_window and self.preview_window.winfo_exists():
            self.preview_window.refresh_preview()

    def validate_bits(self):
        try:
            val = int(self.output_bits_var.get())
            if val < 1:
                self.output_bits_var.set(1)
            elif val > 16:
                self.output_bits_var.set(16)
        except:
            self.output_bits_var.set(8)

    def update_bits_default(self):
        fmt = self.format_var.get()
        if fmt in (1,3):
            self.output_bits_var.set(1)
        elif fmt in (2,4):
            self.output_bits_var.set(2)
        else:
            self.output_bits_var.set(8)

    # 控件可见性控制
    def toggle_image_format_visibility(self):
        if self.format_var.get() == 5:
            self.image_format_frame.pack(anchor=tk.W, padx=20, pady=2)
        else:
            self.image_format_frame.pack_forget()

    def toggle_ebook_format_visibility(self):
        if self.format_var.get() == 6:
            self.ebook_format_frame.pack(anchor=tk.W, padx=20, pady=2)
        else:
            self.ebook_format_frame.pack_forget()

    def toggle_stretch_visibility(self):
        if self.resolution_var.get() == 5:
            self.stretch_check.config(state=tk.DISABLED)
            self.stretch_var.set(False)
        else:
            self.stretch_check.config(state=tk.NORMAL)

    def toggle_custom_res(self):
        if self.resolution_var.get() == 6:
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
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1 (上下等分，滚动模式)", variable=self.crop_sub_var, value=3, command=self.toggle_overlap).pack(anchor=tk.W)
            self.crop_sub_frame.pack(anchor=tk.W, padx=20, pady=2)
            self.toggle_overlap()
        elif mode == 3:
            if self.crop_sub_var.get() not in (1,2,3,4):
                self.crop_sub_var.set(1)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 2 : 1", variable=self.crop_sub_var, value=1).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="2 : 1 : 1", variable=self.crop_sub_var, value=2).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1 : 2", variable=self.crop_sub_var, value=3).pack(anchor=tk.W)
            ttk.Radiobutton(self.crop_sub_frame, text="1 : 1 : 1 (5图滚动模式)", variable=self.crop_sub_var, value=4, command=self.toggle_overlap).pack(anchor=tk.W)
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

    def update_filename_example(self):
        for widget in self.filename_frame.winfo_children():
            widget.destroy()
        fmt = self.format_var.get()
        if fmt == 3:
            ext = "xtg"
        elif fmt == 4:
            ext = "xth"
        elif fmt == 5:
            ext = self.image_format_var.get()
        else:
            return
        ttk.Radiobutton(self.filename_frame, text=f"编号 (例如 1.{ext})", variable=self.filename_format_var, value=1).pack(anchor=tk.W)
        ttk.Radiobutton(self.filename_frame, text=f"电子书名-编号 (例如 电子书名-1.{ext})", variable=self.filename_format_var, value=2).pack(anchor=tk.W)

    def toggle_filename_visibility(self):
        fmt = self.format_var.get()
        if fmt in (3,4,5):
            self.filename_frame.pack(fill=tk.X, pady=5)
            self.update_filename_example()
        else:
            self.filename_frame.pack_forget()

    def toggle_split_visibility(self):
        if self.format_var.get() in (1,2):
            self.split_frame.pack(fill=tk.X, pady=5)
        else:
            self.split_frame.pack_forget()

    def toggle_split_custom(self):
        if self.split_size_var.get() == 2:
            self.split_custom_frame.pack(anchor=tk.W, padx=20)
        else:
            self.split_custom_frame.pack_forget()

    def browse_input(self):
        dirname = filedialog.askdirectory(title="选择包含输入文件的目录", initialdir=self.input_dir.get())
        if dirname:
            self.input_dir.set(dirname)
            if hasattr(self, 'preview_window') and self.preview_window and self.preview_window.winfo_exists():
                self.preview_window.load_current_book()

    def browse_output(self):
        dirname = filedialog.askdirectory(title="选择输出目录", initialdir=self.output_dir.get())
        if dirname:
            self.output_dir.set(dirname)

    def get_resolution(self):
        res = self.resolution_var.get()
        if res == 1:
            return "preset", (480, 800)
        elif res == 2:
            return "preset", (960, 1600)
        elif res == 3:
            return "preset", (528, 792)
        elif res == 4:
            return "preset", (1056, 1584)
        elif res == 5:
            return "original", None
        else:
            try:
                w = int(self.custom_width.get())
                h = int(self.custom_height.get())
                if w > 0 and h > 0:
                    return "custom", (w, h)
                else:
                    raise ValueError
            except:
                messagebox.showerror("错误", "自定义分辨率格式错误，使用默认 X4 (480x800)")
                return "preset", (480, 800)

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
                messagebox.showerror("错误", "请输入自定义分包大小")
                return None
            try:
                return parse_size_string(size_str)
            except ValueError as e:
                messagebox.showerror("错误", f"分包大小格式错误: {e}")
                return None
        else:
            return 0

    def build_settings(self):
        res_type, res_value = self.get_resolution()
        rotate_map = {1: "clockwise", 2: "counterclockwise", 3: "none"}
        rotate_mode = rotate_map[self.rotate_var.get()]
        fmt = self.format_var.get()
        if fmt == 5:
            out_type = "image"
            out_value = self.image_format_var.get()
        elif fmt == 6:
            out_type = "ebook"
            out_value = self.ebook_format_var.get()
        else:
            out_type = "format"
            out_value = {1: "xtc", 2: "xtch", 3: "xtg", 4: "xth"}[fmt]
        split_size = None
        if out_type == "format" and out_value in ('xtc', 'xtch'):
            split_size = self.get_split_size()
            if split_size is None:
                return None
        filename_format = None
        if out_type == "image" or (out_type == "format" and out_value in ('xtg', 'xth')):
            filename_format = self.filename_format_var.get() - 1
        settings = {
            'out_type': out_type,
            'out_value': out_value,
            'res_type': res_type,
            'res_value': res_value,
            'auto_crop': self.auto_crop_var.get(),
            'rotate_mode': rotate_mode,
            'crop': self.get_crop_settings(),
            'stretch': self.stretch_var.get() if res_type != "original" else False,
            'sharpen': self.sharpen_var.get(),
            'contrast': self.contrast_var.get(),
            'clahe': self.clahe_var.get(),
            'dither_algo': self.dither_algo_var.get(),
            'output_bits': self.output_bits_var.get(),
            'dither_strength': self.dither_strength.get(),
            'max_workers': self.max_workers.get(),
            'filename_format': filename_format,
            'split_size': split_size,
            'gif_mode': self.gif_mode_var.get()
        }
        return settings

    def toggle_log_window(self):
        if self.log_window is None or not self.log_window.winfo_exists():
            self.log_window = LogWindow(self.root, self.log_queue)
            self.log_window.show()
        else:
            if self.log_window.state() == 'withdrawn':
                self.log_window.show()
            else:
                self.log_window.hide()

    def toggle_preview_window(self):
        if not PREVIEW_AVAILABLE:
            messagebox.showinfo("预览功能不可用", "未找到预览模块 ebook2xtx_gui_viewer.py")
            return
        if self.preview_window is None or not self.preview_window.winfo_exists():
            self.preview_window = PreviewWindow(self.root, self)
            self.preview_window.show()
        else:
            if self.preview_window.state() == 'withdrawn':
                self.preview_window.show()
            else:
                self.preview_window.hide()

    def start_conversion(self):
        input_dir = self.input_dir.get().strip()
        output_dir = self.output_dir.get().strip()
        if not input_dir or not output_dir:
            messagebox.showerror("错误", "请选择输入和输出目录")
            return
        if not check_and_install_dependencies():
            messagebox.showerror("错误", "依赖安装失败，请手动安装后再试")
            return

        settings = self.build_settings()
        if settings is None:
            return

        setup_gui_logging(self.log_queue, enable_file_log=self.enable_file_log.get())

        self.stop_conversion = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.total_progress['value'] = 0
        self.total_label.config(text="总进度: 0/0 (0%)")
        self.file_progress['value'] = 0
        self.file_label.config(text="当前文件: 0/0 (0%)")

        self.text_choice_memory = None
        self.mixed_choice_memory = None

        def run():
            try:
                items = scan_input_items(Path(input_dir))
                if not items:
                    self.root.after(0, lambda: messagebox.showwarning("警告", "未找到任何可转换的内容"))
                    return

                processed_items = []
                for item in items:
                    if self.stop_conversion:
                        logging.info("用户停止转换")
                        break

                    if item.doc_type == 'comic':
                        logging.info(f"本书为纯图片电子书: {item.name}")
                        processed_items.append(item)
                    elif item.doc_type == 'text':
                        logging.info(f"本书为纯文本电子书: {item.name}")
                        if self.text_choice_memory is None:
                            event = threading.Event()
                            result = [False]
                            def ask():
                                resp = messagebox.askyesno("纯文本电子书", "本书为纯文本电子书，建议直接打开阅读，不建议转换！\n是否输出为TXT文件？")
                                result[0] = resp
                                event.set()
                            self.root.after(0, ask)
                            event.wait()
                            self.text_choice_memory = result[0]
                        if self.text_choice_memory:
                            logging.info(f"用户选择转换为 TXT: {item.name}")
                            txt = item.get_text()
                            if txt:
                                txt_path = Path(output_dir) / f"{sanitize_filename(item.name)}.txt"
                                txt_path.write_text(txt, encoding='utf-8')
                                logging.info(f"已保存 TXT 文件: {txt_path}")
                            else:
                                logging.error(f"提取文本失败: {item.name}")
                        else:
                            logging.info(f"用户选择跳过: {item.name}")
                    else:
                        logging.info(f"本书为图文混排电子书: {item.name}")
                        if self.mixed_choice_memory is None:
                            event = threading.Event()
                            choice = [0]
                            def ask_mixed():
                                dialog = tk.Toplevel(self.root)
                                dialog.title("图文混排电子书")
                                dialog.geometry("500x200")
                                dialog.transient(self.root)
                                dialog.grab_set()
                                label = ttk.Label(dialog, text="本书为图文混排电子书，本工具暂不支持直接转换。\n请选择操作：")
                                label.pack(pady=10)
                                var = tk.IntVar(value=0)
                                ttk.Radiobutton(dialog, text="不转换", variable=var, value=0).pack(anchor=tk.W, padx=20)
                                ttk.Radiobutton(dialog, text="仅转换图片（提取内嵌图片）", variable=var, value=1).pack(anchor=tk.W, padx=20)
                                ttk.Radiobutton(dialog, text="仅转换文本（输出TXT文件）", variable=var, value=2).pack(anchor=tk.W, padx=20)
                                def confirm():
                                    choice[0] = var.get()
                                    dialog.destroy()
                                    event.set()
                                ttk.Button(dialog, text="确定", command=confirm).pack(pady=10)
                                dialog.protocol("WM_DELETE_WINDOW", confirm)
                            self.root.after(0, ask_mixed)
                            event.wait()
                            self.mixed_choice_memory = choice[0]
                        if self.mixed_choice_memory == 1:
                            logging.info(f"用户选择仅转换图片: {item.name}")
                            processed_items.append(item)
                        elif self.mixed_choice_memory == 2:
                            logging.info(f"用户选择仅转换文本: {item.name}")
                            txt = item.get_text()
                            if txt:
                                txt_path = Path(output_dir) / f"{sanitize_filename(item.name)}.txt"
                                txt_path.write_text(txt, encoding='utf-8')
                                logging.info(f"已保存 TXT 文件: {txt_path}")
                            else:
                                logging.error(f"提取文本失败: {item.name}")
                        else:
                            logging.info(f"用户选择跳过: {item.name}")

                if processed_items and not self.stop_conversion:
                    total = len(processed_items)
                    success_count = 0
                    for idx, item in enumerate(processed_items):
                        if self.stop_conversion:
                            break
                        self.root.after(0, lambda name=item.name, cur=idx+1, tot=total: self._update_progress_ui(name, cur, tot))
                        local_settings = settings.copy()
                        if settings['res_type'] == 'original':
                            local_settings['width'] = 0
                            local_settings['height'] = 0
                            local_settings['stretch'] = False
                        else:
                            w, h = settings['res_value']
                            local_settings['width'] = w
                            local_settings['height'] = h
                        images = item.get_images()
                        if not images:
                            logging.error(f"没有找到任何图像: {item.name}")
                            continue
                        if settings['out_type'] == 'ebook':
                            if process_images_to_ebook(images, item.name, local_settings, Path(output_dir)):
                                success_count += 1
                            else:
                                logging.error(f"处理失败: {item.name}")
                        else:
                            if process_images(images, item.name, local_settings, Path(output_dir)):
                                success_count += 1
                            else:
                                logging.error(f"处理失败: {item.name}")
                    self.root.after(0, self.conversion_finished, success_count, total)
                else:
                    self.root.after(0, lambda: messagebox.showinfo("完成", "没有需要转换的项目"))
                    self.root.after(0, self.conversion_finished, 0, 0)
            except Exception as e:
                logging.exception("转换过程异常")
                self.root.after(0, self.conversion_error, str(e))
            finally:
                self.root.after(0, self._reset_ui)

        self.convert_thread = threading.Thread(target=run)
        self.convert_thread.start()

    def _reset_ui(self):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

    def conversion_error(self, error_msg):
        self._reset_ui()
        self.total_label.config(text="转换出错")
        messagebox.showerror("错误", f"转换过程中发生错误:\n{error_msg}")

    def conversion_finished(self, success_count, total):
        self._reset_ui()
        if total > 0:
            self.total_label.config(text=f"转换完成，成功处理 {success_count} 个文件")
            self.total_progress['value'] = 100
            messagebox.showinfo("完成", f"转换完成！成功处理 {success_count}/{total} 个文件。")
        else:
            self.total_label.config(text="转换完成，没有需要转换的项目")
            self.total_progress['value'] = 100
        self.file_label.config(text="完成")
        self.file_progress['value'] = 100

    def stop_conversion_cmd(self):
        self.stop_conversion = True
        self.stop_btn.config(state=tk.DISABLED)
        self.total_label.config(text="正在停止（可能需要等待当前任务完成）...")
        messagebox.showinfo("提示", "停止操作将等待当前图片处理完成，请稍后。")

    def _update_progress_ui(self, archive_name, current, total):
        if total > 0:
            percent = current / total * 100
            self.total_progress['value'] = percent
            self.total_label.config(text=f"总进度: {current}/{total} ({percent:.1f}%)")
        else:
            self.total_progress['value'] = 0
            self.total_label.config(text="总进度: 准备中...")
        self.file_label.config(text=f"当前文件: {archive_name} ({current}/{total})")

    def update_logs(self):
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.update_logs()
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
        ttk.Label(frame, text="电子书/文档转 Xteink 设备格式工具").pack(pady=2)
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