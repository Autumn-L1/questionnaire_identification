"""聚合：把一个被试的所有识别结果拼成一行 flat dict，并生成列名模板。"""
from __future__ import annotations

from .config_schema import ScaleMeta
from .validate import Page1Result, Page2Result, ScaleResult


def column_template(scales: list[ScaleMeta]) -> list[str]:
    """生成 CSV 表头列名：基础列 + 第2页17题 + 各量表题。"""
    cols = ["subject_id", "name", "date"]
    # 第 2 页 17 题
    for q in range(1, 18):
        cols.append(f"p2_q{q}")
    # 各量表
    for s in scales:
        for q in s.q_range:
            cols.append(f"{s.id}_{q}")
    return cols


def build_row(p1: Page1Result, p2: Page2Result,
              scales_res: list[tuple[ScaleMeta, ScaleResult]],
              fallback_id: str) -> dict:
    """拼成一行。subject_id 缺失时用 fallback_id 兜底。"""
    row: dict = {}
    sid = (p1.subject_id or "").strip() or fallback_id
    row["subject_id"] = sid
    row["name"] = p1.name or ""
    row["date"] = p1.date or ""
    # 第 2 页
    for q in range(1, 18):
        row[f"p2_q{q}"] = _norm(p2.answers.get(str(q)))
    # 量表
    for meta, res in scales_res:
        for q in meta.q_range:
            row[f"{meta.id}_{q}"] = _norm(res.answers.get(str(q)))
    return row


def _norm(v) -> str | None:
    """归一化单元格值。dict(多维度答案如 EMBU 父/母)序列化为 JSON 字符串保留维度。"""
    if v is None:
        return None
    if isinstance(v, dict):
        import json
        s = json.dumps(v, ensure_ascii=False).strip()
        return s if s else None
    s = str(v).strip()
    return s if s else None
