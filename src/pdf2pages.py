"""PDF → 源问卷 20 页 PNG。

按 config.pdf.page_map：每个源页来自某个 PDF 页的「整页(s)」或「左半(L)/右半(R)」。
横向页(842×595)沿纵向中线左右等分；竖向页(s)整页输出。

源 PDF 只读，产物写入 work/pages/src_01.png .. src_20.png。
"""
from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from .config_schema import AppConfig

log = logging.getLogger("qr.pdf")


def render_pdf_pages(pdf_path: Path, dpi: int) -> list[Image.Image]:
    """把 PDF 每页渲染成 PIL 图像(按 DPI)。"""
    doc = fitz.open(str(pdf_path))
    try:
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pages: list[Image.Image] = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            mode = "RGB" if pix.n < 4 else "RGBA"
            img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
            if img.mode != "RGB":
                img = img.convert("RGB")
            pages.append(img)
    finally:
        doc.close()
    return pages


def _take_half(img: Image.Image, side: str) -> Image.Image:
    """横向页取左/右半。side='L' 返回左半，'R' 返回右半。"""
    w, h = img.size
    mid = w // 2
    box = (0, 0, mid, h) if side == "L" else (mid, 0, w, h)
    return img.crop(box)


def check_pdf_shape(pdf_path: Path) -> tuple[int, list[str]]:
    """返回 (页数, 朝向列表如 ['竖','横',...])，供 run 校验 11 页/朝向合规。"""
    doc = fitz.open(str(pdf_path))
    try:
        orients: list[str] = []
        for page in doc:
            orients.append("竖" if page.rect.height > page.rect.width else "横")
        return len(doc), orients
    finally:
        doc.close()


def pdf_to_src_pages(pdf_path: str | Path, out_dir: str | Path,
                     cfg: AppConfig) -> list[Path]:
    """按 page_map 把 PDF 拆成 20 个源页 PNG，返回按 src 编号排序的路径列表。"""
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    n_pages, orients = check_pdf_shape(pdf_path)
    if n_pages != 11:
        raise ValueError(f"PDF 页数应为 11，实际 {n_pages}: {pdf_path.name}")

    rendered = render_pdf_pages(pdf_path, cfg.pdf.dpi)
    results: list[Path] = []
    for idx, (pdf_page, side) in enumerate(cfg.pdf.page_map, start=1):
        if pdf_page < 1 or pdf_page > len(rendered):
            raise ValueError(f"page_map 第 {idx} 项引用了不存在的 PDF 页 {pdf_page}")
        full = rendered[pdf_page - 1]
        if side == "s":
            img = full
        elif side in ("L", "R"):
            img = _take_half(full, side)
        else:  # 已被 config 校验拦下，保险起见
            raise ValueError(f"非法 side: {side}")
        out_path = out_dir / f"src_{idx:02d}.png"
        img.save(out_path, format="PNG")
        results.append(out_path)
        log.debug("写出 %s (%dx%d, 来自 PDF第%d页%s)",
                  out_path.name, img.width, img.height, pdf_page, side)
    log.info("拆页完成: %s → %d 源页", pdf_path.name, len(results))
    return results


if __name__ == "__main__":  # 手工冒烟: python -m src.pdf2pages <pdf> [out_dir] [config]
    import sys
    from .config_schema import load_config
    pdf = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "work/pages"
    conf = sys.argv[3] if len(sys.argv) > 3 else "config/config.yaml"
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(conf)
    pdf_to_src_pages(pdf, out, cfg)
