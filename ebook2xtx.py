#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
电子书/文档转 XTC/XTCH/XTG/XTH/图片/电子书 格式 (Ebook2XTX v1.6)
支持输出电子书格式：EPUB, PDF
"""

import os
import sys
import logging
import tempfile
import re
import time
import zipfile
import struct
import subprocess
import io
import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any, Callable, Union
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support

from core import _process_single_image

# ========== 依赖管理 ==========
def check_and_install_dependencies():
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
        ('img2pdf', 'import img2pdf')
    ]
    missing_packages = []
    for package, import_stmt in required:
        try:
            exec(import_stmt)
        except ImportError:
            missing_packages.append(package)
    if missing_packages:
        print(f"检测到缺失的依赖: {', '.join(missing_packages)}")
        print("正在安装...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing_packages])
            print("依赖安装完成。")
            for _, import_stmt in required:
                try:
                    exec(import_stmt)
                except ImportError:
                    print(f"警告: 无法导入 {import_stmt.split()[-1]}，请手动安装")
                    return False
            return True
        except Exception as e:
            print(f"安装依赖失败: {e}")
            return False
    return True

if __name__ == "__main__":
    freeze_support()
    if not check_and_install_dependencies():
        input("按回车键退出...")
        sys.exit(1)

from PIL import Image
import py7zr
import rarfile
from natsort import natsorted
import numpy as np
from numba import njit
import fitz
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import mobi
import img2pdf

# ========== 日志配置 ==========
def setup_logging():
    log_dir = Path.cwd() / "log"
    log_dir.mkdir(exist_ok=True)
    log_filename = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()
_gif_mode = None

# ---------- 基础交互函数 ----------
def get_user_choice(prompt: str, options: Dict[int, str], default: int) -> int:
    print(prompt)
    for key, desc in options.items():
        print(f"{key}. {desc}")
    choice = input(f"请输入序号 [默认 {default}]: ").strip()
    if not choice:
        return default
    try:
        choice_int = int(choice)
        if choice_int in options:
            return choice_int
    except ValueError:
        pass
    print(f"无效输入，使用默认 {default}")
    return default

def get_float_input(prompt: str, default: float) -> float:
    value = input(f"{prompt} [默认 {default}]: ").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        print(f"无效输入，使用默认 {default}")
        return default

def get_int_input(prompt: str, default: int) -> int:
    value = input(f"{prompt} [默认 {default}]: ").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"无效输入，使用默认 {default}")
        return default

def parse_size_string(size_str: str) -> int:
    size_str = size_str.strip()
    if not size_str:
        raise ValueError("输入不能为空")
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([kKmMgG]?[bB]?)$', size_str)
    if not match:
        raise ValueError("格式错误，请输入数字+可选单位（k/KB/m/MB/g/GB）")
    num = float(match.group(1))
    unit = match.group(2).lower()
    if unit == '' or unit == 'm' or unit == 'mb':
        return int(num * 1024 * 1024)
    elif unit == 'k' or unit == 'kb':
        return int(num * 1024)
    elif unit == 'g' or unit == 'gb':
        return int(num * 1024 * 1024 * 1024)
    else:
        raise ValueError(f"不支持的单位: {unit}")

def get_split_size() -> int:
    print("\n请选择分包大小：")
    options = {1: "4G (FAT32 最大文件大小)", 2: "自定义", 3: "不分包"}
    choice = get_user_choice("", options, 1)
    if choice == 1:
        return 4 * 1024 * 1024 * 1024
    elif choice == 2:
        while True:
            size_input = input("请输入分包大小（支持单位 k/KB/m/MB/g/GB，不输入单位默认为 MB）: ").strip()
            try:
                size_bytes = parse_size_string(size_input)
                if size_bytes <= 0:
                    print("分包大小必须大于 0")
                    continue
                return size_bytes
            except ValueError as e:
                print(f"输入错误: {e}，请重新输入")
    else:
        return 0

def get_resolution_custom() -> Tuple[int, int]:
    while True:
        res_str = input("请输入分辨率 (格式: 宽x高，例如 800x1280): ").strip()
        if not res_str:
            print("分辨率不能为空")
            continue
        match = re.match(r'^(\d+)\s*[xX]\s*(\d+)$', res_str)
        if match:
            w = int(match.group(1))
            h = int(match.group(2))
            if w > 0 and h > 0:
                return w, h
        print("格式错误，请使用数字x数字，例如 800x1280")

def get_filename_format(ext: str = "xtg") -> int:
    options = {
        1: f"编号 (例如 1.{ext})",
        2: f"电子书名-编号 (例如 电子书名-1.{ext})"
    }
    choice = get_user_choice("请选择输出文件名格式：", options, 1)
    return 0 if choice == 1 else 1

def get_crop_settings():
    print("\n画面切割选项：")
    crop_options = {1: "不切割", 2: "横切2图", 3: "横切3图"}
    crop_choice = get_user_choice("请选择切割方式：", crop_options, 1)
    if crop_choice == 1:
        return {'mode': 0, 'ratio': None}
    if crop_choice == 2:
        sub_options = {
            1: "1 : 1.618 (黄金比例，上少下多)",
            2: "1.618 : 1 (上多下少)",
            3: "1 : 1 (上下等分)",
            4: "1 : 1 (3图滚动)"
        }
        sub_choice = get_user_choice("请选择切割方式：", sub_options, 1)
        if sub_choice == 1:
            return {'mode': 2, 'ratio': (1, 1.618)}
        elif sub_choice == 2:
            return {'mode': 2, 'ratio': (1.618, 1)}
        elif sub_choice == 3:
            return {'mode': 2, 'ratio': (1, 1)}
        else:
            overlap = get_int_input("请输入重叠比例 (0-100，默认 100):", 100)
            overlap = max(0, min(100, overlap))
            return {'mode': 4, 'overlap_percent': overlap}
    else:
        sub_options = {
            1: "1 : 2 : 1",
            2: "2 : 1 : 1",
            3: "1 : 1 : 2",
            4: "1 : 1 : 1",
            5: "1 : 1 : 1（5图滚动）"
        }
        sub_choice = get_user_choice("请选择切割方式：", sub_options, 1)
        if sub_choice == 1:
            return {'mode': 3, 'ratio': (1, 2, 1)}
        elif sub_choice == 2:
            return {'mode': 3, 'ratio': (2, 1, 1)}
        elif sub_choice == 3:
            return {'mode': 3, 'ratio': (1, 1, 2)}
        elif sub_choice == 4:
            return {'mode': 3, 'ratio': (1, 1, 1)}
        else:
            overlap = get_int_input("请输入重叠比例 (0-100，默认 100):", 100)
            overlap = max(0, min(100, overlap))
            return {'mode': 5, 'overlap_percent': overlap}

def get_output_format_choice():
    print("\n请选择输出格式：")
    options = {
        1: "XTC (1-bit 黑白，容器格式，单文件)",
        2: "XTCH (2-bit 4级灰度，容器格式，单文件)",
        3: "XTG (1-bit 黑白，单页模式，每个图片单独输出)",
        4: "XTH (2-bit 4级灰度，单页模式，每个图片单独输出)",
        5: "图片格式 (jpg/png/webp/bmp，每页保存为图片)",
        6: "电子书格式 (epub/pdf)"
    }
    choice = get_user_choice("", options, 1)
    if choice == 5:
        print("\n请选择具体图片格式：")
        img_opts = {1: "JPEG (.jpg)", 2: "PNG (.png)", 3: "WebP (.webp)", 4: "BMP (.bmp)"}
        img_choice = get_user_choice("", img_opts, 1)   # 默认改为 1 (JPEG)
        img_map = {1: "jpg", 2: "png", 3: "webp", 4: "bmp"}
        return ("image", img_map[img_choice])
    elif choice == 6:
        print("\n请选择电子书格式：")
        ebook_opts = {1: "EPUB (.epub)", 2: "PDF (.pdf)"}
        ebook_choice = get_user_choice("", ebook_opts, 1)
        ebook_map = {1: "epub", 2: "pdf"}
        return ("ebook", ebook_map[ebook_choice])
    else:
        fmt_map = {1: "xtc", 2: "xtch", 3: "xtg", 4: "xth"}
        return ("format", fmt_map[choice])

def get_resolution_choice():
    print("\n请选择目标分辨率：")
    options = {
        1: "X4 (480×800)",
        2: "X4 双倍分辨率 (960×1600)",
        3: "X3 (528×792)",
        4: "X3 双倍分辨率 (1056×1584)",
        5: "原分辨率（保持每张图片原始尺寸）",
        6: "自定义分辨率"
    }
    choice = get_user_choice("", options, 1)
    if choice == 5:
        return "original", None
    elif choice == 6:
        w, h = get_resolution_custom()
        return "custom", (w, h)
    else:
        res_map = {1: (480, 800), 2: (960, 1600), 3: (528, 792), 4: (1056, 1584)}
        return "preset", res_map[choice]

def get_user_settings():
    print("\n请选择转换设置：")
    out_type, out_value = get_output_format_choice()
    res_type, res_value = get_resolution_choice()
    crop_choice = get_user_choice("是否自动裁切黑白边？", {1: "是", 2: "否"}, 1)
    auto_crop = (crop_choice == 1)
    rotate_choice = get_user_choice("横版图片旋转方式：", {1: "顺时针90度", 2: "逆时针90度", 3: "不旋转"}, 1)
    rotate_mode = "clockwise" if rotate_choice == 1 else "counterclockwise" if rotate_choice == 2 else "none"
    crop_cfg = get_crop_settings()
    stretch = True
    if res_type != "original":
        stretch_choice = get_user_choice("是否拉伸图片至全屏？", {1: "是 (拉伸至全屏)", 2: "否 (保持比例，填充黑边)"}, 1)
        stretch = (stretch_choice == 1)
    else:
        print("原分辨率模式下，拉伸选项无效，将保持原始比例。")
    dither_strength = get_float_input("请输入抖动强度 (0-1 之间):", 0.7)
    cpu_count = os.cpu_count() or 1
    default_workers = min(cpu_count, 61)
    workers_input = input(f"请输入同时处理的图片数量 (进程数，建议不超过 {default_workers}，默认 {default_workers}): ").strip()
    if workers_input.isdigit():
        max_workers = int(workers_input)
        if max_workers > 61:
            print(f"Windows 限制最大 61 个进程，已自动调整为 61")
            max_workers = 61
    else:
        max_workers = default_workers

    # 文件名格式（仅单页模式或图片格式需要）
    filename_format = None
    if out_type == "image" or (out_type == "format" and out_value in ('xtg', 'xth')):
        if out_type == "image":
            ext = out_value   # jpg, png, webp, bmp
        else:
            ext = out_value   # xtg 或 xth
        filename_format = get_filename_format(ext)

    split_size = None
    if out_type == "format" and out_value in ('xtc', 'xtch'):
        split_size = get_split_size()

    print("\nGIF 动图处理方式：")
    gif_options = {1: "只处理第一帧", 2: "处理所有帧", 3: "跳过 GIF 文件（不转换）"}
    gif_mode = get_user_choice("请选择：", gif_options, 1)

    return {
        'out_type': out_type,
        'out_value': out_value,
        'res_type': res_type,
        'res_value': res_value,
        'auto_crop': auto_crop,
        'rotate_mode': rotate_mode,
        'crop': crop_cfg,
        'stretch': stretch,
        'dither_strength': dither_strength,
        'max_workers': max_workers,
        'filename_format': filename_format,
        'split_size': split_size,
        'gif_mode': gif_mode
    }

# ---------- 电子书处理辅助函数 ----------
def extract_text_from_html(html_content: str) -> str:
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator=' ', strip=True)

def guess_document_type(total_pages: int, total_images: int, total_text_chars: int) -> str:
    if total_pages == 0:
        return 'text'
    avg_images_per_page = total_images / total_pages
    if avg_images_per_page > 0.8:
        return 'comic'
    elif avg_images_per_page > 0.2:
        return 'mixed'
    else:
        return 'text'

def get_epub_stats(epub_path: Path) -> Tuple[int, int, int]:
    book = epub.read_epub(epub_path)
    text_items = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]
    image_items = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_IMAGE]
    total_pages = len(text_items)
    total_images = len(image_items)
    total_text_chars = 0
    for item in text_items:
        content = item.get_content()
        try:
            html = content.decode('utf-8', errors='ignore')
            total_text_chars += len(extract_text_from_html(html))
        except:
            pass
    return total_pages, total_images, total_text_chars

def get_pdf_stats(pdf_path: Path) -> Tuple[int, int, int]:
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    total_text_chars = 0
    for page_num in range(total_pages):
        total_text_chars += len(doc.load_page(page_num).get_text())
    doc.close()
    total_images = total_pages
    return total_pages, total_images, total_text_chars

def get_mobi_stats(mobi_path: Path) -> Tuple[int, int, int]:
    temp_dir = None
    try:
        temp_dir, entry_point = mobi.extract(str(mobi_path))
        temp_path = Path(temp_dir)
        epub_candidates = list(temp_path.glob("mobi8/*.epub")) + list(temp_path.glob("*.epub"))
        if not epub_candidates:
            return 0, 0, 0
        epub_file = epub_candidates[0]
        return get_epub_stats(epub_file)
    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

def extract_images_from_epub(epub_path: Path) -> List[Image.Image]:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    import io
    from PIL import Image
    import logging
    import os
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

def extract_images_from_pdf(pdf_path: Path, dpi: int = 150) -> List[Image.Image]:
    doc = fitz.open(pdf_path)
    images = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        img = Image.frombytes("L", [pix.width, pix.height], pix.samples)
        images.append(img)
    doc.close()
    return images

def extract_images_from_mobi_azw3(mobi_path: Path) -> List[Image.Image]:
    temp_dir = None
    try:
        temp_dir, entry_point = mobi.extract(str(mobi_path))
        temp_path = Path(temp_dir)
        epub_candidates = list(temp_path.glob("mobi8/*.epub")) + list(temp_path.glob("*.epub"))
        if not epub_candidates:
            return []
        epub_file = epub_candidates[0]
        return extract_images_from_epub(epub_file)
    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

def extract_text_from_epub(epub_path: Path) -> str:
    book = epub.read_epub(epub_path)
    text_items = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]
    full_text = []
    for item in text_items:
        content = item.get_content()
        try:
            html = content.decode('utf-8', errors='ignore')
            full_text.append(extract_text_from_html(html))
        except:
            pass
    return "\n".join(full_text)

def extract_text_from_pdf(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    full_text = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        full_text.append(page.get_text())
    doc.close()
    return "\n".join(full_text)

def extract_text_from_mobi_azw3(mobi_path: Path) -> str:
    temp_dir = None
    try:
        temp_dir, entry_point = mobi.extract(str(mobi_path))
        temp_path = Path(temp_dir)
        epub_candidates = list(temp_path.glob("mobi8/*.epub")) + list(temp_path.glob("*.epub"))
        if not epub_candidates:
            return ""
        epub_file = epub_candidates[0]
        return extract_text_from_epub(epub_file)
    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

# ---------- XTC/XTCH/XTG/XTH 读取器 ----------
class XTCReader:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.container_mode = False
        self.page_files = []
        self.pages = []
        self.page_count = 0
        self.title = ""
        self.author = ""
        self.is_hq = False
        self.chapters = []
        self.f = None
        ext = os.path.splitext(filepath)[1].lower()
        if ext in ('.xtc', '.xtch'):
            self.container_mode = True
            self._parse_container()
        elif ext in ('.xtg', '.xth'):
            self.container_mode = False
            self._load_single_page_files(filepath)
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    def _parse_container(self):
        self.f = open(self.filepath, 'rb')
        magic = self.f.read(4)
        if magic == b'XTC\0':
            self.is_hq = False
        elif magic == b'XTCH':
            self.is_hq = True
        else:
            raise ValueError(f"未知文件格式: {magic}")
        self.f.read(2)  # version
        self.page_count = struct.unpack('<H', self.f.read(2))[0]
        self.f.read(1)  # read_dir
        has_metadata = self.f.read(1)[0]
        self.f.read(1)  # has_thumbnails
        has_chapters = self.f.read(1)[0]
        self.f.read(4)  # current_page
        metadata_offset = struct.unpack('<Q', self.f.read(8))[0]
        index_offset = struct.unpack('<Q', self.f.read(8))[0]
        data_offset = struct.unpack('<Q', self.f.read(8))[0]
        self.f.read(8)  # thumb_offset
        chapter_offset = struct.unpack('<Q', self.f.read(8))[0]
        if has_metadata:
            self._parse_metadata(metadata_offset)
        self._parse_index(index_offset)
        if has_chapters:
            self._parse_chapters(chapter_offset)

    def _load_single_page_files(self, filepath):
        dir_path = os.path.dirname(filepath)
        files = []
        for ext in ('.xtg', '.xth'):
            files.extend(Path(dir_path).glob(f'*{ext}'))
        files = natsorted(files, key=lambda p: p.name)
        if not files:
            raise ValueError(f"目录 {dir_path} 中没有找到 .xtg 或 .xth 文件")
        self.page_files = [str(f) for f in files]
        self.page_count = len(self.page_files)
        self.title = os.path.basename(dir_path)
        self.author = ""

    def _parse_metadata(self, offset):
        self.f.seek(offset)
        title_bytes = self.f.read(128)
        self.title = title_bytes.split(b'\x00')[0].decode('utf-8', errors='ignore')
        author_bytes = self.f.read(64)
        self.author = author_bytes.split(b'\x00')[0].decode('utf-8', errors='ignore')

    def _parse_index(self, offset):
        self.f.seek(offset)
        for i in range(self.page_count):
            page_offset = struct.unpack('<Q', self.f.read(8))[0]
            page_size = struct.unpack('<I', self.f.read(4))[0]
            self.f.read(4)  # width+height
            self.pages.append((page_offset, page_size))

    def _parse_chapters(self, offset):
        self.f.seek(offset)
        for i in range(self.page_count):
            name_bytes = self.f.read(80)
            name = name_bytes.split(b'\x00')[0].decode('utf-8', errors='ignore')
            start_page = struct.unpack('<H', self.f.read(2))[0]
            end_page = struct.unpack('<H', self.f.read(2))[0]
            self.f.read(12)
            if name:
                self.chapters.append({'name': name, 'start': start_page-1, 'end': end_page-1})
            else:
                break

    def get_page_image(self, page_index: int) -> Image.Image:
        if page_index < 0 or page_index >= self.page_count:
            raise IndexError(f"页码超出范围: {page_index}")
        if self.container_mode:
            offset, size = self.pages[page_index]
            self.f.seek(offset)
            page_data = self.f.read(size)
            if self.is_hq:
                return self._decode_xth(page_data)
            else:
                return self._decode_xtg(page_data)
        else:
            file_path = self.page_files[page_index]
            with open(file_path, 'rb') as f:
                file_data = f.read()
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.xtg' or file_data[:4] == b'XTG\0':
                return self._decode_xtg(file_data)
            else:
                return self._decode_xth(file_data)

    @staticmethod
    def _decode_xtg(data: bytes) -> Image.Image:
        if len(data) < 22:
            raise ValueError("数据过短")
        header = data[:22]
        actual_w = struct.unpack('<H', header[4:6])[0]
        actual_h = struct.unpack('<H', header[6:8])[0]
        if actual_w == 0 or actual_h == 0:
            raise ValueError("宽高为0")
        bitmap = data[22:]
        row_bytes = (actual_w + 7) // 8
        expected_size = row_bytes * actual_h
        if len(bitmap) < expected_size:
            raise ValueError("位图数据不足")
        img = Image.new('L', (actual_w, actual_h), 255)
        pixels = img.load()
        for y in range(actual_h):
            for x in range(actual_w):
                byte_idx = y * row_bytes + (x // 8)
                if byte_idx >= len(bitmap):
                    continue
                bit = 7 - (x % 8)
                pixel = (bitmap[byte_idx] >> bit) & 1
                pixels[x, y] = 0 if pixel == 0 else 255
        return img

    @staticmethod
    def _decode_xth(data: bytes) -> Image.Image:
        if len(data) < 22:
            raise ValueError("数据过短")
        header = data[:22]
        actual_w = struct.unpack('<H', header[4:6])[0]
        actual_h = struct.unpack('<H', header[6:8])[0]
        if actual_w == 0 or actual_h == 0:
            raise ValueError("宽高为0")
        planes = data[22:]
        col_bytes = (actual_h + 7) // 8
        plane_size = col_bytes * actual_w
        if len(planes) < plane_size * 2:
            raise ValueError("位平面数据不足")
        plane0 = planes[:plane_size]
        plane1 = planes[plane_size:plane_size*2]
        level_to_gray = {0: 255, 1: 85, 2: 170, 3: 0}
        img = Image.new('L', (actual_w, actual_h), 255)
        pixels = img.load()
        for x in range(actual_w-1, -1, -1):
            col_idx = actual_w - 1 - x
            for y in range(actual_h):
                byte_idx = col_idx * col_bytes + (y // 8)
                if byte_idx >= len(plane0):
                    continue
                bit_pos = 7 - (y % 8)
                b0 = (plane0[byte_idx] >> bit_pos) & 1
                b1 = (plane1[byte_idx] >> bit_pos) & 1
                level = (b1 << 1) | b0
                pixels[x, y] = level_to_gray.get(level, 255)
        return img

    def close(self):
        if self.container_mode and self.f:
            self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

# ---------- 输入项抽象 ----------
class InputItem:
    def __init__(self, name: str, doc_type: str, image_getter: Optional[Callable] = None, text_getter: Optional[Callable] = None):
        self.name = name
        self.doc_type = doc_type
        self._get_images = image_getter
        self._get_text = text_getter
    def get_images(self) -> List[Image.Image]:
        return self._get_images() if self._get_images else []
    def get_text(self) -> str:
        return self._get_text() if self._get_text else ""

def scan_input_items(root_dir: Path) -> List[InputItem]:
    items = []
    archive_exts = {'.zip', '.cbz', '.7z', '.cbr', '.rar'}
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in archive_exts:
                path = Path(dirpath) / f
                items.append(InputItem(
                    name=path.stem,
                    doc_type='comic',
                    image_getter=lambda p=path: extract_images_from_archive(p)
                ))
    folder_items = find_folder_ebooks(root_dir)
    for name, img_paths in folder_items:
        items.append(InputItem(
            name=name,
            doc_type='comic',
            image_getter=lambda paths=img_paths: [Image.open(p) for p in paths]
        ))
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in ('.xtc', '.xtch'):
                path = Path(dirpath) / f
                items.append(InputItem(
                    name=path.stem,
                    doc_type='comic',
                    image_getter=lambda p=path: extract_images_from_container(p)
                ))
    single_page_dirs = set()
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in ('.xtg', '.xth'):
                single_page_dirs.add(Path(dirpath))
    for d in single_page_dirs:
        items.append(InputItem(
            name=d.name,
            doc_type='comic',
            image_getter=lambda dir_path=d: extract_images_from_single_pages(dir_path)
        ))
    ebook_exts = {'.epub', '.mobi', '.azw3', '.pdf'}
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in ebook_exts:
                path = Path(dirpath) / f
                if ext == '.pdf':
                    pages, images, text_chars = get_pdf_stats(path)
                elif ext == '.epub':
                    pages, images, text_chars = get_epub_stats(path)
                else:
                    pages, images, text_chars = get_mobi_stats(path)
                doc_type = guess_document_type(pages, images, text_chars)
                if doc_type == 'comic':
                    items.append(InputItem(
                        name=path.stem,
                        doc_type='comic',
                        image_getter=lambda p=path: extract_images_from_ebook(p)
                    ))
                elif doc_type == 'text':
                    items.append(InputItem(
                        name=path.stem,
                        doc_type='text',
                        text_getter=lambda p=path: extract_text_from_ebook(p)
                    ))
                else:
                    items.append(InputItem(
                        name=path.stem,
                        doc_type='mixed',
                        image_getter=lambda p=path: extract_images_from_ebook(p),
                        text_getter=lambda p=path: extract_text_from_ebook(p)
                    ))
    seen = set()
    unique = []
    for item in items:
        key = (item.name, id(item._get_images) if item._get_images else id(item._get_text))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique

def extract_images_from_archive(archive_path: Path) -> List[Image.Image]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        if not extract_archive(archive_path, tmp_path):
            logger.error(f"解压失败: {archive_path}")
            return []
        image_paths = collect_images(tmp_path)
        if not image_paths:
            return []
        images = []
        for p in image_paths:
            with Image.open(p) as img:
                images.append(img.copy())
        return images

def extract_images_from_container(container_path: Path) -> List[Image.Image]:
    reader = XTCReader(str(container_path))
    images = []
    try:
        for i in range(reader.page_count):
            img = reader.get_page_image(i)
            images.append(img)
    finally:
        reader.close()
    return images

def extract_images_from_single_pages(dir_path: Path) -> List[Image.Image]:
    files = []
    for ext in ('.xtg', '.xth'):
        files.extend(dir_path.glob(f'*{ext}'))
    files = natsorted(files, key=lambda p: p.name)
    images = []
    for f in files:
        with open(f, 'rb') as fp:
            data = fp.read()
        if f.suffix == '.xtg' or data[:4] == b'XTG\0':
            img = XTCReader._decode_xtg(data)
        else:
            img = XTCReader._decode_xth(data)
        images.append(img)
    return images

def extract_images_from_ebook(ebook_path: Path) -> List[Image.Image]:
    ext = ebook_path.suffix.lower()
    if ext == '.pdf':
        return extract_images_from_pdf(ebook_path)
    elif ext == '.epub':
        return extract_images_from_epub(ebook_path)
    elif ext in ('.mobi', '.azw3'):
        return extract_images_from_mobi_azw3(ebook_path)
    else:
        return []

def extract_text_from_ebook(ebook_path: Path) -> str:
    ext = ebook_path.suffix.lower()
    if ext == '.pdf':
        return extract_text_from_pdf(ebook_path)
    elif ext == '.epub':
        return extract_text_from_epub(ebook_path)
    elif ext in ('.mobi', '.azw3'):
        return extract_text_from_mobi_azw3(ebook_path)
    else:
        return ""

def find_folder_ebooks(root_dir: Path) -> List[Tuple[str, List[Path]]]:
    img_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp',
                '.tif', '.tiff', '.ico', '.icns', '.mpo', '.hdr'}
    ebooks = {}
    for root, dirs, files in os.walk(root_dir):
        rel_path = Path(root).relative_to(root_dir)
        if rel_path == Path('.'):
            continue
        img_files = [Path(root) / f for f in files if Path(f).suffix.lower() in img_exts]
        if img_files:
            name_parts = rel_path.parts
            ebook_name = '-'.join(name_parts)
            if ebook_name in ebooks:
                ebooks[ebook_name].extend(img_files)
            else:
                ebooks[ebook_name] = img_files
    return sorted(ebooks.items(), key=lambda x: x[0])

def extract_archive(archive_path: Path, dest_dir: Path) -> bool:
    ext = archive_path.suffix.lower()
    try:
        if ext in ('.zip', '.cbz'):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(dest_dir)
        elif ext in ('.7z',):
            with py7zr.SevenZipFile(archive_path, mode='r') as sz:
                sz.extractall(dest_dir)
        elif ext in ('.rar', '.cbr'):
            if getattr(sys, 'frozen', False):
                base_path = sys._MEIPASS
            else:
                base_path = Path.cwd()
            unrar_exe = Path(base_path) / "UnRAR.exe"
            if unrar_exe.exists():
                subprocess.run([str(unrar_exe), 'x', '-y', str(archive_path), str(dest_dir)], check=True)
            else:
                with rarfile.RarFile(archive_path) as rf:
                    rf.extractall(dest_dir)
        else:
            return False
        return True
    except Exception as e:
        logger.error(f"解压失败 {archive_path}: {e}")
        return False

def collect_images(extract_dir: Path) -> List[Path]:
    img_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp',
                '.tif', '.tiff', '.ico', '.icns', '.mpo', '.hdr'}
    images = []
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in img_exts:
                images.append(Path(root) / f)
    return natsorted(images)

def build_xtc_container(pages_data: List[bytes], title: str, author: str, width: int, height: int, is_hq: bool, page_dimensions: List[Tuple[int,int]] = None) -> bytes:
    page_count = len(pages_data)
    magic = b'XTCH' if is_hq else b'XTC\0'
    header_size = 56
    metadata_size = 256
    chapter_size = 96 * 0
    index_size = page_count * 16
    metadata_offset = header_size
    chapter_offset = metadata_offset + metadata_size
    index_offset = chapter_offset + chapter_size
    data_offset = index_offset + index_size
    metadata = bytearray(metadata_size)
    title_bytes = title.encode('utf-8')[:127] + b'\0'
    author_bytes = author.encode('utf-8')[:63] + b'\0'
    metadata[:len(title_bytes)] = title_bytes
    metadata[128:128+len(author_bytes)] = author_bytes
    struct.pack_into('<I', metadata, 192, int(time.time()))
    struct.pack_into('<H', metadata, 196, 0)
    index_table = bytearray(index_size)
    current_offset = data_offset
    for i, page_data in enumerate(pages_data):
        offset = current_offset
        size = len(page_data)
        if page_dimensions and i < len(page_dimensions):
            w, h = page_dimensions[i]
        else:
            w, h = width, height
        struct.pack_into('<Q', index_table, i*16, offset)
        struct.pack_into('<I', index_table, i*16+8, size)
        struct.pack_into('<H', index_table, i*16+12, w)
        struct.pack_into('<H', index_table, i*16+14, h)
        current_offset += size
    header = bytearray(header_size)
    header[:4] = magic
    struct.pack_into('<H', header, 4, 1)
    struct.pack_into('<H', header, 6, page_count)
    header[8] = 0
    header[9] = 1
    header[10] = 0
    header[11] = 0
    struct.pack_into('<I', header, 12, 1)
    struct.pack_into('<Q', header, 16, metadata_offset)
    struct.pack_into('<Q', header, 24, index_offset)
    struct.pack_into('<Q', header, 32, data_offset)
    struct.pack_into('<Q', header, 40, 0)
    struct.pack_into('<Q', header, 48, chapter_offset)
    return header + metadata + index_table + b''.join(pages_data)

def sanitize_filename(filename: str) -> str:
    invalid_chars = r'[<>:"/\\|?*]'
    cleaned = re.sub(invalid_chars, '', filename)
    if cleaned != filename:
        logger.info(f"文件名包含非法字符，已清理: {filename} -> {cleaned}")
    return cleaned

# ---------- 输出电子书函数 ----------
def create_epub(images: List[Image.Image], title: str, output_path: Path) -> bool:
    try:
        book = epub.EpubBook()
        book.set_identifier(title)
        book.set_title(title)
        book.set_language('zh')
        if images:
            cover_img = images[0].convert('RGB')
            cover_data = io.BytesIO()
            cover_img.save(cover_data, format='PNG')
            cover_data.seek(0)
            book.set_cover("cover.png", cover_data.read())
        chapters = []
        for idx, img in enumerate(images):
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img_data = io.BytesIO()
            img.save(img_data, format='PNG')
            img_data.seek(0)
            img_name = f"image_{idx+1:04d}.png"
            book.add_item(epub.EpubImage(
                uid=img_name,
                file_name=f"images/{img_name}",
                media_type="image/png",
                content=img_data.read()
            ))
            content = f'''<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Page {idx+1}</title></head>
<body>
<div style="text-align: center;">
<img src="../images/{img_name}" alt="page {idx+1}" style="max-width: 100%;"/>
</div>
</body>
</html>'''
            chap = epub.EpubHtml(
                title=f"Page {idx+1}",
                file_name=f"page_{idx+1:04d}.xhtml",
                lang='zh'
            )
            chap.content = content.encode('utf-8')
            book.add_item(chap)
            chapters.append(chap)
        if not chapters:
            logger.error("没有有效的页面内容，无法生成 EPUB")
            return False
        book.spine = chapters
        book.toc = []
        epub.write_epub(output_path, book, {})
        logger.info(f"成功生成 EPUB: {output_path}")
        return True
    except Exception as e:
        logger.error(f"生成 EPUB 失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

def create_pdf(images: List[Image.Image], title: str, output_path: Path) -> bool:
    try:
        image_bytes_list = []
        for img in images:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='JPEG', quality=95)
            img_bytes.seek(0)
            image_bytes_list.append(img_bytes.read())
        pdf_bytes = img2pdf.convert(image_bytes_list)
        output_path.write_bytes(pdf_bytes)
        logger.info(f"成功生成 PDF: {output_path}")
        return True
    except Exception as e:
        logger.error(f"生成 PDF 失败: {e}")
        return False

# ---------- 核心处理 ----------
def process_images_to_ebook(images: List[Image.Image], title: str, settings: dict, output_dir: Path,
                            progress_callback: Optional[Callable[[str, int, int], None]] = None) -> bool:
    total = len(images)
    logger.info(f"处理电子书 {title}，共 {total} 张图片")
    out_format = settings['out_value']
    safe_title = sanitize_filename(title)
    output_path = output_dir / f"{safe_title}.{out_format}"
    if out_format == 'epub':
        return create_epub(images, title, output_path)
    elif out_format == 'pdf':
        return create_pdf(images, title, output_path)
    else:
        logger.error(f"不支持的电子书格式: {out_format}")
        return False

def init_worker():
    logging.getLogger().handlers.clear()
    logging.getLogger().addHandler(logging.NullHandler())

def process_images(images: List[Image.Image], title: str, settings: dict, output_base_dir: Path,
                   progress_callback: Optional[Callable[[str, int, int], None]] = None) -> bool:
    gif_mode = settings.get('gif_mode', 1)
    new_images = []
    for img in images:
        is_animated = getattr(img, 'is_animated', False)
        n_frames = getattr(img, 'n_frames', 1)
        if is_animated and n_frames > 1:
            if gif_mode == 3:
                logger.info(f"跳过 GIF 动图: {title}")
                continue
            elif gif_mode == 2:
                frames = []
                try:
                    img.seek(0)
                    for frame_idx in range(n_frames):
                        img.seek(frame_idx)
                        frames.append(img.copy())
                except Exception as e:
                    logger.error(f"展开 GIF 帧失败: {e}")
                    frames = [img.copy()]
                new_images.extend(frames)
                continue
        new_images.append(img)
    images = new_images
    total = len(images)
    logger.info(f"处理电子书 {title}，共 {total} 张图片")
    max_workers = settings['max_workers']
    logger.info(f"使用 {max_workers} 个进程并行处理图片")

    out_type = settings.get('out_type')
    out_value = settings.get('out_value')
    if out_type == 'format' and out_value in ('xtch', 'xth'):
        bits = 2
    else:
        bits = 1
    settings['format'] = 'xth' if bits == 2 else 'xtg'
    is_1bit = (settings['format'] == 'xtg')

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        img_paths = []
        for idx, img in enumerate(images):
            ext = '.png'
            save_path = tmp_path / f"temp_{idx:05d}{ext}"
            img.save(save_path)
            img_paths.append(save_path)
        total = len(img_paths)
        pages_data_list = [None] * total
        failed_images = []
        args_list = [(img_path, idx, total, settings) for idx, img_path in enumerate(img_paths)]
        with ProcessPoolExecutor(max_workers=max_workers, initializer=init_worker) as executor:
            futures = [executor.submit(_process_single_image, args) for args in args_list]
            completed_count = 0
            for future in as_completed(futures):
                idx, enc_list, split_info, status, err_msg = future.result()
                completed_count += 1
                if status == 0:
                    pages_data_list[idx] = enc_list
                    if split_info:
                        for info in split_info:
                            logger.info(f"图片 {idx+1}/{total} {info}")
                else:
                    failed_images.append((idx, img_paths[idx], err_msg))
                if progress_callback:
                    progress_callback(title, completed_count, total)
        if failed_images:
            error_msg = f"处理失败，共 {len(failed_images)} 张图片失败，页码索引: {[idx for idx, _, _ in failed_images]}。转换终止。"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        pages_data = []
        for sublist in pages_data_list:
            if sublist is not None:
                pages_data.extend(sublist)
        if not pages_data:
            logger.error(f"没有成功处理任何图片: {title}")
            return False

        output_base_dir.mkdir(parents=True, exist_ok=True)

        if out_type == 'image':
            ext = out_value
            out_dir = output_base_dir / sanitize_filename(title)
            out_dir.mkdir(parents=True, exist_ok=True)
            filename_format = settings.get('filename_format', 0)
            for idx, page_data in enumerate(pages_data):
                if is_1bit:
                    img = XTCReader._decode_xtg(page_data)
                else:
                    img = XTCReader._decode_xth(page_data)
                if filename_format == 0:
                    filename = f"{idx+1:04d}.{ext}"
                else:
                    filename = f"{sanitize_filename(title)}-{idx+1:04d}.{ext}"
                save_path = out_dir / filename
                if ext == 'jpg':
                    if img.mode == 'L':
                        img = img.convert('RGB')
                    img.save(save_path, 'JPEG', quality=95)
                elif ext == 'png':
                    img.save(save_path, 'PNG')
                elif ext == 'webp':
                    img.save(save_path, 'WEBP')
                elif ext == 'bmp':
                    img.save(save_path, 'BMP')
                logger.info(f"输出图片: {save_path}")
            logger.info(f"成功输出 {len(pages_data)} 张图片到 {out_dir}")
            return True

        elif out_type == 'format' and out_value in ('xtc', 'xtch'):
            title_safe = sanitize_filename(title)
            author = "Unknown"
            is_hq = (out_value == 'xtch')
            ext = ".xtch" if is_hq else ".xtc"
            base_name = title_safe
            split_size = settings.get('split_size', 0)
            page_dimensions = []
            for page_data in pages_data:
                if len(page_data) >= 8:
                    w = struct.unpack('<H', page_data[4:6])[0]
                    h = struct.unpack('<H', page_data[6:8])[0]
                    page_dimensions.append((w, h))
                else:
                    page_dimensions.append((0, 0))
            parts = []
            current_part = []
            current_part_start_idx = 0
            current_size = 0
            for idx, page in enumerate(pages_data):
                page_size = len(page)
                if split_size > 0 and current_part and current_size + page_size > split_size:
                    container = build_xtc_container(current_part, base_name, author, 0, 0, is_hq, page_dimensions[current_part_start_idx:idx])
                    parts.append(container)
                    current_part = []
                    current_size = 0
                    current_part_start_idx = idx
                if not current_part:
                    current_part_start_idx = idx
                current_part.append(page)
                current_size += page_size
            if current_part:
                container = build_xtc_container(current_part, base_name, author, 0, 0, is_hq, page_dimensions[current_part_start_idx:])
                parts.append(container)
            if len(parts) == 1:
                output_path = output_base_dir / f"{base_name}{ext}"
                output_path.write_bytes(parts[0])
                logger.info(f"输出文件: {output_path} ({len(parts[0])} bytes)")
            else:
                for i, container in enumerate(parts, start=1):
                    filename = f"{base_name}-{i}{ext}"
                    filename = sanitize_filename(filename)
                    output_path = output_base_dir / filename
                    output_path.write_bytes(container)
                    logger.info(f"输出文件: {output_path} ({len(container)} bytes)")
            return True

        elif out_type == 'format' and out_value in ('xtg', 'xth'):
            out_dir = output_base_dir / sanitize_filename(title)
            out_dir.mkdir(parents=True, exist_ok=True)
            ext = ".xtg" if out_value == 'xtg' else ".xth"
            filename_format = settings.get('filename_format', 0)
            for idx, page_data in enumerate(pages_data, start=1):
                if filename_format == 0:
                    filename = f"{idx}{ext}"
                else:
                    filename = f"{sanitize_filename(title)}-{idx}{ext}"
                output_path = out_dir / filename
                output_path.write_bytes(page_data)
                logger.info(f"写入文件: {output_path} ({len(page_data)} bytes)")
            logger.info(f"成功输出 {len(pages_data)} 个单页文件到 {out_dir}")
            return True

        else:
            logger.error(f"未知输出类型: {out_type}/{out_value}")
            return False

def convert_items(items: List[InputItem], output_dir: Path, settings: dict,
                  overall_progress_callback: Optional[Callable[[str, int, int], None]] = None) -> int:
    total = len(items)
    success_count = 0
    text_choice_memory = None
    mixed_choice_memory = None
    for idx, item in enumerate(items):
        logger.info(f"[{idx+1}/{total}] 处理: {item.name}")
        if item.doc_type == 'comic':
            logger.info(f"本书为纯图片电子书")
            print(f"本书为纯图片电子书")
            images = item.get_images()
            if not images:
                logger.error(f"没有找到任何图像: {item.name}")
                continue
        elif item.doc_type == 'text':
            logger.info(f"本书为纯文本电子书，建议直接打开阅读，不建议转换！")
            print(f"本书为纯文本电子书，建议直接打开阅读，不建议转换！")
            if text_choice_memory is None:
                choice = input("是否需要转换成txt格式？(y/n) [默认 n]: ").strip().lower()
                text_choice_memory = (choice == 'y')
            if text_choice_memory:
                logger.info(f"用户选择转换为 TXT: {item.name}")
                txt = item.get_text()
                if txt:
                    txt_path = output_dir / f"{sanitize_filename(item.name)}.txt"
                    txt_path.write_text(txt, encoding='utf-8')
                    logger.info(f"已保存 TXT 文件: {txt_path}")
                    print(f"已保存 TXT 文件: {txt_path}")
                    success_count += 1
                else:
                    logger.error(f"提取文本失败: {item.name}")
                continue
            else:
                logger.info(f"用户选择跳过: {item.name}")
                continue
        else:
            logger.info(f"本书为图文混排电子书，本工具暂不支持转换，建议使用其他工具转换为PDF格式后再使用本工具转换！")
            print(f"本书为图文混排电子书，本工具暂不支持转换，建议使用其他工具转换为PDF格式后再使用本工具转换！")
            if mixed_choice_memory is None:
                print("请选择操作：")
                print("1. 不转换 (默认)")
                print("2. 仅转换图片")
                print("3. 仅转换文本")
                choice = input("请输入序号 [默认 1]: ").strip()
                if choice == '2':
                    mixed_choice_memory = 1
                elif choice == '3':
                    mixed_choice_memory = 2
                else:
                    mixed_choice_memory = 0
            if mixed_choice_memory == 1:
                logger.info(f"用户选择仅转换图片: {item.name}")
                images = item.get_images()
                if not images:
                    logger.error(f"没有找到任何图像: {item.name}")
                    continue
            elif mixed_choice_memory == 2:
                logger.info(f"用户选择仅转换文本: {item.name}")
                txt = item.get_text()
                if txt:
                    txt_path = output_dir / f"{sanitize_filename(item.name)}.txt"
                    txt_path.write_text(txt, encoding='utf-8')
                    logger.info(f"已保存 TXT 文件: {txt_path}")
                    print(f"已保存 TXT 文件: {txt_path}")
                    success_count += 1
                else:
                    logger.error(f"提取文本失败: {item.name}")
                continue
            else:
                logger.info(f"用户选择跳过: {item.name}")
                continue

        local_settings = settings.copy()
        if settings['res_type'] == 'original':
            local_settings['width'] = 0
            local_settings['height'] = 0
            local_settings['stretch'] = False
        else:
            w, h = settings['res_value']
            local_settings['width'] = w
            local_settings['height'] = h
        local_settings['out_type'] = settings['out_type']
        local_settings['out_value'] = settings['out_value']
        if settings['out_type'] == 'ebook':
            success = process_images_to_ebook(images, item.name, local_settings, output_dir,
                                              lambda name, cur, total: overall_progress_callback(name, cur, total) if overall_progress_callback else None)
        else:
            success = process_images(images, item.name, local_settings, output_dir,
                                     lambda name, cur, total: overall_progress_callback(name, cur, total) if overall_progress_callback else None)
        if success:
            success_count += 1
        else:
            logger.error(f"处理失败: {item.name}")
    return success_count

def main():
    print("="*50)
    print("Ebook2XTX v1.6 - 电子书转 XTC/XTCH/XTG/XTH/图片/电子书 格式")
    print("支持输出电子书：EPUB, PDF")
    print("="*50)
    settings = get_user_settings()
    root_dir = input("请输入要处理的目录路径 [默认当前目录]: ").strip()
    if not root_dir:
        root_dir = "."
    root_path = Path(root_dir).resolve()
    if not root_path.exists():
        print(f"目录不存在: {root_path}")
        return
    default_output = Path.cwd()
    output_dir_str = input(f"请输入要输出的目录路径 [默认 {default_output}]: ").strip()
    if not output_dir_str:
        output_dir = default_output
    else:
        output_dir = Path(output_dir_str).resolve()
        if not output_dir.exists():
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"输出目录不存在，已自动创建: {output_dir}")
            except Exception as e:
                print(f"无法创建输出目录: {e}")
                return
    print(f"输出目录: {output_dir}")
    items = scan_input_items(root_path)
    if not items:
        print("未找到任何可转换的内容")
        return
    print(f"找到 {len(items)} 个输入项")
    success = convert_items(items, output_dir, settings)
    logger.info(f"处理完成，成功 {success}/{len(items)}")
    print(f"处理完成，成功 {success}/{len(items)}，详细日志见 log 目录")
    input("按回车键退出...")

if __name__ == "__main__":
    freeze_support()
    import multiprocessing
    if multiprocessing.current_process().name == 'MainProcess':
        main()