"""按量表 bbox + padding 裁出单量表小图(软裁剪，留余量)。"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from .config_schema import ScaleMeta


def crop_scale(img: Image.Image, meta: ScaleMeta, padding_pct: float = 0.03
               ) -> bytes | None:
    """返回裁剪后的 PNG 字节。无 bbox 时返回整页。"""
    if not meta.bbox_nominal:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    w, h = img.size
    x1, y1, x2, y2 = meta.bbox_nominal
    pad = padding_pct
    box = (max(0, (x1 - pad) * w), max(0, (y1 - pad) * h),
           min(w, (x2 + pad) * w), min(h, (y2 + pad) * h))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    crop = img.crop((int(box[0]), int(box[1]), int(box[2]), int(box[3])))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()
