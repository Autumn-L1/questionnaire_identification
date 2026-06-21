"""图像预处理：中缝/边缘黑边清理、可选 deskew、统一长边。

本批样本实测无装订黑边，但保留该能力以应对扫描质量更差的样本(稳健性)。
返回 ``PreprocessReport`` 记录诊断信息(黑边宽度、倾斜角)，供 logger 判断是否告警。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .config_schema import PreprocessConfig

log = logging.getLogger("qr.pre")


@dataclass
class PreprocessReport:
    gutter_removed_px: int = 0      # 本次清理的黑边像素(单侧)
    gutter_too_wide: bool = False   # 黑边超过阈值(告警)
    deskew_angle: float = 0.0       # 校正角度(度)
    deskew_too_large: bool = False  # 倾斜过大(告警)
    out_size: tuple[int, int] = (0, 0)


def _detect_dark_band(arr: np.ndarray, side: str, dark_thresh: float = 80.0
                      ) -> int:
    """从某侧边缘数连续暗列(列均亮度<dark_thresh)的宽度。side: 'L'|'R'。"""
    col_mean = arr.mean(axis=0)
    dark = col_mean < dark_thresh
    n = 0
    seq = dark if side == "L" else dark[::-1]
    for v in seq:
        if v:
            n += 1
        else:
            break
    return n


def remove_edge_gutter(img: Image.Image, cfg: PreprocessConfig
                       ) -> tuple[Image.Image, PreprocessReport]:
    """裁掉左右两侧的连续暗列(装订黑边)。对竖向单列页同样适用。"""
    rep = PreprocessReport()
    arr = np.asarray(img.convert("L"), dtype=float)
    w = arr.shape[1]
    left = _detect_dark_band(arr, "L")
    right = _detect_dark_band(arr, "R")
    max_side = max(left, right)
    rep.gutter_removed_px = max_side
    if max_side > cfg.gutter_max_width_pct * w:
        rep.gutter_too_wide = True
        log.warning("检测到黑边宽度 %dpx 超过阈值 %.0fpx(页宽%.1f%%)，将裁剪并告警",
                    max_side, cfg.gutter_max_width_pct * w,
                    cfg.gutter_max_width_pct * 100)
    if max_side > 0:
        # 多裁 2px 留白边，避免残影
        crop_l = min(left + 2, w // 4)
        crop_r = max(0, w - right - 2)
        if crop_r > crop_l:
            img = img.crop((crop_l, 0, crop_r, img.height))
    return img, rep


def _estimate_skew(arr: np.ndarray, max_angle: float) -> float:
    """用水平投影方差最大化估计倾斜角(度)，范围 ±max_angle。"""
    h, w = arr.shape
    if min(h, w) < 64:
        return 0.0
    # 缩小加速
    scale = min(1.0, 800 / max(h, w))
    small = arr[::max(1, int(1 / scale)), ::max(1, int(1 / scale))]
    binary = (small < 128).astype(np.float32)
    best_angle, best_var = 0.0, -1.0
    for angle in np.arange(-max_angle, max_angle + 0.1, 0.5):
        rad = np.deg2rad(angle)
        sh = np.tan(rad)
        # 简单水平 shear：按行平移
        hh, ww = binary.shape
        rows, cols = np.indices(binary.shape)
        new_cols = cols + (rows - hh / 2) * sh
        valid = (new_cols >= 0) & (new_cols < ww)
        rc = rows[valid].astype(int)
        nc = np.clip(new_cols[valid].astype(int), 0, ww - 1)
        warped = np.zeros_like(binary)
        warped[rc, nc] = binary[valid]
        proj_var = warped.sum(axis=1).var()
        if proj_var > best_var:
            best_var, best_angle = proj_var, angle
    return float(best_angle)


def auto_deskew(img: Image.Image, cfg: PreprocessConfig,
                report: PreprocessReport) -> Image.Image:
    if not cfg.deskew_enabled:
        return img
    arr = np.asarray(img.convert("L"), dtype=float)
    angle = _estimate_skew(arr, cfg.deskew_max_angle)
    report.deskew_angle = angle
    if abs(angle) > cfg.deskew_max_angle * 0.9:
        report.deskew_too_large = True
        log.warning("倾斜角 %.1f° 接近/超过上限 %.1f°，告警", angle, cfg.deskew_max_angle)
    if abs(angle) < 0.3:
        return img
    # 用 expand 旋转(整体正向旋转矫正)
    return img.rotate(angle, resample=Image.BICUBIC, fillcolor="white")


def normalize_size(img: Image.Image, long_edge: int) -> Image.Image:
    w, h = img.size
    scale = min(1.0, long_edge / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
    return img


def preprocess_page(img: Image.Image, cfg: PreprocessConfig) -> tuple[Image.Image, PreprocessReport]:
    """对单页依次：去黑边 → deskew → 统一长边。返回处理后图像与诊断。"""
    img, rep = remove_edge_gutter(img, cfg)
    img = auto_deskew(img, cfg, rep)
    img = normalize_size(img, cfg.normalize_long_edge)
    rep.out_size = img.size
    return img, rep


def preprocess_dir(in_dir, out_dir, cfg: PreprocessConfig) -> list[PreprocessReport]:
    """批量预处理一个 pages 目录。in_dir/out_dir 可为 Path/str。"""
    from pathlib import Path
    in_dir, out_dir = Path(in_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reps: list[PreprocessReport] = []
    for p in sorted(in_dir.glob("src_*.png")):
        img = Image.open(p).convert("RGB")
        out, rep = preprocess_page(img, cfg)
        out.save(out_dir / p.name, format="PNG")
        reps.append(rep)
    return reps
