"""集中校验规则。对识别结果生成 ``Issue`` 列表，由 ReviewLogger 落盘。

三组规则：
- 第1页：编号须为 2 位、姓名非空、日期可解析；
- 第2页：必须 17 题、题号连续；
- 单量表：题数=n_items、答案在 option_range 内、未答/多选/低置信度记 WARN。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config_schema import ScaleMeta
from .logger import Issue

_SUBJECT_ID_RE = re.compile(r"^\d{2}$")
_DATE_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})")


# ---------------- 数据结构 ----------------
@dataclass
class Page1Result:
    subject_id: str | None = None
    name: str | None = None
    date: str | None = None
    issues_payload: list[dict] = field(default_factory=list)
    confidence: float = 1.0
    raw: dict | None = None


@dataclass
class Page2Result:
    answers: dict[str, Any] = field(default_factory=dict)   # qno -> str|None
    issues_payload: list[dict] = field(default_factory=list)
    confidence: float = 1.0
    raw: dict | None = None


@dataclass
class ScaleResult:
    scale_id: str
    answers: dict[str, Any] = field(default_factory=dict)
    issues_payload: list[dict] = field(default_factory=list)
    confidence: float = 1.0
    raw: dict | None = None
    subject_id: str = ""


# ---------------- 第 1 页 ----------------
def validate_page1(r: Page1Result, subject_id: str) -> list[Issue]:
    out: list[Issue] = []
    sid = (r.subject_id or "").strip()
    if not sid:
        out.append(Issue(subject_id, "page1", "ERROR", "missing_subject_id",
                         "未识别到左上角红色编号"))
    elif not _SUBJECT_ID_RE.match(sid):
        out.append(Issue(subject_id, "page1", "ERROR", "bad_subject_id",
                         f"编号不是2位数字: {sid!r}", ctx={"value": sid}))
    if not (r.name or "").strip():
        out.append(Issue(subject_id, "page1", "ERROR", "missing_name",
                         "未识别到右下角姓名"))
    d = (r.date or "").strip()
    if not d:
        out.append(Issue(subject_id, "page1", "ERROR", "missing_date",
                         "未识别到调查时间"))
    elif not _DATE_RE.search(d):
        out.append(Issue(subject_id, "page1", "WARN", "bad_date",
                         f"调查时间格式难以解析: {d!r}", ctx={"value": d}))
    if r.confidence < 0.6:
        out.append(Issue(subject_id, "page1", "WARN", "low_confidence",
                         f"第1页置信度低 {r.confidence:.2f}"))
    return out


# ---------------- 第 2 页 ----------------
def validate_page2(r: Page2Result, subject_id: str, expected: int = 17
                   ) -> list[Issue]:
    out: list[Issue] = []
    keys = list(r.answers.keys())
    if len(keys) != expected:
        out.append(Issue(subject_id, "page2", "ERROR", "item_count_mismatch",
                         f"第2页识别到 {len(keys)} 题，期望 {expected} 题",
                         ctx={"got": len(keys), "expected": expected,
                              "keys": keys}))
    # 题号连续性
    try:
        nums = sorted(int(k) for k in keys)
    except ValueError:
        out.append(Issue(subject_id, "page2", "ERROR", "bad_item_key",
                         f"第2页存在非数字题号: {keys}"))
        nums = []
    if nums and (nums[0] != 1 or nums[-1] != expected or len(set(nums)) != len(nums)):
        miss = sorted(set(range(1, expected + 1)) - set(nums))
        dup = [n for n in set(nums) if nums.count(n) > 1]
        out.append(Issue(subject_id, "page2", "WARN", "item_not_contiguous",
                         f"第2页题号不连续 缺失={miss} 重复={dup}",
                         ctx={"missing": miss, "duplicate": dup}))
    # 空答案
    blanks = [k for k, v in r.answers.items() if v is None or str(v).strip() == ""]
    if blanks:
        out.append(Issue(subject_id, "page2", "WARN", "unanswered",
                         f"第2页以下题未作答: {blanks}", ctx={"items": blanks}))
    if r.confidence < 0.6:
        out.append(Issue(subject_id, "page2", "WARN", "low_confidence",
                         f"第2页置信度低 {r.confidence:.2f}"))
    return out


# ---------------- 单量表 ----------------
def _coerce_int(v: Any) -> int | None:
    try:
        if isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def validate_scale(r: ScaleResult, meta: ScaleMeta) -> list[Issue]:
    out: list[Issue] = []
    sid = r.subject_id
    scope = f"scale:{meta.id}"
    expected = set(str(q) for q in meta.q_range)
    got = set(r.answers.keys())

    missing = sorted(expected - got, key=lambda x: int(x) if x.isdigit() else 0)
    extra = sorted(got - expected, key=lambda x: int(x) if x.isdigit() else 0)
    if missing:
        out.append(Issue(sid, scope, "ERROR", "missing_item",
                         f"量表 {meta.id}({meta.title}) 缺题: {missing}",
                         ctx={"scale": meta.id, "missing": missing}))
    if extra:
        out.append(Issue(sid, scope, "WARN", "extra_item",
                         f"量表 {meta.id} 多出题号: {extra}",
                         ctx={"scale": meta.id, "extra": extra}))

    # 选项范围校验(仅数值型 answer_style)。答案可能是多维度 dict(如 EMBU 每题
    # 父亲/母亲各一值)，对每个子值分别校验；仅当存在越界子值才报错。
    if meta.option_range and meta.answer_style in ("circle", "check", "hatch"):
        lo, hi = meta.option_range[0], meta.option_range[1]
        for q, v in r.answers.items():
            if v is None or str(v).strip() == "":
                continue
            subs = v.values() if isinstance(v, dict) else [v]
            bad = []
            for sv in subs:
                if sv is None or str(sv).strip() == "":
                    continue
                iv = _coerce_int(sv)
                if iv is None or iv < lo or iv > hi:
                    bad.append(sv)
            if bad:
                out.append(Issue(sid, scope, "ERROR", "out_of_range",
                                 f"量表 {meta.id} 第{q}题答案={v!r} 含越界值{bad} [{lo},{hi}]",
                                 ctx={"scale": meta.id, "q": q, "value": v,
                                      "bad": bad, "range": [lo, hi]}))

    # 未答 / 低置信度
    blanks = [k for k, v in r.answers.items() if v is None or str(v).strip() == ""]
    if blanks:
        out.append(Issue(sid, scope, "WARN", "unanswered",
                         f"量表 {meta.id} 以下题未作答/无法判断: {blanks}",
                         ctx={"scale": meta.id, "items": blanks}))
    if r.confidence < 0.6:
        out.append(Issue(sid, scope, "WARN", "low_confidence",
                         f"量表 {meta.id} 置信度低 {r.confidence:.2f}"))
    return out
