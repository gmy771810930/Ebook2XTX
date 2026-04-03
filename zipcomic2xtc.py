#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
漫画压缩包转 XTC/XTCH/XTG/XTH 格式 (Xteink 设备)
优化版：多进程并行处理图片 + Numba 加速抖动 + 自动分包
新增单页输出模式：XTG、XTH，可自定义文件名格式
新增画面切割功能：横切2图/3图，可自定义比例，首尾页不切割，旋转规则符合用户要求
新增滚动切割模式：横切2图（3图滚动）、横切3图（5图滚动），可自定义重叠比例
新增输出目录选择：用户可自定义输出位置，默认当前目录
新增 GIF 动图处理：可选择只处理第一帧或处理所有帧
新增 WebP、TIFF、ICO、ICNS、MPO、HDR 格式支持
新增容器格式分包大小可自定义（支持单位 k/KB/m/MB/g/GB），默认大小调整为4GB（FAT32），并支持不分包
修改 支持进度回调，便于图形界面集成
修改 UnRAR.exe 路径在 PyInstaller 打包后也能正确找到
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
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any, Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support

# ========== 导入工作函数（从独立模块） ==========
from core import _process_single_image

# ========== 依赖管理 ==========
def check_and_install_dependencies():
    """检查并安装缺失的依赖，返回是否成功"""
    required = [
        ('Pillow', 'from PIL import Image'),
        ('py7zr', 'import py7zr'),
        ('rarfile', 'import rarfile'),
        ('natsort', 'from natsort import natsorted'),
        ('numpy', 'import numpy as np'),
        ('numba', 'from numba import njit')
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
            # 再次验证
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

# ========== 日志配置 ==========
def setup_logging():
    log_dir = Path.cwd() / "log"
    log_dir.mkdir(exist_ok=True)
    log_filename = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# 全局变量：GIF处理模式（None表示未询问，1=第一帧，2=所有帧）
_gif_mode = None

# ========== 用户交互 ==========
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
    """
    解析用户输入的大小字符串，返回字节数
    支持格式：数字（默认MB）、数字k/KB、数字m/MB、数字g/GB
    """
    size_str = size_str.strip()
    if not size_str:
        raise ValueError("输入不能为空")

    # 匹配数字和可选单位
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([kKmMgG]?[bB]?)$', size_str)
    if not match:
        raise ValueError("格式错误，请输入数字+可选单位（k/KB/m/MB/g/GB）")

    num = float(match.group(1))
    unit = match.group(2).lower()

    # 默认单位是 MB
    if unit == '' or unit == 'm' or unit == 'mb':
        return int(num * 1024 * 1024)
    elif unit == 'k' or unit == 'kb':
        return int(num * 1024)
    elif unit == 'g' or unit == 'gb':
        return int(num * 1024 * 1024 * 1024)
    else:
        raise ValueError(f"不支持的单位: {unit}")

def get_split_size() -> int:
    """
    询问用户分包大小选项，返回字节数（0 表示不分包）
    """
    print("\n请选择分包大小：")
    options = {
        1: "4G (FAT32 最大文件大小)",
        2: "自定义",
        3: "不分包"
    }
    choice = get_user_choice("", options, 1)

    if choice == 1:
        return 4 * 1024 * 1024 * 1024  # 4GB
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
    else:  # choice == 3
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

def get_filename_format() -> int:
    """获取文件名格式选择，返回 0: 编号，1: 漫画名-编号"""
    options = {1: "编号 (例如 1.xtg)", 2: "漫画名-编号 (例如 漫画名-1.xtg)"}
    choice = get_user_choice("请选择输出文件名格式：", options, 1)
    return 0 if choice == 1 else 1

def get_crop_settings():
    """获取画面切割设置，支持滚动切割模式"""
    print("\n画面切割选项：")
    crop_options = {
        1: "不切割",
        2: "横切2图",
        3: "横切3图"
    }
    crop_choice = get_user_choice("请选择切割方式：", crop_options, 1)

    if crop_choice == 1:
        return {'mode': 0, 'ratio': None}

    if crop_choice == 2:  # 横切2图
        sub_options = {
            1: "1 : 1.618 (黄金比例，上少下多)",
            2: "1.618 : 1 (上多下少)",
            3: "1 : 1 (上下等分)",
            4: "1 : 1 (3图滚动)"   # 新增滚动模式
        }
        sub_choice = get_user_choice("请选择切割方式：", sub_options, 1)
        if sub_choice == 1:
            ratio = (1, 1.618)
            return {'mode': 2, 'ratio': ratio}
        elif sub_choice == 2:
            ratio = (1.618, 1)
            return {'mode': 2, 'ratio': ratio}
        elif sub_choice == 3:
            ratio = (1, 1)
            return {'mode': 2, 'ratio': ratio}
        else:  # 3图滚动
            overlap_percent = get_int_input("请输入重叠比例 (0-100，默认 100):", 100)
            overlap_percent = max(0, min(100, overlap_percent))
            return {'mode': 4, 'overlap_percent': overlap_percent}

    else:  # 横切3图
        sub_options = {
            1: "1 : 2 : 1",
            2: "2 : 1 : 1",
            3: "1 : 1 : 2",
            4: "1 : 1 : 1",
            5: "1 : 1 : 1（5图滚动）"   # 新增滚动模式
        }
        sub_choice = get_user_choice("请选择切割方式：", sub_options, 1)
        if sub_choice == 1:
            ratio = (1, 2, 1)
            return {'mode': 3, 'ratio': ratio}
        elif sub_choice == 2:
            ratio = (2, 1, 1)
            return {'mode': 3, 'ratio': ratio}
        elif sub_choice == 3:
            ratio = (1, 1, 2)
            return {'mode': 3, 'ratio': ratio}
        elif sub_choice == 4:
            ratio = (1, 1, 1)
            return {'mode': 3, 'ratio': ratio}
        else:  # 5图滚动
            overlap_percent = get_int_input("请输入重叠比例 (0-100，默认 100):", 100)
            overlap_percent = max(0, min(100, overlap_percent))
            return {'mode': 5, 'overlap_percent': overlap_percent}

def get_user_settings():
    print("\n请选择转换设置：")
    # 格式
    format_options = {
        1: "XTC (1-bit 黑白，容器格式，单文件)",
        2: "XTCH (2-bit 4级灰度，容器格式，单文件)",
        3: "XTG (1-bit 黑白，单页模式，每个图片单独输出)",
        4: "XTH (2-bit 4级灰度，单页模式，每个图片单独输出)"
    }
    fmt_choice = get_user_choice("请选择输出格式：", format_options, 1)
    if fmt_choice == 1:
        output_format = "xtc"
    elif fmt_choice == 2:
        output_format = "xtch"
    elif fmt_choice == 3:
        output_format = "xtg"
    else:
        output_format = "xth"

    # 分辨率
    res_options = {
        1: "X4 (480×800)",
        2: "X4 双倍分辨率 (960×1600)",
        3: "X3 (528×792)",
        4: "X3 双倍分辨率 (1056×1584)",
        5: "自定义分辨率"
    }
    res_choice = get_user_choice("请选择目标分辨率：", res_options, 1)
    if res_choice == 1:
        width, height = 480, 800
    elif res_choice == 2:
        width, height = 960, 1600
    elif res_choice == 3:
        width, height = 528, 792
    elif res_choice == 4:
        width, height = 1056, 1584
    else:
        width, height = get_resolution_custom()

    # 裁切
    crop_options = {1: "是", 2: "否"}
    crop_choice = get_user_choice("是否自动裁切黑白边？", crop_options, 1)
    auto_crop = (crop_choice == 1)

    # 旋转（横版图片旋转方式）
    rotate_options = {
        1: "顺时针90度",
        2: "逆时针90度",
        3: "不旋转"
    }
    rotate_choice = get_user_choice("横版图片旋转方式：", rotate_options, 1)
    rotate_mode = None
    if rotate_choice == 1:
        rotate_mode = "clockwise"
    elif rotate_choice == 2:
        rotate_mode = "counterclockwise"
    else:
        rotate_mode = "none"

    # 画面切割
    crop_cfg = get_crop_settings()

    # 拉伸
    stretch_options = {1: "是 (拉伸至全屏)", 2: "否 (保持比例，填充黑边)"}
    stretch_choice = get_user_choice("是否拉伸图片至全屏？", stretch_options, 1)
    stretch = (stretch_choice == 1)

    # 抖动强度
    dither_strength = get_float_input("请输入抖动强度 (0-1 之间):", 0.7)

    # 并发数
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

    # 文件名格式（仅在单页模式下需要）
    filename_format = None
    if output_format in ("xtg", "xth"):
        filename_format = get_filename_format()

    # 如果是容器格式，询问分包大小
    split_size = None
    if output_format in ("xtc", "xtch"):
        split_size = get_split_size()

    return {
        'format': output_format,
        'width': width,
        'height': height,
        'auto_crop': auto_crop,
        'rotate_mode': rotate_mode,
        'crop': crop_cfg,
        'stretch': stretch,
        'dither_strength': dither_strength,
        'max_workers': max_workers,
        'filename_format': filename_format,
        'split_size': split_size
    }

# ========== 压缩包处理 ==========
def find_archives(root_dir: str) -> List[Path]:
    archive_exts = {'.zip', '.cbz', '.7z', '.cbr', '.rar'}
    archives = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in archive_exts:
                archives.append(Path(dirpath) / f)
    return archives

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
            # 判断是否在 PyInstaller 打包环境中
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
            logger.error(f"不支持的压缩包格式: {archive_path}")
            return False
        return True
    except Exception as e:
        logger.error(f"解压失败 {archive_path}: {e}")
        return False

def collect_images(extract_dir: Path) -> List[Path]:
    # 支持的图片格式
    img_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp',
                '.tif', '.tiff', '.ico', '.icns', '.mpo', '.hdr'}
    images = []
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in img_exts:
                images.append(Path(root) / f)
    return natsorted(images)

# ========== 容器构建 ==========
def build_xtc_container(pages_data: List[bytes], title: str, author: str, width: int, height: int, is_hq: bool) -> bytes:
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
        struct.pack_into('<Q', index_table, i*16, offset)
        struct.pack_into('<I', index_table, i*16+8, size)
        struct.pack_into('<H', index_table, i*16+12, width)
        struct.pack_into('<H', index_table, i*16+14, height)
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
    """移除 Windows 不支持的字符"""
    invalid_chars = r'[<>:"/\\|?*]'
    cleaned = re.sub(invalid_chars, '', filename)
    if cleaned != filename:
        logger.info(f"文件名包含非法字符，已清理: {filename} -> {cleaned}")
    return cleaned

# ========== 主转换流程（支持进度回调） ==========
def process_archive(archive_path: Path, settings: dict, output_base_dir: Path,
                    progress_callback: Optional[Callable[[str, int, int], None]] = None) -> bool:
    """
    处理单个压缩包
    :param archive_path: 压缩包路径
    :param settings: 转换设置
    :param output_base_dir: 输出基础目录（用户指定的输出位置）
    :param progress_callback: 进度回调函数，参数：(archive_name, current_image_index, total_images)
    """
    logger.info(f"开始处理: {archive_path}")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        if not extract_archive(archive_path, tmp_path):
            logger.error(f"解压失败: {archive_path}")
            return False
        images = collect_images(tmp_path)
        if not images:
            logger.error(f"未找到图片文件: {archive_path}")
            return False
        total = len(images)
        logger.info(f"找到 {total} 张图片")

        # 检查是否有GIF文件，若未设置GIF模式则询问用户
        global _gif_mode
        has_gif = any(img.suffix.lower() == '.gif' for img in images)
        if has_gif and _gif_mode is None:
            print("\n检测到压缩包中包含GIF动图文件。")
            gif_options = {
                1: "只处理第一帧（默认）",
                2: "处理所有帧（将每一帧作为独立图片输出）"
            }
            gif_choice = get_user_choice("请选择GIF处理方式：", gif_options, 1)
            _gif_mode = 1 if gif_choice == 1 else 2
            logger.info(f"GIF处理模式已选择: {'只处理第一帧' if _gif_mode == 1 else '处理所有帧'}")

        # 将GIF模式添加到settings中，以便子进程使用
        settings['gif_mode'] = _gif_mode

        max_workers = settings['max_workers']
        logger.info(f"使用 {max_workers} 个进程并行处理图片")
        pages_data_list = [None] * total  # 每个元素是列表
        failed_images = []
        args_list = [(img_path, idx, total, settings) for idx, img_path in enumerate(images)]

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_process_single_image, args) for args in args_list]
            completed_count = 0
            for future in as_completed(futures):
                idx, enc_list, split_info, status, err_msg = future.result()
                completed_count += 1
                if status == 0:
                    pages_data_list[idx] = enc_list
                    # 输出切割日志（仅日志文件）
                    if split_info:
                        for info in split_info:
                            logger.info(f"图片 {idx+1}/{total} {info}")
                else:
                    failed_images.append((idx, images[idx], err_msg))
                # 报告进度
                if progress_callback:
                    progress_callback(archive_path.name, completed_count, total)

        if failed_images:
            logger.error(f"共有 {len(failed_images)} 张图片处理失败:")
            for idx, img, err in failed_images:
                logger.error(f"  #{idx+1}: {img} - {err}")

        # 展平页面列表
        pages_data = []
        for sublist in pages_data_list:
            if sublist is not None:
                pages_data.extend(sublist)
        if not pages_data:
            logger.error(f"没有成功处理任何图片: {archive_path}")
            return False

        output_format = settings['format']
        # 确保输出目录存在
        output_base_dir.mkdir(parents=True, exist_ok=True)

        # 容器格式（XTC/XTCH）
        if output_format in ('xtc', 'xtch'):
            # 分包处理
            title = sanitize_filename(archive_path.stem)
            author = "Unknown"
            is_hq = (output_format == 'xtch')
            ext = ".xtch" if is_hq else ".xtc"
            base_name = sanitize_filename(archive_path.stem)

            split_size = settings.get('split_size', 0)

            parts = []
            current_part = []
            current_size = 0
            for page in pages_data:
                page_size = len(page)
                # 只有当 split_size > 0 且当前部分已累积且加上新页面会超过大小时才分包
                if split_size > 0 and current_part and current_size + page_size > split_size:
                    # 当前部分达到阈值，保存
                    container = build_xtc_container(current_part, title, author, settings['width'], settings['height'], is_hq)
                    parts.append(container)
                    current_part = []
                    current_size = 0
                current_part.append(page)
                current_size += page_size

            # 最后一部分
            if current_part:
                container = build_xtc_container(current_part, title, author, settings['width'], settings['height'], is_hq)
                parts.append(container)

            # 写入文件（输出到用户指定目录）
            if len(parts) == 1:
                output_path = output_base_dir / f"{base_name}{ext}"
                output_path.write_bytes(parts[0])
                logger.info(f"输出文件: {output_path} ({len(parts[0])} bytes)")
            else:
                for i, container in enumerate(parts, start=1):
                    output_path = output_base_dir / f"{base_name}-{i}{ext}"
                    output_path.write_bytes(container)
                    logger.info(f"输出文件: {output_path} ({len(container)} bytes)")

        # 单页格式（XTG/XTH）
        else:  # xtg or xth
            # 在输出目录下创建以压缩包名命名的文件夹
            folder_name = sanitize_filename(archive_path.stem)
            output_folder = output_base_dir / folder_name
            output_folder.mkdir(parents=True, exist_ok=True)
            logger.info(f"创建输出目录: {output_folder}")

            ext = ".xtg" if output_format == 'xtg' else ".xth"
            filename_format = settings['filename_format']  # 0:编号, 1:漫画名-编号

            for idx, page_data in enumerate(pages_data, start=1):
                if filename_format == 0:
                    filename = f"{idx}{ext}"
                else:
                    filename = f"{folder_name}-{idx}{ext}"
                output_path = output_folder / filename
                output_path.write_bytes(page_data)
                logger.info(f"写入文件: {output_path} ({len(page_data)} bytes)")

            logger.info(f"成功输出 {len(pages_data)} 个单页文件到 {output_folder}")

        return True

def convert_archives(root_dir: Path, output_dir: Path, settings: dict,
                     overall_progress_callback: Optional[Callable[[str, int, int], None]] = None) -> int:
    """
    转换所有压缩包
    :param root_dir: 搜索压缩包的根目录
    :param output_dir: 输出目录
    :param settings: 转换设置
    :param overall_progress_callback: 总体进度回调，参数：(archive_name, current_image, total_images)
    :return: 成功处理的压缩包数量
    """
    archives = find_archives(root_dir)
    total_archives = len(archives)
    success_count = 0
    for idx, arc in enumerate(archives):
        logger.info(f"[{idx+1}/{total_archives}] {arc}")
        try:
            def archive_progress(name, current, total):
                if overall_progress_callback:
                    overall_progress_callback(name, current, total)
            if process_archive(arc, settings, output_dir, archive_progress):
                success_count += 1
            else:
                logger.error(f"处理失败: {arc}")
        except Exception as e:
            logger.exception(f"处理 {arc} 时发生异常: {e}")
    return success_count

def main():
    print("="*50)
    print("漫画压缩包转 XTC/XTCH/XTG/XTH 格式 (Xteink 设备) - 多进程优化版")
    print("="*50)
    settings = get_user_settings()

    # 获取输入目录
    root_dir = input("请输入要处理的目录路径 [默认当前目录]: ").strip()
    if not root_dir:
        root_dir = "."
    root_path = Path(root_dir).resolve()
    if not root_path.exists():
        print(f"目录不存在: {root_path}")
        return

    # 获取输出目录
    default_output = Path.cwd()
    output_dir_str = input(f"请输入要输出的目录路径 [默认 {default_output}]: ").strip()
    if not output_dir_str:
        output_dir = default_output
    else:
        output_dir = Path(output_dir_str).resolve()
        # 如果输出目录不存在，询问是否创建
        if not output_dir.exists():
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"输出目录不存在，已自动创建: {output_dir}")
            except Exception as e:
                print(f"无法创建输出目录: {e}")
                return
    print(f"输出目录: {output_dir}")

    # 调用 convert_archives 进行转换
    success = convert_archives(root_path, output_dir, settings)

    logger.info(f"处理完成，成功 {success}/{len(find_archives(root_path))}")
    print(f"处理完成，成功 {success}/{len(find_archives(root_path))}，详细日志见 log 目录")
    input("按回车键退出...")

if __name__ == "__main__":
    freeze_support()
    import multiprocessing
    if multiprocessing.current_process().name == 'MainProcess':
        main()
