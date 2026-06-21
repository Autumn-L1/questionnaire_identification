"""CLI 入口：induct | infer | batch。

- induct: 离线做一次。对 data/ 下多份样本拆页→归纳→交叉 reconcile→导出 scales.yaml + 核对HTML。
- infer : 单文件识别。拆页→预处理→第1/2页→量表切分识别→聚合→CSV。
- batch : 遍历 data/ 下所有 PDF，逐份 infer，合并到一张总表。

每份 PDF 的中间产物写入 work/{subject_id}/pages、scales 等；复核图入 work/review。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from PIL import Image

from . import (config_schema, csv_io, llm_client, logger, page12, preprocess,
               scale_catalog, scale_infer, split, aggregate, validate)
from .config_schema import AppConfig, ScaleMeta, load_config, load_scales

log = logging.getLogger("qr.run")


# ---------------- 公共：拆页 + 预处理 ----------------
def split_and_preprocess(pdf_path: Path, cfg: AppConfig, work_dir: Path
                         ) -> tuple[list[Path], list[logger.Issue]]:
    """拆页 + 预处理，返回预处理后的 20 页路径列表(按 src 号)及诊断 issues。"""
    from . import pdf2pages
    pages_dir = work_dir / "pages"
    proc_dir = work_dir / "pages_proc"
    # 拆页(写原始)
    pdf2pages.pdf_to_src_pages(pdf_path, pages_dir, cfg)
    # 预处理
    reports = preprocess.preprocess_dir(pages_dir, proc_dir, cfg.preprocess)
    proc_paths = sorted(proc_dir.glob("src_*.png"))
    issues: list[logger.Issue] = []
    for i, rep in enumerate(reports, start=1):
        if rep.gutter_too_wide:
            issues.append(logger.Issue(
                subject_id="", scope=f"preprocess:src_{i:02d}", severity="WARN",
                code="gutter_too_wide",
                msg=f"src_{i:02d} 黑边宽{rep.gutter_removed_px}px超阈值"))
        if rep.deskew_too_large:
            issues.append(logger.Issue(
                subject_id="", scope=f"preprocess:src_{i:02d}", severity="WARN",
                code="deskew_too_large",
                msg=f"src_{i:02d} 倾斜{rep.deskew_angle:.1f}°超阈值"))
    return proc_paths, issues


def _load_page(paths: list[Path], idx: int) -> Image.Image:
    """按 src 号(1-based) 加载预处理页。"""
    p = paths[idx - 1]
    return Image.open(p).convert("RGB")


# ---------------- infer 单文件 ----------------
def infer_one(pdf_path: Path, cfg: AppConfig, scales: list[ScaleMeta],
              review: logger.ReviewLogger, client: llm_client.VisionLLMClient,
              fallback_id: str, append_to: Path | None = None) -> dict | None:
    """识别一份 PDF，返回一行 dict(并按需追加到 CSV)。"""
    t0 = time.time()
    subject_id_hint = fallback_id
    work_dir = Path(cfg.paths.work_dir) / fallback_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1) 拆页 + 预处理
    try:
        proc_paths, pre_issues = split_and_preprocess(pdf_path, cfg, work_dir)
    except Exception as e:
        review.issue(logger.Issue(fallback_id, "pdf", "ERROR", "split_fail",
                                  f"拆页失败: {e}"))
        return None
    for iss in pre_issues:
        iss.subject_id = fallback_id
        review.issue(iss)

    # 2) 第 1 页
    p1_img = _load_page(proc_paths, 1)
    p1 = page12.recognize_page1(p1_img, client, review, subject_id_hint)
    for iss in validate.validate_page1(p1, fallback_id):
        review.issue(iss)
        if iss.severity == "ERROR" and iss.code in ("bad_subject_id", "missing_subject_id"):
            # 存档编号裁剪图便于复核
            from .page12 import _crop_rel, ROI_P1_ID
            review.save_review_image(fallback_id, "page1_id",
                                     _crop_rel(p1_img, ROI_P1_ID),
                                     {"raw": p1.raw})

    # 编号合法则更新 subject_id 作列主键
    real_id = (p1.subject_id or "").strip()
    if real_id and real_id.isdigit():
        fallback_id = real_id

    # 3) 第 2 页
    p2_img = _load_page(proc_paths, 2)
    p2 = page12.recognize_page2(p2_img, client, review, fallback_id)
    for iss in validate.validate_page2(p2, fallback_id):
        review.issue(iss)

    # 4) 量表：按源页分组处理
    page_to_scales: dict[int, list[ScaleMeta]] = {}
    for s in scales:
        for p in s.source_pages:
            page_to_scales.setdefault(p, []).append(s)

    scales_res: list[tuple[ScaleMeta, validate.ScaleResult]] = []
    for page, metas in sorted(page_to_scales.items()):
        try:
            page_img = _load_page(proc_paths, page)
        except Exception as e:
            for m in metas:
                review.issue(logger.Issue(fallback_id, f"scale:{m.id}", "ERROR",
                                          "page_load_fail", f"源页{page}加载失败: {e}"))
            continue
        # 统一整页识别：送整页图 + 量表元数据 + 位置提示，由模型在整页中定位量表。
        # 彻底规避 bbox 偏移 / 同页多量表重叠导致的裁剪错位问题。
        from .page12 import _img_bytes as _page_bytes
        page_bytes = _page_bytes(page_img)
        for m in metas:
            res = scale_infer.infer_scale(page_bytes, m, client)
            res.subject_id = fallback_id
            for iss in validate.validate_scale(res, m):
                review.issue(iss)
                if iss.severity == "ERROR":
                    review.save_review_image(fallback_id, f"scale_{m.id}",
                                             page_bytes, {"raw": res.raw, "meta_id": m.id})
            scales_res.append((m, res))

    # 5) 聚合 + 输出
    row = aggregate.build_row(p1, p2, scales_res, fallback_id)
    fieldnames = aggregate.column_template(scales)
    if append_to is not None:
        csv_io.append_csv(row, fieldnames, append_to, cfg.csv.null_token)
    log.info("识别完成: %s -> %s (耗时 %.1fs)", pdf_path.name, fallback_id,
             time.time() - t0)
    return row


# ---------------- induct 归纳 ----------------
def induct(cfg: AppConfig, data_dir: Path, out_yaml: Path,
           focused: bool = False, n_samples: int | None = None) -> list[ScaleMeta]:
    """多样本归纳模板。focused=True 走按大量表聚焦归纳路径。"""
    client = llm_client.VisionLLMClient(cfg.llm)
    pdfs = sorted(data_dir.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"[induct] data 目录无 PDF: {data_dir}")
    if n_samples:
        pdfs = pdfs[:n_samples]
    if focused:
        return _induct_focused(cfg, client, pdfs, data_dir, out_yaml)

    scale_pages = list(range(3, 21))  # 源页 3..20 含量表
    per_sample: dict[int, list[list[scale_catalog.RawScale]]] = {p: [] for p in scale_pages}
    cache_dir = Path(cfg.paths.work_dir) / "induct_cache"

    for pdf in pdfs:
        log.info("[induct] 处理样本: %s", pdf.name)
        work_dir = Path(cfg.paths.work_dir) / f"_induct_{pdf.stem[:24]}"
        try:
            proc_paths, _ = split_and_preprocess(pdf, cfg, work_dir)
        except Exception as e:
            log.error("[induct] 样本 %s 拆页失败，跳过: %s", pdf.name, e)
            continue
        stem = pdf.stem[:24]
        for page in scale_pages:
            try:
                img = _load_page(proc_paths, page)
            except Exception as e:
                log.warning("[induct] %s 源页%d加载失败: %s", pdf.name, page, e)
                continue
            cache_path = cache_dir / f"{stem}_p{page:02d}.json"
            # key 含 prompt 版本，版本变更时旧缓存自动失效
            cache_key = f"{stem}_{scale_catalog.PROMPT_VERSION}"
            rs = scale_catalog.induct_page_cached(
                img, client, cache_path, key=cache_key, retries=cfg.induct.per_page_retry)
            per_sample[page].append(rs)
            log.info("[induct] %s 源页%d: 归纳到 %d 个量表", pdf.name, page, len(rs))

    metas = scale_catalog.reconcile(per_sample, cfg.induct.expected_total_scales,
                                    scale_pages)
    config_schema.save_scales(metas, out_yaml, inducted_from=str(data_dir))

    # 可视化核对页(用第一份样本的预处理页作底图)
    sample_pages = {}
    first_proc = Path(cfg.paths.work_dir)
    cand_dirs = sorted([d for d in first_proc.glob("_induct_*") if d.is_dir()])
    if cand_dirs:
        for page in scale_pages:
            p = cand_dirs[0] / "pages_proc" / f"src_{page:02d}.png"
            if p.exists():
                sample_pages[page] = p
    review_html = Path(cfg.paths.work_dir) / "induct" / "scales_review.html"
    scale_catalog.render_review_html(metas, sample_pages, review_html)

    log.info("[induct] 已导出模板 %s 与核对页 %s", out_yaml, review_html)
    log.info("[induct] ⚠ 请人工核对题数/选项/作答方式，修正后再用于正式识别。")
    return metas


def _induct_focused(cfg: AppConfig, client: llm_client.VisionLLMClient,
                    pdfs: list[Path], data_dir: Path, out_yaml: Path) -> list[ScaleMeta]:
    """按大量表逐个聚焦归纳：对每个(大量表,源页)×样本 精确识别该量表在该页的作答单元。

    优点：归属(group)由 layout 指定天然正确；同小节题目被合并(不逐题碎)；
    bbox 用中位数投票，抗拍摄偏移。
    """
    layout = scale_catalog.SCALE_LAYOUT
    cache_dir = Path(cfg.paths.work_dir) / "induct_focused_cache"
    # 预处理各样本
    samples: list[tuple[str, list[Path]]] = []
    for pdf in pdfs:
        log.info("[focused] 处理样本: %s", pdf.name)
        work_dir = Path(cfg.paths.work_dir) / f"_induct_{pdf.stem[:24]}"
        try:
            proc_paths, _ = split_and_preprocess(pdf, cfg, work_dir)
        except Exception as e:
            log.error("[focused] 样本 %s 拆页失败，跳过: %s", pdf.name, e)
            continue
        samples.append((pdf.stem[:24], proc_paths))
    if not samples:
        raise SystemExit("[focused] 无可用样本")

    per_group_page: dict[tuple[str, int], list[list[scale_catalog.RawScale]]] = {}
    all_pages = sorted({page for _, pages, _ in layout for page in pages})
    for group, pages, hint in layout:
        for page in pages:
            per_group_page.setdefault((group, page), [])
            for stem, proc_paths in samples:
                try:
                    img = _load_page(proc_paths, page)
                except Exception as e:
                    log.warning("[focused] %s 源页%d加载失败: %s", stem, page, e)
                    per_group_page[(group, page)].append([])
                    continue
                cache_path = cache_dir / f"{stem}_g{group}_p{page:02d}.json"
                key = f"{stem}_{scale_catalog.FOCUSED_PROMPT_VERSION}"
                units = scale_catalog.induct_group_on_page_cached(
                    img, client, group, hint, cache_path, key, cfg.induct.per_page_retry)
                per_group_page[(group, page)].append(units)
                log.info("[focused] %s 量表%s 源页%d: %d单元", stem, group, page, len(units))

    metas = scale_catalog.reconcile_focused(per_group_page)
    config_schema.save_scales(metas, out_yaml, inducted_from=str(data_dir))

    # 核对页(用第一样本的预处理页作底图)
    sample_pages = {page: samples[0][1][page - 1] for page in all_pages
                    if page - 1 < len(samples[0][1])}
    review_html = Path(cfg.paths.work_dir) / "induct" / "scales_review.html"
    scale_catalog.render_review_html(metas, sample_pages, review_html)
    log.info("[focused] 已导出模板 %s 与核对页 %s", out_yaml, review_html)
    log.info("[focused] ⚠ 请人工核对题数/选项/作答方式，修正后再用于正式识别。")
    return metas


# ---------------- CLI ----------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纸质问卷识别系统")
    parser.add_argument("--config", default="config/config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_induct = sub.add_parser("induct", help="多样本归纳量表模板(离线做一次)")
    p_induct.add_argument("--dir", default="data")
    p_induct.add_argument("--out", default="config/scales.yaml")
    p_induct.add_argument("--focused", action="store_true",
                          help="按大量表逐个聚焦归纳(归属准、不逐题碎、bbox中位数)")
    p_induct.add_argument("--n-samples", type=int, default=None,
                          help="只用前 N 份样本(默认全部)")

    p_infer = sub.add_parser("infer", help="识别单个 PDF")
    p_infer.add_argument("--pdf", required=True)
    p_infer.add_argument("--out", default="out/results.csv")
    p_infer.add_argument("--template", default="config/scales.yaml")

    p_batch = sub.add_parser("batch", help="批量识别 data/ 下所有 PDF")
    p_batch.add_argument("--dir", default="data")
    p_batch.add_argument("--out", default="out/results.csv")
    p_batch.add_argument("--template", default="config/scales.yaml")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    ts = logger.setup_logging(cfg.paths.log_dir)

    if args.cmd == "induct":
        induct(cfg, Path(args.dir), Path(args.out),
               focused=args.focused, n_samples=args.n_samples)
        return 0

    # infer / batch 需要模板与 LLM
    scales = load_scales(args.template)
    client = llm_client.VisionLLMClient(cfg.llm)
    review = logger.ReviewLogger(cfg.paths.log_dir, cfg.paths.review_dir, ts)

    exit_code = 0
    if args.cmd == "infer":
        pdf_path = Path(args.pdf)
        # 若 out 已存在则覆盖(单文件模式重写)
        out_path = Path(args.out)
        if out_path.exists():
            out_path.unlink()
        row = infer_one(pdf_path, cfg, scales, review, client,
                        fallback_id=pdf_path.stem[:24], append_to=out_path)
        if row is None:
            exit_code = 2
    elif args.cmd == "batch":
        out_path = Path(args.out)
        if out_path.exists():
            out_path.unlink()
        pdfs = sorted(Path(args.dir).glob("*.pdf"))
        if not pdfs:
            log.error("目录无 PDF: %s", args.dir)
            return 1
        for pdf in pdfs:
            try:
                infer_one(pdf, cfg, scales, review, client,
                          fallback_id=pdf.stem[:24], append_to=out_path)
            except Exception as e:
                review.issue(logger.Issue(pdf.stem[:24], "run", "ERROR",
                                          "infer_crash", f"识别异常中断: {e}"))
                log.exception("样本 %s 识别异常", pdf.name)
                exit_code = 2

    log.info(logger_const_summary(review))
    return exit_code or (2 if review.error_count else 0)


def logger_const_summary(review: logger.ReviewLogger) -> str:
    return review.summary()


if __name__ == "__main__":
    sys.exit(main())
