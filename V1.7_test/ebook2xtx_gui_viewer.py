#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ebook2XTX 预览窗口子模块
仅供 Ebook2XTX_GUI 调用，不可单独启动。
提供完整的漫画预览功能，实时应用主程序转换设置。
参考 XTC_Viewer 实现，支持缩放、翻页、双页显示、全屏、背景色等。
"""

import os
import sys
import logging
import struct
import webbrowser
import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
from PIL import Image, ImageTk
import numpy as np

# 导入核心处理模块
try:
    from ebook2xtx import XTCReader, sanitize_filename, scan_input_items, InputItem
    from core import (
        fill_transparent_with_white, crop_white_black_borders, rotate_image,
        split_image_vertically, split_rolling_2, split_rolling_3,
        resize_to_target, apply_sharpen, apply_contrast, apply_clahe,
        floyd_steinberg_dither_numba, atkinson_dither_numba, no_dither_quantize,
        encode_xtg, encode_xth
    )
except ImportError as e:
    print(f"导入核心模块失败: {e}")
    sys.exit(1)

# ========== 日志配置（预览窗口专用，默认启用文件日志） ==========
def setup_viewer_logger():
    logger = logging.getLogger("Ebook2XTX_Viewer")
    logger.setLevel(logging.DEBUG)
    # 清除已有处理器
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    # 控制台处理器
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console.setFormatter(fmt)
    logger.addHandler(console)
    # 文件处理器（默认开启）
    log_dir = Path.cwd() / "log"
    log_dir.mkdir(exist_ok=True)
    log_filename = log_dir / f"Ebook2XTX_Viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger

logger = setup_viewer_logger()

# ========== 辅助函数：从输入目录获取图片列表 ==========
def get_image_paths_from_input_dir(input_dir: str) -> List[str]:
    """扫描输入目录下所有支持的图片文件，返回绝对路径列表（不解压压缩包）"""
    if not input_dir or not Path(input_dir).exists():
        return []
    image_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif')
    paths = []
    for ext in image_exts:
        paths.extend(Path(input_dir).rglob(f'*{ext}'))
    # 自然排序
    from natsort import natsorted
    return natsorted([str(p) for p in paths])

# ========== 预览窗口类 ==========
class PreviewWindow(tk.Toplevel):
    def __init__(self, parent, main_app):
        super().__init__(parent)
        self.main_app = main_app
        self.title("Ebook2XTX - 预览窗口")
        self.geometry("1100x800")
        self.minsize(800, 600)

        # 图像列表相关
        self.image_paths: List[str] = []
        self.current_index: int = 0
        self.total_pages: int = 0

        # 当前解码后的原始图像（未切割、未处理）
        self.original_raw_image: Optional[Image.Image] = None
        # 当前显示的图像（经过全部处理）
        self.display_image: Optional[Image.Image] = None
        self.photo_image: Optional[ImageTk.PhotoImage] = None
        self.canvas_image_id = None

        # 缩放相关
        self.zoom_factor: float = 1.0
        self.scale_mode = tk.StringVar(value="预览")  # "原图", "预览(原分辨率)", "预览"
        self.scale_factors = {
            "原图": None,
            "预览(原分辨率)": None,   # 应用所有处理但不缩放分辨率
            "预览": None              # 应用所有处理并按主程序目标分辨率缩放
        }

        # 双页显示
        self.double_page = False
        self.double_page_var = tk.BooleanVar(value=False)

        # 背景颜色
        self.background_color = "gray"

        # 全屏相关
        self.fullscreen = False
        self.menubar = None
        self.status_frame = None
        self.original_geometry = None

        # 预加载缓存（用于平滑翻页）
        self.cache: Dict[int, Image.Image] = {}
        self.cache_lock = threading.Lock()
        self.preload_thread = None
        # 预加载范围：当前页前后各2页（双页模式则前后各4页）
        self.preload_range = 2

        # 切割类型（用于预加载数量计算）
        self.crop_mode = 0  # 0=不切割, 2=横切2图, 3=横切3图, 4=滚动2图, 5=滚动3图
        self.crop_sub = 0
        self.overlap = 0

        # 创建UI
        self._create_widgets()
        self._bind_events()

        # 加载当前主程序的输入目录
        self.load_current_book()

        # 设置窗口关闭协议（隐藏而非销毁）
        self.protocol("WM_DELETE_WINDOW", self.hide)

        # 定时刷新预览（当主程序参数改变时）
        self._refresh_after_id = None

    # ---------- UI 创建 ----------
    def _create_widgets(self):
        # 菜单栏
        self.menubar = tk.Menu(self)

        # 文件菜单
        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="保存当前页为图片", command=self.save_current_page)
        file_menu.add_separator()
        file_menu.add_command(label="退出预览", command=self.hide)
        self.menubar.add_cascade(label="文件", menu=file_menu)

        # 跳转菜单
        jump_menu = tk.Menu(self.menubar, tearoff=0)
        jump_menu.add_command(label="上一页", command=self.prev_page, accelerator="Left")
        jump_menu.add_command(label="下一页", command=self.next_page, accelerator="Right")
        jump_menu.add_separator()
        jump_menu.add_command(label="跳转到页码...", command=self.show_jump_dialog, accelerator="Ctrl+G")
        self.menubar.add_cascade(label="跳转", menu=jump_menu)

        # 设置菜单
        settings_menu = tk.Menu(self.menubar, tearoff=0)

        # 缩放模式子菜单
        zoom_menu = tk.Menu(settings_menu, tearoff=0)
        for mode in self.scale_factors.keys():
            zoom_menu.add_radiobutton(label=mode, variable=self.scale_mode, value=mode,
                                      command=self.on_scale_mode_changed)
        settings_menu.add_cascade(label="缩放模式", menu=zoom_menu)

        # 背景颜色子菜单
        bg_menu = tk.Menu(settings_menu, tearoff=0)
        bg_menu.add_command(label="灰色", command=lambda: self.set_background_color("gray"))
        bg_menu.add_command(label="白色", command=lambda: self.set_background_color("white"))
        bg_menu.add_command(label="黑色", command=lambda: self.set_background_color("black"))
        bg_menu.add_command(label="自定义...", command=self.custom_background_color)
        settings_menu.add_cascade(label="背景颜色", menu=bg_menu)

        # 双页显示
        settings_menu.add_separator()
        settings_menu.add_checkbutton(label="双页显示", variable=self.double_page_var,
                                      command=self.toggle_double_page)

        # 全屏
        settings_menu.add_separator()
        settings_menu.add_command(label="全屏 (F11)", command=self.toggle_fullscreen)

        self.menubar.add_cascade(label="设置", menu=settings_menu)

        # 帮助菜单
        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self.show_about)
        self.menubar.add_cascade(label="帮助", menu=help_menu)

        self.config(menu=self.menubar)

        # 主框架
        main_frame = ttk.Frame(self, padding=5)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 画布区域（带滚动条）
        canvas_frame = ttk.Frame(main_frame)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        h_scroll = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        v_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(canvas_frame, bg=self.background_color, highlightthickness=0,
                                xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        h_scroll.config(command=self.canvas.xview)
        v_scroll.config(command=self.canvas.yview)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        # 状态栏
        self.status_frame = ttk.Frame(self)
        self.status_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.status = ttk.Label(self.status_frame, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.page_status_label = ttk.Label(self.status_frame, text="", relief=tk.SUNKEN, anchor=tk.E)
        self.page_status_label.pack(side=tk.RIGHT, padx=5)

        # 绑定事件
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)
        self.canvas.bind("<Control-MouseWheel>", self.on_ctrl_mousewheel)
        self.canvas.bind("<Control-Button-4>", self.on_ctrl_mousewheel)
        self.canvas.bind("<Control-Button-5>", self.on_ctrl_mousewheel)

        self.bind('<Configure>', self.on_window_resize)
        self.bind('<Left>', lambda e: self.prev_page())
        self.bind('<Right>', lambda e: self.next_page())
        self.bind('<F11>', lambda e: self.toggle_fullscreen())
        self.bind('<Escape>', lambda e: self.exit_fullscreen())
        self.bind('<Control-g>', lambda e: self.show_jump_dialog())
        self.bind('<Control-G>', lambda e: self.show_jump_dialog())

    def _bind_events(self):
        pass  # 已在上面绑定

    # ---------- 核心功能 ----------
    def load_current_book(self):
        """从主程序获取输入目录并加载图片列表"""
        input_dir = self.main_app.input_dir.get().strip()
        if not input_dir or not Path(input_dir).exists():
            self.image_paths = []
            self.total_pages = 0
            self.current_index = 0
            self.status.config(text="输入目录无效")
            self.page_status_label.config(text="0/0")
            return

        self.image_paths = get_image_paths_from_input_dir(input_dir)
        self.total_pages = len(self.image_paths)
        self.current_index = 0 if self.total_pages > 0 else -1
        self.status.config(text=f"已加载 {self.total_pages} 张图片")
        self._update_page_display()
        if self.total_pages > 0:
            self.render_current_page()
            self._start_preload()

    def _get_current_settings(self) -> Dict[str, Any]:
        """从主程序获取当前转换设置"""
        main = self.main_app
        settings = {
            'auto_crop': main.auto_crop_var.get(),
            'rotate_mode': {1: "clockwise", 2: "counterclockwise", 3: "none"}[main.rotate_var.get()],
            'crop': main.get_crop_settings(),
            'stretch': main.stretch_var.get(),
            'sharpen': main.sharpen_var.get(),
            'contrast': main.contrast_var.get(),
            'clahe': main.clahe_var.get(),
            'dither_algo': main.dither_algo_var.get(),
            'output_bits': main.output_bits_var.get(),
            'dither_strength': main.dither_strength.get() / 100.0,
            'target_width': 0,
            'target_height': 0,
            'scale_mode': self.scale_mode.get(),
        }
        # 获取目标分辨率（如果缩放模式为“预览”且主程序不是原分辨率）
        if self.scale_mode.get() == "预览":
            res_type, res_value = main.get_resolution()
            if res_type != 'original' and res_value:
                settings['target_width'], settings['target_height'] = res_value
        return settings

    def render_current_page(self):
        """渲染当前页（应用所有处理）"""
        if self.current_index < 0 or self.current_index >= self.total_pages:
            return
        img_path = self.image_paths[self.current_index]
        try:
            with Image.open(img_path) as f:
                f.load()
                raw_img = f.copy()
        except Exception as e:
            logger.error(f"加载图片失败: {e}")
            self.status.config(text=f"加载失败: {e}")
            return

        # 应用全部处理
        processed_img = self._apply_full_processing(raw_img, self._get_current_settings())
        self.original_raw_image = processed_img  # 保存处理后的图像
        self._apply_zoom()

    def _apply_full_processing(self, img: Image.Image, settings: Dict) -> Image.Image:
        """完整处理流程（与转换管道一致）"""
        # 透明填充
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            img = fill_transparent_with_white(img)
        img = img.convert('L')

        # 黑白边裁切
        if settings.get('auto_crop', False):
            img = crop_white_black_borders(img)

        # 整体旋转（仅横版）
        rotate_mode = settings.get('rotate_mode', 'none')
        if img.width > img.height and rotate_mode != "none":
            img = rotate_image(img, rotate_mode)

        # 切割（仅竖版，不区分首尾页，预览时直接切割）
        crop_cfg = settings.get('crop', {'mode': 0})
        self.crop_mode = crop_cfg.get('mode', 0)
        if self.crop_mode != 0:
            if self.crop_mode == 2:  # 横切2图直接
                ratio = crop_cfg['ratio']
                splits = split_image_vertically(img, 2, ratio)
                img = splits[0]  # 预览只显示第一块（可以后续增加选择）
            elif self.crop_mode == 3:
                ratio = crop_cfg['ratio']
                splits = split_image_vertically(img, 3, ratio)
                img = splits[0]
            elif self.crop_mode == 4:
                overlap = crop_cfg['overlap_percent']
                splits = split_rolling_2(img, overlap)
                img = splits[0]
            elif self.crop_mode == 5:
                overlap = crop_cfg['overlap_percent']
                splits = split_rolling_3(img, overlap)
                img = splits[0]
            # 切割后再次旋转（如果用户需要）
            if rotate_mode != "none" and img.width > img.height:
                img = rotate_image(img, rotate_mode)

        # 缩放（根据缩放模式决定）
        target_w = settings.get('target_width', 0)
        target_h = settings.get('target_height', 0)
        stretch = settings.get('stretch', False)
        if target_w > 0 and target_h > 0:
            img = resize_to_target(img, target_w, target_h, stretch)

        # 图像增强
        sharpen = settings.get('sharpen', 0)
        if sharpen > 0:
            img = apply_sharpen(img, sharpen)
        contrast = settings.get('contrast', 0)
        if contrast > 0:
            img = apply_contrast(img, contrast)
        clahe = settings.get('clahe', 0)
        if clahe > 0:
            img = apply_clahe(img, clahe)

        # 抖动与量化
        bits = settings.get('output_bits', 1)
        dither_algo = settings.get('dither_algo', 'Floyd-Steinberg')
        dither_strength = settings.get('dither_strength', 0.7)
        gray_arr = np.array(img, dtype=np.float32)
        if dither_algo == 'Floyd-Steinberg':
            dithered = floyd_steinberg_dither_numba(gray_arr, bits, dither_strength)
        elif dither_algo == 'Atkinson':
            dithered = atkinson_dither_numba(gray_arr, bits, dither_strength)
        else:
            dithered = no_dither_quantize(gray_arr, bits)
        img = Image.fromarray(dithered, mode='L')
        return img

    def _apply_zoom(self):
        """根据当前缩放因子和缩放模式，生成最终显示图像"""
        if self.original_raw_image is None:
            return
        img = self.original_raw_image
        scale_mode = self.scale_mode.get()
        if scale_mode == "原图":
            # 不应用任何处理，直接显示原始图片（需重新加载原始未处理的图片）
            # 重新加载原始图片
            if self.current_index >= 0:
                img_path = self.image_paths[self.current_index]
                with Image.open(img_path) as f:
                    f.load()
                    img = f.copy()
                if img.mode != 'L':
                    img = img.convert('L')
        elif scale_mode == "预览(原分辨率)":
            # 已经应用了所有处理，但不缩放，直接显示
            pass
        elif scale_mode == "预览":
            # 已经应用了所有处理，且可能已经缩放到目标尺寸
            pass

        # 应用用户缩放因子
        w = int(img.width * self.zoom_factor)
        h = int(img.height * self.zoom_factor)
        self.display_image = img.resize((w, h), Image.Resampling.LANCZOS)
        self.photo_image = ImageTk.PhotoImage(self.display_image)
        self._update_canvas()

    def _update_canvas(self):
        self.canvas.delete("all")
        if self.display_image is None:
            return
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        img_w = self.display_image.width
        img_h = self.display_image.height
        x = max(0, (canvas_w - img_w) // 2)
        y = max(0, (canvas_h - img_h) // 2)
        self.canvas_image_id = self.canvas.create_image(x, y, anchor=tk.NW, image=self.photo_image)
        self.canvas.config(scrollregion=(0, 0, img_w, img_h))
        # 更新状态栏
        mode_name = self.scale_mode.get()
        self.status.config(text=f"第 {self.current_index+1}/{self.total_pages} 页 | 显示尺寸 {img_w}x{img_h} | 模式 {mode_name} | 缩放 {int(self.zoom_factor*100)}%")

    def _update_page_display(self):
        if self.double_page:
            if self.current_index + 1 < self.total_pages:
                self.page_status_label.config(text=f"第 {self.current_index+1}-{self.current_index+2} / {self.total_pages} 页")
            else:
                self.page_status_label.config(text=f"第 {self.current_index+1} / {self.total_pages} 页")
        else:
            self.page_status_label.config(text=f"第 {self.current_index+1} / {self.total_pages} 页")

    # ---------- 预加载 ----------
    def _start_preload(self):
        """启动预加载线程"""
        if self.preload_thread and self.preload_thread.is_alive():
            return
        self.preload_thread = threading.Thread(target=self._preload_worker, daemon=True)
        self.preload_thread.start()

    def _preload_worker(self):
        """预加载当前页前后各若干页"""
        if self.total_pages == 0:
            return
        # 根据切割模式动态调整预加载数量
        extra = 0
        if self.crop_mode == 2 or self.crop_mode == 4:
            extra = 2  # 横切2图，需要加载额外页面（前后各2页？实际根据需求：横切2图加载6张图，即前后各多2页）
        elif self.crop_mode == 3 or self.crop_mode == 5:
            extra = 3
        preload_count = self.preload_range + extra
        indices = []
        for offset in range(-preload_count, preload_count + 1):
            idx = self.current_index + offset
            if 0 <= idx < self.total_pages and idx != self.current_index:
                indices.append(idx)
        for idx in indices:
            if idx in self.cache:
                continue
            try:
                img_path = self.image_paths[idx]
                with Image.open(img_path) as f:
                    f.load()
                    raw = f.copy()
                processed = self._apply_full_processing(raw, self._get_current_settings())
                with self.cache_lock:
                    self.cache[idx] = processed
            except Exception as e:
                logger.debug(f"预加载失败 {idx}: {e}")

    def _get_cached_image(self, idx: int) -> Optional[Image.Image]:
        with self.cache_lock:
            return self.cache.get(idx)

    def _clear_cache(self):
        with self.cache_lock:
            self.cache.clear()

    # ---------- 翻页 ----------
    def prev_page(self):
        if self.total_pages == 0:
            return
        if self.double_page:
            new_index = self.current_index - 2
            if new_index < 0:
                new_index = 0
            if new_index != self.current_index:
                self.current_index = new_index
                self._update_page_display()
                self.render_current_page()
                self._start_preload()
        else:
            self.current_index = (self.current_index - 1) % self.total_pages
            self._update_page_display()
            self.render_current_page()
            self._start_preload()

    def next_page(self):
        if self.total_pages == 0:
            return
        if self.double_page:
            new_index = self.current_index + 2
            if new_index >= self.total_pages:
                if self.current_index < self.total_pages - 1:
                    new_index = self.total_pages - 1
                else:
                    return
            self.current_index = new_index
            self._update_page_display()
            self.render_current_page()
            self._start_preload()
        else:
            self.current_index = (self.current_index + 1) % self.total_pages
            self._update_page_display()
            self.render_current_page()
            self._start_preload()

    def jump_to_page(self, page_index: int):
        if 0 <= page_index < self.total_pages:
            self.current_index = page_index
            self._update_page_display()
            self.render_current_page()
            self._start_preload()

    def show_jump_dialog(self):
        if self.total_pages == 0:
            return
        result = simpledialog.askinteger("跳转到页码", f"请输入页码 (1-{self.total_pages}):",
                                         parent=self, initialvalue=self.current_index+1,
                                         minvalue=1, maxvalue=self.total_pages)
        if result:
            self.jump_to_page(result-1)

    # ---------- 缩放模式 ----------
    def on_scale_mode_changed(self):
        self.render_current_page()  # 重新处理
        self._clear_cache()
        self._start_preload()

    # ---------- 背景颜色 ----------
    def set_background_color(self, color):
        self.background_color = color
        self.canvas.config(bg=color)

    def custom_background_color(self):
        # 简单实现：使用颜色选择对话框（tkinter 没有原生，使用 simpledialog 输入十六进制）
        color = simpledialog.askstring("自定义背景颜色", "请输入颜色代码 (如 #RRGGBB 或 gray, white, black):",
                                       parent=self, initialvalue=self.background_color)
        if color:
            self.set_background_color(color)

    # ---------- 双页显示 ----------
    def toggle_double_page(self):
        self.double_page = self.double_page_var.get()
        self.render_current_page()
        self._update_page_display()

    # ---------- 全屏 ----------
    def toggle_fullscreen(self):
        if self.fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        self.original_geometry = self.geometry()
        self.attributes('-fullscreen', True)
        self.config(menu='')
        self.status_frame.pack_forget()
        self.fullscreen = True

    def exit_fullscreen(self):
        if self.fullscreen:
            self.attributes('-fullscreen', False)
            self.config(menu=self.menubar)
            self.status_frame.pack(side=tk.BOTTOM, fill=tk.X)
            if self.original_geometry:
                self.geometry(self.original_geometry)
            self.fullscreen = False
            self._update_canvas()

    # ---------- 保存当前页为图片 ----------
    def save_current_page(self):
        if self.display_image is None:
            return
        save_path = filedialog.asksaveasfilename(defaultextension=".png",
                                                 filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg *.jpeg")])
        if save_path:
            ext = os.path.splitext(save_path)[1].lower()
            img = self.display_image
            if ext in ('.jpg', '.jpeg'):
                if img.mode == 'L':
                    img = img.convert('RGB')
                img.save(save_path, 'JPEG', quality=95)
            else:
                img.save(save_path, 'PNG')
            logger.info(f"保存页面至: {save_path}")
            self.status.config(text=f"已保存: {save_path}")

    # ---------- 关于 ----------
    def show_about(self):
        about_win = tk.Toplevel(self)
        about_win.title("关于")
        about_win.geometry("400x200")
        about_win.resizable(False, False)
        about_win.transient(self)
        about_win.grab_set()
        frame = ttk.Frame(about_win, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Ebook2XTX 预览窗口", font=("微软雅黑", 14, "bold")).pack(pady=(0,10))
        ttk.Label(frame, text="版本 1.0").pack()
        ttk.Label(frame, text="实时应用主程序转换设置").pack()
        link_frame = ttk.Frame(frame)
        link_frame.pack(pady=10)
        link_label = tk.Label(link_frame, text="GitHub 仓库", fg="blue", cursor="hand2")
        link_label.pack()
        link_label.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/gmy771810930/Ebook2XTX"))
        ttk.Button(frame, text="关闭", command=about_win.destroy).pack(pady=10)

    # ---------- 窗口事件 ----------
    def on_window_resize(self, event):
        if self.display_image:
            self._update_canvas()

    def on_mousewheel(self, event):
        # 普通滚轮：滚动画布或翻页
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if self.display_image:
            img_w = self.display_image.width
            img_h = self.display_image.height
            if img_w <= canvas_w and img_h <= canvas_h:
                # 图像小于画布，翻页
                if event.delta > 0 or event.num == 4:
                    self.prev_page()
                else:
                    self.next_page()
            else:
                # 滚动
                if event.delta > 0 or event.num == 4:
                    self.canvas.yview_scroll(-1, "units")
                else:
                    self.canvas.yview_scroll(1, "units")
        else:
            # 无图像时翻页
            if event.delta > 0 or event.num == 4:
                self.prev_page()
            else:
                self.next_page()

    def on_ctrl_mousewheel(self, event):
        # Ctrl+滚轮缩放
        if event.delta > 0 or event.num == 4:
            self.zoom_factor *= 1.1
        else:
            self.zoom_factor *= 0.9
        self.zoom_factor = max(0.1, min(5.0, self.zoom_factor))
        self._apply_zoom()

    # ---------- 刷新预览（主程序参数改变时调用） ----------
    def refresh_preview(self):
        """当主程序设置改变时，重新渲染当前页"""
        if self._refresh_after_id:
            self.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.after(200, self._do_refresh)

    def _do_refresh(self):
        self._clear_cache()
        self.render_current_page()
        self._start_preload()
        self._refresh_after_id = None

    # ---------- 显示/隐藏 ----------
    def show(self):
        self.deiconify()
        self.load_current_book()
        self.refresh_preview()

    def hide(self):
        self.withdraw()

    def winfo_exists(self):
        return self.winfo_exists() and self.state() != 'withdrawn'


# ---------- 单独启动时的检测 ----------
if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("错误", "预览模块不可单独启动，请从 Ebook2XTX_GUI 主程序调用。")
    sys.exit(1)