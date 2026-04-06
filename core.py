# core.py
import os
import sys
import time
import struct
import logging
from pathlib import Path
from typing import List, Tuple
from PIL import Image, ImageEnhance, ImageFilter
import numpy as np
from numba import njit

# ========== 日志 ==========
logger = logging.getLogger(__name__)

# ========== Numba 抖动 (Floyd-Steinberg) ==========
@njit(cache=True, fastmath=True)
def floyd_steinberg_dither_numba(gray_arr: np.ndarray, bits: int, strength: float) -> np.ndarray:
    h, w = gray_arr.shape
    img = gray_arr.astype(np.float32)
    levels = 2 ** bits
    step = 255.0 / (levels - 1)
    for y in range(h):
        for x in range(w):
            old = img[y, x]
            # 量化
            new = round(old / step) * step
            new = max(0, min(255, new))
            img[y, x] = new
            err = (old - new) * strength
            if x + 1 < w:
                img[y, x+1] += err * 7 / 16
            if y + 1 < h:
                if x > 0:
                    img[y+1, x-1] += err * 3 / 16
                img[y+1, x] += err * 5 / 16
                if x + 1 < w:
                    img[y+1, x+1] += err * 1 / 16
    return np.clip(img, 0, 255).astype(np.uint8)

# ========== Atkinson 抖动 (Numba 加速) ==========
@njit(cache=True, fastmath=True)
def atkinson_dither_numba(gray_arr: np.ndarray, bits: int, strength: float) -> np.ndarray:
    """
    Atkinson 抖动，strength 控制误差扩散强度。
    """
    h, w = gray_arr.shape
    img = gray_arr.astype(np.float32)
    levels = 2 ** bits
    step = 255.0 / (levels - 1)
    for y in range(h):
        for x in range(w):
            old = img[y, x]
            new = round(old / step) * step
            new = max(0, min(255, new))
            img[y, x] = new
            err = (old - new) * strength / 8.0  # Atkinson 误差除以8
            if x + 1 < w:
                img[y, x+1] += err
            if x + 2 < w:
                img[y, x+2] += err
            if y + 1 < h:
                if x - 1 >= 0:
                    img[y+1, x-1] += err
                img[y+1, x] += err
                if x + 1 < w:
                    img[y+1, x+1] += err
            if y + 2 < h:
                img[y+2, x] += err
    return np.clip(img, 0, 255).astype(np.uint8)

# ========== 无抖动（直接量化） ==========
def no_dither_quantize(gray_arr: np.ndarray, bits: int) -> np.ndarray:
    levels = 2 ** bits
    step = 255.0 / (levels - 1)
    quantized = np.round(gray_arr / step) * step
    return np.clip(quantized, 0, 255).astype(np.uint8)

# ========== 透明处理 ==========
def fill_transparent_with_white(img: Image.Image) -> Image.Image:
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    background = Image.new('RGBA', img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(background, img)
    img = img.convert('RGB')
    return img

# ========== 图像处理 ==========
def crop_white_black_borders(img: Image.Image, threshold: int = 10) -> Image.Image:
    gray = img.convert('L')
    arr = np.array(gray)
    h, w = arr.shape
    top = 0
    while top < h and (np.all(arr[top] <= threshold) or np.all(arr[top] >= 255 - threshold)):
        top += 1
    bottom = h - 1
    while bottom >= top and (np.all(arr[bottom] <= threshold) or np.all(arr[bottom] >= 255 - threshold)):
        bottom -= 1
    left = 0
    while left < w and (np.all(arr[:, left] <= threshold) or np.all(arr[:, left] >= 255 - threshold)):
        left += 1
    right = w - 1
    while right >= left and (np.all(arr[:, right] <= threshold) or np.all(arr[:, right] >= 255 - threshold)):
        right -= 1
    if top < bottom and left < right:
        return img.crop((left, top, right + 1, bottom + 1))
    else:
        return img

def rotate_image(img: Image.Image, mode: str) -> Image.Image:
    if mode == "none":
        return img
    elif mode == "clockwise":
        return img.rotate(-90, expand=True)
    elif mode == "counterclockwise":
        return img.rotate(90, expand=True)
    else:
        return img

def resize_to_target(img: Image.Image, target_width: int, target_height: int, stretch: bool) -> Image.Image:
    if target_width <= 0 or target_height <= 0:
        return img
    if stretch:
        return img.resize((target_width, target_height), Image.Resampling.LANCZOS)
    else:
        img.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
        new_img = Image.new("L", (target_width, target_height), color=255)
        offset_x = (target_width - img.width) // 2
        offset_y = (target_height - img.height) // 2
        new_img.paste(img, (offset_x, offset_y))
        return new_img

def split_image_vertically(img: Image.Image, parts: int, ratios: Tuple[float, ...]) -> List[Image.Image]:
    total_ratio = sum(ratios)
    img_height = img.height
    splits = []
    y_start = 0
    for r in ratios:
        height = int(img_height * r / total_ratio)
        if height <= 0:
            height = 1
        splits.append(img.crop((0, y_start, img.width, y_start + height)))
        y_start += height
    if y_start < img_height:
        last = splits[-1]
        last = img.crop((0, y_start - last.height, img.width, img_height))
        splits[-1] = last
    return splits

def split_rolling_2(img: Image.Image, overlap_percent: int) -> List[Image.Image]:
    H = img.height
    if H < 50:
        logger.warning(f"图片高度 {H} 过小，无法进行滚动切割，将使用原图")
        return [img]
    t = overlap_percent / 100.0
    h_max = H / 2.0
    d_max = H / 4.0
    h_min = (H + 2) / 3.0
    d_min = 1.0
    h = h_min + t * (h_max - h_min)
    d = d_min + t * (d_max - d_min)
    y1_start = 0
    y1_end = h
    y2_start = h - d
    y2_end = y2_start + h
    y3_start = 2 * h - 2 * d
    y3_end = H
    y1_start = int(round(y1_start))
    y1_end = int(round(y1_end))
    y2_start = int(round(y2_start))
    y2_end = int(round(y2_end))
    y3_start = int(round(y3_start))
    y3_end = int(round(y3_end))
    y1_end = max(y1_start + 1, y1_end)
    y2_end = max(y2_start + 1, y2_end)
    y3_end = max(y3_start + 1, y3_end)
    y1_end = min(y1_end, H)
    y2_end = min(y2_end, H)
    y3_end = min(y3_end, H)
    y2_start = max(y2_start, 0)
    y3_start = max(y3_start, 0)
    logger.info(f"滚动切割(2图滚动) 重叠比例 {overlap_percent}%: 图1[{y1_start}-{y1_end}], 图2[{y2_start}-{y2_end}], 图3[{y3_start}-{y3_end}]")
    splits = [
        img.crop((0, y1_start, img.width, y1_end)),
        img.crop((0, y2_start, img.width, y2_end)),
        img.crop((0, y3_start, img.width, y3_end))
    ]
    return splits

def split_rolling_3(img: Image.Image, overlap_percent: int) -> List[Image.Image]:
    H = img.height
    if H < 50:
        logger.warning(f"图片高度 {H} 过小，无法进行滚动切割，将使用原图")
        return [img]
    t = overlap_percent / 100.0
    h_max = H / 3.0
    d_max = H / 6.0
    h_min = (H + 4) / 5.0
    d_min = 1.0
    h = h_min + t * (h_max - h_min)
    d = d_min + t * (d_max - d_min)
    y_start = [0, h - d, 2*h - 2*d, 3*h - 3*d, 4*h - 4*d]
    y_end = [h, 2*h - d, 3*h - 2*d, 4*h - 3*d, H]
    y_start = [int(round(s)) for s in y_start]
    y_end = [int(round(e)) for e in y_end]
    for i in range(5):
        y_start[i] = max(0, y_start[i])
        y_end[i] = min(H, y_end[i])
        if y_end[i] <= y_start[i]:
            y_end[i] = y_start[i] + 1
    logger.info(f"滚动切割(3图滚动) 重叠比例 {overlap_percent}%: 图1[{y_start[0]}-{y_end[0]}], 图2[{y_start[1]}-{y_end[1]}], 图3[{y_start[2]}-{y_end[2]}], 图4[{y_start[3]}-{y_end[3]}], 图5[{y_start[4]}-{y_end[4]}]")
    splits = [img.crop((0, y_start[i], img.width, y_end[i])) for i in range(5)]
    return splits

# ========== 图像增强函数 ==========
def apply_sharpen(img: Image.Image, strength_pct: int) -> Image.Image:
    """锐化，strength_pct 0-100，映射到 UnsharpMask 的 percent 0-200%"""
    if strength_pct <= 0:
        return img
    percent = int(strength_pct * 2)  # 0-100 -> 0-200
    return img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=percent, threshold=2))

def apply_contrast(img: Image.Image, strength_pct: int) -> Image.Image:
    """对比度调整，strength_pct 0-100 -> factor 1.0 - 2.0"""
    if strength_pct == 0:
        return img
    factor = 1.0 + strength_pct / 100.0  # 0->1.0, 100->2.0
    enhancer = ImageEnhance.Contrast(img)
    return enhancer.enhance(factor)

def apply_clahe(img: Image.Image, strength_pct: int) -> Image.Image:
    """局部对比度增强 (CLAHE)，需要 opencv，strength_pct 0-100 -> clipLimit 0.5-4.0"""
    if strength_pct <= 0:
        return img
    try:
        import cv2
        clip_limit = max(0.5, strength_pct / 100.0 * 4.0)  # 0-100 -> 0.5-4.0
        arr = np.array(img.convert('L'))
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
        arr = clahe.apply(arr)
        return Image.fromarray(arr).convert(img.mode)
    except ImportError:
        logger.warning("OpenCV not installed, CLAHE disabled")
        return img

# ========== 编码 ==========
def encode_xtg(image: Image.Image) -> bytes:
    width, height = image.size
    gray = np.array(image.convert('L'), dtype=np.uint8)
    binary = (gray >= 128).astype(np.uint8)
    row_bytes = (width + 7) // 8
    data = bytearray(row_bytes * height)
    for y in range(height):
        for x in range(width):
            if binary[y, x]:
                byte_idx = y * row_bytes + (x // 8)
                bit_idx = 7 - (x % 8)
                data[byte_idx] |= (1 << bit_idx)
    header = struct.pack('<4sHHBBi8s', b'XTG\0', width, height, 0, 0, len(data), b'\0'*8)
    return header + data

def encode_xth(image: Image.Image) -> bytes:
    width, height = image.size
    gray = np.array(image.convert('L'), dtype=np.uint8)
    quant = np.zeros((height, width), dtype=np.uint8)
    quant[gray > 212] = 0
    quant[(gray > 127) & (gray <= 212)] = 2
    quant[(gray > 42) & (gray <= 127)] = 1
    quant[gray <= 42] = 3
    col_bytes = (height + 7) // 8
    plane0 = bytearray(col_bytes * width)
    plane1 = bytearray(col_bytes * width)
    for x in range(width-1, -1, -1):
        col_idx = width - 1 - x
        for y in range(height):
            val = quant[y, x]
            bit0 = (val >> 0) & 1
            bit1 = (val >> 1) & 1
            byte_idx = col_idx * col_bytes + (y // 8)
            bit_pos = 7 - (y % 8)
            if bit0:
                plane0[byte_idx] |= (1 << bit_pos)
            if bit1:
                plane1[byte_idx] |= (1 << bit_pos)
    header = struct.pack('<4sHHBBi8s', b'XTH\0', width, height, 0, 0, len(plane0) + len(plane1), b'\0'*8)
    return header + plane0 + plane1

# ========== 多进程工作函数 ==========
def _process_single_image(args):
    img_path, idx, total, settings = args
    try:
        img = Image.open(img_path)
        is_gif = img_path.suffix.lower() == '.gif'
        gif_mode = settings.get('gif_mode', 1)

        if is_gif and gif_mode == 2:
            all_encoded_pages = []
            split_info = []
            try:
                n_frames = img.n_frames
                logger.info(f"处理GIF动图 {img_path}，共 {n_frames} 帧，将处理所有帧")
                split_info.append(f"GIF动图，共 {n_frames} 帧，将处理所有帧")
            except AttributeError:
                logger.warning(f"无法获取GIF帧数，将只处理第一帧: {img_path}")
                gif_mode = 1

            if gif_mode == 2:
                for frame_idx in range(n_frames):
                    img.seek(frame_idx)
                    frame = img.copy()
                    processed = _process_single_frame(frame, idx, total, settings, frame_idx)
                    if processed is not None:
                        all_encoded_pages.extend(processed)
                return idx, all_encoded_pages, split_info, 0, None
            else:
                pass

        if is_gif and gif_mode == 1:
            img.seek(0)
        processed = _process_single_frame(img, idx, total, settings)
        if processed is None:
            return idx, None, [], 1, "处理失败"
        return idx, processed, [], 0, None
    except Exception as e:
        return idx, None, [], 1, str(e)

def _process_single_frame(img: Image.Image, idx: int, total: int, settings: dict, frame_idx: int = None) -> List[bytes]:
    try:
        # 透明背景处理
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            img = fill_transparent_with_white(img)
        img = img.convert('L')

        # 黑白边裁切
        if settings['auto_crop']:
            img = crop_white_black_borders(img)

        rotate_mode = settings['rotate_mode']
        crop_cfg = settings['crop']
        is_first = (idx == 0)
        is_last = (idx == total - 1)
        is_landscape = (img.width > img.height)

        splits = []

        if is_first or is_last:
            if is_landscape and rotate_mode != "none":
                img = rotate_image(img, rotate_mode)
            splits = [img]
        elif is_landscape:
            if rotate_mode != "none":
                img = rotate_image(img, rotate_mode)
            splits = [img]
        else:
            if crop_cfg['mode'] != 0:
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
                if rotate_mode != "none":
                    splits = [rotate_image(part, rotate_mode) for part in splits]
            else:
                splits = [img]

        encoded_pages = []
        for part_img in splits:
            target_w = settings['width']
            target_h = settings['height']
            if target_w > 0 and target_h > 0:
                part_img = resize_to_target(part_img, target_w, target_h, settings['stretch'])
            # 应用图像增强（锐化、对比度、CLAHE）-- 在灰度图上进行
            part_img = part_img.convert('L')
            sharpen = settings.get('sharpen', 0)
            if sharpen > 0:
                part_img = apply_sharpen(part_img, sharpen)
            contrast = settings.get('contrast', 0)
            if contrast > 0:
                part_img = apply_contrast(part_img, contrast)
            clahe = settings.get('clahe', 0)
            if clahe > 0:
                part_img = apply_clahe(part_img, clahe)

            gray_arr = np.array(part_img, dtype=np.float32)
            bits = settings.get('output_bits', 1)  # 目标位深度
            dither_algo = settings.get('dither_algo', 'Floyd-Steinberg')
            dither_strength = settings.get('dither_strength', 70) / 100.0  # 0-100 -> 0-1

            if dither_algo == 'Floyd-Steinberg':
                dithered = floyd_steinberg_dither_numba(gray_arr, bits, dither_strength)
            elif dither_algo == 'Atkinson':
                dithered = atkinson_dither_numba(gray_arr, bits, dither_strength)
            else:  # None
                dithered = no_dither_quantize(gray_arr, bits)

            dithered_img = Image.fromarray(dithered, mode='L')
            if settings['format'] in ('xtc', 'xtg'):
                page_enc = encode_xtg(dithered_img)
            else:
                page_enc = encode_xth(dithered_img)
            encoded_pages.append(page_enc)

        return encoded_pages
    except Exception as e:
        logger.error(f"处理帧时出错: {e}")
        return None