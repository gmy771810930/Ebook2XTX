#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ebook2XTX 预览窗口子模块
仅供 Ebook2XTX_GUI 调用，不可单独启动。
提供独立的漫画预览功能，实时应用主程序转换设置。
支持打开压缩包、文件夹、EPUB/PDF等格式。
"""

import os
import sys
import logging
import tempfile
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
from PIL import Image, ImageTk
import numpy as np

# 导入核心模块
try:
    from ebook2xtx import sanitize_filename, extract_archive
    from core import (
        fill_transparent_with_white, crop_white_black_borders, rotate_image,
        split_image_vertically, split_rolling_2, split_rolling_3,
        resize_to_target, apply_sharpen, apply_contrast, apply_clahe,
        floyd_steinberg_dither_numba, atkinson_dither_numba, no_dither_quantize
    )
except ImportError as e:
    print(f"导入核心模块失败: {e}")
    sys.exit(1)

# ========== 独立实现 spine 顺序提取 EPUB 图片（与主程序一致） ==========
def extract_images_from_epub_spine(epub_path: Path) -> List[Image.Image]:
    """按 spine 顺序提取 EPUB 中的图片（用户提供的正确实现）"""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    import io
    import logging
    logger = logging.getLogger(__name__)

    book = epub.read_epub(epub_path)

    image_map = {}
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            orig_name = item.get_name()
            content = item.get_content()
            image_map[orig_name] = content
            base = os.path.basename(orig_name)
            image_map[base] = content

    spine_ids = []
    for spine_item in book.spine:
        if isinstance(spine_item, tuple):
            ref = spine_item[0]
        else:
            ref = spine_item
        spine_ids.append(ref)

    id_to_item = {item.get_id(): item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT}

    images = []
    for idref in spine_ids:
        item = id_to_item.get(idref)
        if not item:
            continue
        content = item.get_content()
        try:
            html = content.decode('utf-8', errors='ignore')
            soup = BeautifulSoup(html, 'html.parser')
            img_tags = soup.find_all('img')
            for img_tag in img_tags:
                src = img_tag.get('src')
                if not src:
                    continue
                norm_src = src.replace('\\', '/')
                if norm_src.startswith('../'):
                    norm_src = norm_src[3:]
                elif norm_src.startswith('./'):
                    norm_src = norm_src[2:]
                img_data = image_map.get(norm_src)
                if img_data is None:
                    base = os.path.basename(norm_src)
                    img_data = image_map.get(base)
                if img_data is not None:
                    img = Image.open(io.BytesIO(img_data))
                    images.append(img)
                else:
                    logger.warning(f"无法匹配图片: {src}")
        except Exception as e:
            logger.warning(f"解析文档出错: {e}")

    if not images:
        logger.warning("按 spine 顺序未提取到图片，回退到自然排序")
        from natsort import natsorted
        image_items = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_IMAGE]
        image_items = natsorted(image_items, key=lambda x: x.get_name())
        for img_item in image_items:
            img_data = img_item.get_content()
            img = Image.open(io.BytesIO(img_data))
            images.append(img)
        logger.info(f"自然排序提取到 {len(images)} 张图片")
    else:
        logger.info(f"按 spine 顺序提取到 {len(images)} 张图片")
    return images

# 日志配置（默认启用文件日志）
def setup_viewer_logger():
    logger = logging.getLogger("Ebook2XTX_Viewer")
    logger.setLevel(logging.DEBUG)
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console.setFormatter(fmt)
    logger.addHandler(console)
    log_dir = Path.cwd() / "log"
    log_dir.mkdir(exist_ok=True)
    log_filename = log_dir / f"Ebook2XTX_Viewer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger

logger = setup_viewer_logger()

# 辅助函数：从路径获取原始图片列表（不切割、不处理）
def get_raw_image_paths_from_path(path: str, temp_dir: Path) -> Tuple[List[str], str]:
    """
    根据输入的路径（文件或文件夹）提取原始图片路径列表。
    对于压缩包或电子书，解压到临时目录并返回临时目录中的图片路径。
    对于文件夹，直接扫描返回图片路径。
    返回 (图片路径列表, 源描述)
    """
    path_obj = Path(path)
    if not path_obj.exists():
        return [], "路径不存在"

    # 如果是文件
    if path_obj.is_file():
        ext = path_obj.suffix.lower()
        # 压缩包
        if ext in ('.zip', '.cbz', '.7z', '.rar', '.cbr'):
            extract_dir = temp_dir / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            if extract_archive(path_obj, extract_dir):
                image_paths = []
                for img_ext in ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif'):
                    image_paths.extend(extract_dir.rglob(f'*{img_ext}'))
                from natsort import natsorted
                image_paths = natsorted([str(p) for p in image_paths])
                return image_paths, f"压缩包: {path_obj.name}"
            else:
                return [], f"解压失败: {path_obj.name}"
        # 电子书 (epub, pdf, mobi, azw3)
        elif ext in ('.epub', '.pdf', '.mobi', '.azw3'):
            try:
                if ext == '.epub':
                    images = extract_images_from_epub_spine(path_obj)
                else:
                    # PDF/MOBI/AZW3 使用主程序的提取函数（暂时不支持预览，回退到简单提取）
                    from ebook2xtx import extract_images_from_ebook
                    images = extract_images_from_ebook(path_obj)
                if not images:
                    return [], f"电子书无图片: {path_obj.name}"
                img_dir = temp_dir / "ebook_images"
                img_dir.mkdir(parents=True, exist_ok=True)
                image_paths = []
                for i, img in enumerate(images):
                    save_path = img_dir / f"page_{i+1:04d}.png"
                    img.save(save_path)
                    image_paths.append(str(save_path))
                return image_paths, f"电子书: {path_obj.name}"
            except Exception as e:
                return [], f"电子书解析失败: {e}"
        else:
            return [], f"不支持的文件类型: {ext}"
    # 如果是文件夹
    elif path_obj.is_dir():
        image_paths = []
        for img_ext in ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif'):
            image_paths.extend(path_obj.rglob(f'*{img_ext}'))
        from natsort import natsorted
        image_paths = natsorted([str(p) for p in image_paths])
        return image_paths, f"文件夹: {path_obj.name}"
    else:
        return [], "无效路径"

# 预览窗口类
class PreviewWindow(tk.Toplevel):
    def __init__(self, parent, main_app):
        super().__init__(parent)
        self.main_app = main_app
        self.title("Ebook2XTX - 预览窗口")
        self.geometry("1100x800")
        self.minsize(800, 600)

        # 临时目录（用于存放解压的图片）
        self.temp_dir = None
        self.current_source_desc = ""

        # 原始图片路径列表
        self.raw_image_paths: List[str] = []
        # 最终页面列表（每个元素是 PIL Image 对象，已完整处理）
        self.page_images: List[Image.Image] = []
        # 页面元数据列表，每个元素为 (raw_idx, split_idx)
        self.page_metadata: List[Tuple[int, int]] = []
        self.current_page: int = 0
        self.total_pages: int = 0

        # 图像渲染相关
        self.display_image: Optional[Image.Image] = None
        self.photo_image: Optional[ImageTk.PhotoImage] = None
        self.canvas_image_id = None

        # 缩放相关
        self.zoom_factor: float = 1.0
        self.scale_mode = tk.StringVar(value="预览")
        self.scale_factors = {"原图": None, "预览(原分辨率)": None, "预览": None}

        # 双页显示（暂不实现，保持简单）
        self.double_page = False
        self.double_page_var = tk.BooleanVar(value=False)

        # 背景颜色
        self.background_color = "gray"

        # 全屏相关
        self.fullscreen = False
        self.menubar = None
        self.status_frame = None
        self.original_geometry = None

        # 创建UI
        self._create_widgets()
        self._bind_events()

        # 设置窗口关闭协议（隐藏而非销毁）
        self.protocol("WM_DELETE_WINDOW", self.hide)

        # 延迟刷新
        self._refresh_after_id = None

        # 初始显示提示
        self._show_placeholder()

    def _show_placeholder(self):
        """显示未加载图片的提示"""
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas.winfo_width() // 2,
            self.canvas.winfo_height() // 2,
            text="请从「文件」菜单打开漫画文件或文件夹",
            fill="gray",
            font=("微软雅黑", 14)
        )

    def _create_widgets(self):
        # 菜单栏
        self.menubar = tk.Menu(self)

        # 文件菜单
        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="打开文件...", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="打开文件夹...", command=self.open_folder)
        file_menu.add_separator()
        file_menu.add_command(label="保存当前页为图片", command=self.save_current_page)
        file_menu.add_separator()
        file_menu.add_command(label="关闭预览", command=self.hide)
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
        # 缩放模式
        zoom_menu = tk.Menu(settings_menu, tearoff=0)
        for mode in self.scale_factors.keys():
            zoom_menu.add_radiobutton(label=mode, variable=self.scale_mode, value=mode,
                                      command=self.on_scale_mode_changed)
        settings_menu.add_cascade(label="缩放模式", menu=zoom_menu)
        # 背景颜色
        bg_menu = tk.Menu(settings_menu, tearoff=0)
        bg_menu.add_command(label="灰色", command=lambda: self.set_background_color("gray"))
        bg_menu.add_command(label="白色", command=lambda: self.set_background_color("white"))
        bg_menu.add_command(label="黑色", command=lambda: self.set_background_color("black"))
        bg_menu.add_command(label="自定义...", command=self.custom_background_color)
        settings_menu.add_cascade(label="背景颜色", menu=bg_menu)
        # 全屏
        settings_menu.add_separator()
        settings_menu.add_command(label="全屏 (F11)", command=self.toggle_fullscreen)

        self.menubar.add_cascade(label="设置", menu=settings_menu)

        self.config(menu=self.menubar)

        # 主框架
        main_frame = ttk.Frame(self, padding=5)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 画布区域
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

        # 绑定快捷键
        self.bind('<Control-o>', lambda e: self.open_file())
        self.bind('<Control-O>', lambda e: self.open_file())

    def _bind_events(self):
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

    # ---------- 打开文件/文件夹 ----------
    def open_file(self):
        file_path = filedialog.askopenfilename(
            title="选择漫画文件",
            filetypes=[
                ("支持格式", "*.zip *.cbz *.7z *.rar *.cbr *.epub *.pdf *.mobi *.azw3"),
                ("压缩包", "*.zip *.cbz *.7z *.rar *.cbr"),
                ("电子书", "*.epub *.pdf *.mobi *.azw3"),
                ("所有文件", "*.*")
            ]
        )
        if file_path:
            self._load_path(file_path)

    def open_folder(self):
        dir_path = filedialog.askdirectory(title="选择漫画文件夹")
        if dir_path:
            self._load_path(dir_path)

    def _load_path(self, path: str):
        """加载指定路径的图片列表，并生成最终页面列表"""
        # 清理之前的临时目录
        if self.temp_dir:
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except:
                pass
        # 创建新临时目录
        self.temp_dir = tempfile.mkdtemp(prefix="ebook2xtx_preview_")
        raw_paths, desc = get_raw_image_paths_from_path(path, Path(self.temp_dir))
        if not raw_paths:
            messagebox.showerror("错误", f"无法加载图片: {desc}")
            self._show_placeholder()
            return
        self.raw_image_paths = raw_paths
        self.current_source_desc = desc
        self.status.config(text=f"已加载: {desc} (原始图片 {len(raw_paths)} 张)")
        # 生成最终页面列表（根据当前设置）
        self._rebuild_page_list()

    def _get_current_settings(self) -> Dict[str, Any]:
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
        if self.scale_mode.get() == "预览":
            res_type, res_value = main.get_resolution()
            if res_type != 'original' and res_value:
                settings['target_width'], settings['target_height'] = res_value
        return settings

    def _rebuild_page_list(self):
        """根据当前设置，重新生成所有最终页面图像（在后台线程中执行）"""
        if not self.raw_image_paths:
            return

        # 保存当前页面对应的原始图片索引和切割块索引（用于恢复）
        current_raw_idx = -1
        current_split_idx = -1
        if 0 <= self.current_page < len(self.page_metadata):
            current_raw_idx, current_split_idx = self.page_metadata[self.current_page]

        self.status.config(text="正在生成预览页面...")
        self.update_idletasks()

        def build():
            settings = self._get_current_settings()
            new_page_images = []
            new_metadata = []
            for raw_idx, img_path in enumerate(self.raw_image_paths):
                try:
                    with Image.open(img_path) as f:
                        f.load()
                        raw_img = f.copy()
                except Exception as e:
                    logger.error(f"加载图片失败 {img_path}: {e}")
                    continue
                is_first = (raw_idx == 0)
                is_last = (raw_idx == len(self.raw_image_paths) - 1)
                splits = self._get_split_images(raw_img, settings, is_first, is_last)
                for split_idx, split_img in enumerate(splits):
                    new_page_images.append(split_img)
                    new_metadata.append((raw_idx, split_idx))
            # 更新UI
            self.page_images = new_page_images
            self.page_metadata = new_metadata
            self.total_pages = len(self.page_images)
            # 尝试恢复页码
            new_page = 0
            if current_raw_idx >= 0 and current_split_idx >= 0:
                # 查找新列表中相同 (raw_idx, split_idx) 的页面
                for idx, (r, s) in enumerate(new_metadata):
                    if r == current_raw_idx and s == current_split_idx:
                        new_page = idx
                        break
                else:
                    # 如果找不到相同切割块，尝试找到相同原始图片的第一个块
                    for idx, (r, s) in enumerate(new_metadata):
                        if r == current_raw_idx:
                            new_page = idx
                            break
            # 确保新页码有效
            if new_page >= self.total_pages:
                new_page = 0
            self.current_page = new_page
            self.after(0, self._on_rebuild_done)

        threading.Thread(target=build, daemon=True).start()

    def _on_rebuild_done(self):
        self.status.config(text=f"已加载: {self.current_source_desc} (最终页面 {self.total_pages} 页)")
        self._update_page_display()
        if self.total_pages > 0:
            self.render_current_page()
        else:
            self._show_placeholder()

    def _get_split_images(self, img: Image.Image, settings: Dict, is_first: bool, is_last: bool) -> List[Image.Image]:
        """对单张原始图片应用完整处理，返回切割后的所有子图列表（不切割则返回单元素列表）"""
        # 透明处理
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            img = fill_transparent_with_white(img)
        img = img.convert('L')
        # 裁边
        if settings.get('auto_crop', False):
            img = crop_white_black_borders(img)
        # 旋转（整体）
        rotate_mode = settings.get('rotate_mode', 'none')
        is_landscape = img.width > img.height
        if is_landscape and rotate_mode != "none":
            img = rotate_image(img, rotate_mode)
        # 判断是否需要切割
        crop_cfg = settings.get('crop', {'mode': 0})
        should_crop = (not is_first and not is_last) and (not is_landscape) and (crop_cfg.get('mode', 0) != 0)
        splits = []
        if should_crop:
            mode = crop_cfg['mode']
            if mode == 2:
                ratio = crop_cfg['ratio']
                splits = split_image_vertically(img, 2, ratio)
            elif mode == 3:
                ratio = crop_cfg['ratio']
                splits = split_image_vertically(img, 3, ratio)
            elif mode == 4:
                overlap = crop_cfg['overlap_percent']
                splits = split_rolling_2(img, overlap)
            elif mode == 5:
                overlap = crop_cfg['overlap_percent']
                splits = split_rolling_3(img, overlap)
            else:
                splits = [img]
            # 切割后对每个子图再次旋转（如果用户需要且子图是横版）
            if rotate_mode != "none":
                splits = [rotate_image(part, rotate_mode) if part.width > part.height else part for part in splits]
        else:
            splits = [img]

        # 对每个子图进行缩放、增强、抖动
        target_w = settings.get('target_width', 0)
        target_h = settings.get('target_height', 0)
        stretch = settings.get('stretch', False)
        sharpen = settings.get('sharpen', 0)
        contrast = settings.get('contrast', 0)
        clahe = settings.get('clahe', 0)
        bits = settings.get('output_bits', 1)
        dither_algo = settings.get('dither_algo', 'Floyd-Steinberg')
        dither_strength = settings.get('dither_strength', 0.7)

        processed_splits = []
        for part in splits:
            if target_w > 0 and target_h > 0:
                part = resize_to_target(part, target_w, target_h, stretch)
            if sharpen > 0:
                part = apply_sharpen(part, sharpen)
            if contrast > 0:
                part = apply_contrast(part, contrast)
            if clahe > 0:
                part = apply_clahe(part, clahe)
            gray_arr = np.array(part, dtype=np.float32)
            if dither_algo == 'Floyd-Steinberg':
                dithered = floyd_steinberg_dither_numba(gray_arr, bits, dither_strength)
            elif dither_algo == 'Atkinson':
                dithered = atkinson_dither_numba(gray_arr, bits, dither_strength)
            else:
                dithered = no_dither_quantize(gray_arr, bits)
            processed_splits.append(Image.fromarray(dithered, mode='L'))
        return processed_splits

    def render_current_page(self):
        if self.current_page < 0 or self.current_page >= self.total_pages:
            return
        img = self.page_images[self.current_page]
        self._apply_zoom(img)

    def _apply_zoom(self, img: Image.Image):
        w = int(img.width * self.zoom_factor)
        h = int(img.height * self.zoom_factor)
        self.display_image = img.resize((w, h), Image.Resampling.LANCZOS)
        self.photo_image = ImageTk.PhotoImage(self.display_image)
        self._update_canvas()

    def _update_canvas(self):
        self.canvas.delete("all")
        if self.display_image is None:
            self._show_placeholder()
            return
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        img_w = self.display_image.width
        img_h = self.display_image.height
        x = max(0, (canvas_w - img_w) // 2)
        y = max(0, (canvas_h - img_h) // 2)
        self.canvas_image_id = self.canvas.create_image(x, y, anchor=tk.NW, image=self.photo_image)
        self.canvas.config(scrollregion=(0, 0, img_w, img_h))
        mode_name = self.scale_mode.get()
        self.status.config(text=f"第 {self.current_page+1}/{self.total_pages} 页 | 显示尺寸 {img_w}x{img_h} | 模式 {mode_name} | 缩放 {int(self.zoom_factor*100)}% | {self.current_source_desc}")

    def _update_page_display(self):
        self.page_status_label.config(text=f"第 {self.current_page+1} / {self.total_pages} 页")

    # ---------- 翻页 ----------
    def prev_page(self):
        if self.total_pages == 0:
            return
        self.current_page = (self.current_page - 1) % self.total_pages
        self._update_page_display()
        self.render_current_page()

    def next_page(self):
        if self.total_pages == 0:
            return
        self.current_page = (self.current_page + 1) % self.total_pages
        self._update_page_display()
        self.render_current_page()

    def jump_to_page(self, page_index: int):
        if 0 <= page_index < self.total_pages:
            self.current_page = page_index
            self._update_page_display()
            self.render_current_page()

    def show_jump_dialog(self):
        if self.total_pages == 0:
            return
        result = simpledialog.askinteger("跳转到页码", f"请输入页码 (1-{self.total_pages}):",
                                         parent=self, initialvalue=self.current_page+1,
                                         minvalue=1, maxvalue=self.total_pages)
        if result:
            self.jump_to_page(result-1)

    # ---------- 缩放模式 ----------
    def on_scale_mode_changed(self):
        self.render_current_page()

    # ---------- 背景颜色 ----------
    def set_background_color(self, color):
        self.background_color = color
        self.canvas.config(bg=color)

    def custom_background_color(self):
        color = simpledialog.askstring("自定义背景颜色", "请输入颜色代码 (如 #RRGGBB 或 gray, white, black):",
                                       parent=self, initialvalue=self.background_color)
        if color:
            self.set_background_color(color)

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

    # ---------- 保存当前页 ----------
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

    # ---------- 事件 ----------
    def on_window_resize(self, event):
        if self.display_image:
            self._update_canvas()
        else:
            self._show_placeholder()

    def on_mousewheel(self, event):
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if self.display_image:
            img_w = self.display_image.width
            img_h = self.display_image.height
            if img_w <= canvas_w and img_h <= canvas_h:
                if event.delta > 0 or event.num == 4:
                    self.prev_page()
                else:
                    self.next_page()
            else:
                if event.delta > 0 or event.num == 4:
                    self.canvas.yview_scroll(-1, "units")
                else:
                    self.canvas.yview_scroll(1, "units")
        else:
            if event.delta > 0 or event.num == 4:
                self.prev_page()
            else:
                self.next_page()

    def on_ctrl_mousewheel(self, event):
        if event.delta > 0 or event.num == 4:
            self.zoom_factor *= 1.1
        else:
            self.zoom_factor *= 0.9
        self.zoom_factor = max(0.1, min(5.0, self.zoom_factor))
        self.render_current_page()

    def refresh_preview(self):
        """主程序设置改变时调用，重新生成页面列表"""
        if self._refresh_after_id:
            self.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.after(200, self._do_refresh)

    def _do_refresh(self):
        if self.raw_image_paths:
            self._rebuild_page_list()
        self._refresh_after_id = None

    # ---------- 显示/隐藏 ----------
    def show(self):
        self.deiconify()
        if not self.raw_image_paths:
            self._show_placeholder()
        else:
            self.refresh_preview()

    def hide(self):
        self.withdraw()

    def destroy(self):
        if self.temp_dir:
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except:
                pass
        super().destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("错误", "预览模块不可单独启动，请从 Ebook2XTX_GUI 主程序调用。")
    sys.exit(1)