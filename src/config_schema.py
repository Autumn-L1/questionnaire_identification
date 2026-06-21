"""配置加载与校验。

用 dataclass 手写校验，不引入 pydantic。加载 config.yaml 时对必填项、
page_map 长度(=20)、路径存在性等做检查，不合法直接抛 SystemExit 退出。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_SCALE_KEYS = ("id", "source_pages", "n_items", "answer_style")
ANSWER_STYLES = {"check", "circle", "fill", "hatch", "mixed"}


@dataclass
class LLMConfig:
    api_key_env: str
    base_url: str
    model: str
    timeout: int = 120
    max_retries: int = 3
    temperature: float = 0.0
    max_image_long_edge: int = 2200
    json_mode: bool = True

    @property
    def api_key(self) -> str:
        """懒加载获取 api_key，兼容两种填法：

        - 若 ``api_key_env`` 的值形如环境变量名(全大写下划线，如
          ``QUESTIONNAIRE_OCR_API_KEY``)，则从该环境变量读取(推荐，更安全)；
        - 否则(如直接填了 ``sk-xxxx`` 形式的 key)，原样作为 key 使用。

        仅在真正调用 LLM 时校验，避免拆页等不需 LLM 的流程被阻断。
        """
        import re
        raw = (self.api_key_env or "").strip()
        if re.match(r"^[A-Z_][A-Z0-9_]*$", raw):
            # 当作环境变量名
            key = os.environ.get(raw, "").strip()
            if not key:
                raise SystemExit(
                    f"[配置错误] 未在环境变量 {raw} 中找到 LLM api_key。"
                    f"请先 setx {raw} \"你的key\"，或在 config 中直接填写 key。"
                )
            return key
        # 直接当作 key 使用
        if not raw:
            raise SystemExit("[配置错误] llm.api_key_env 既非环境变量名也非有效 key。")
        return raw


@dataclass
class PathsConfig:
    work_dir: str
    out_csv: str
    log_dir: str
    review_dir: str
    template_yaml: str


@dataclass
class PdfConfig:
    dpi: int = 200
    # page_map: List[[pdf_page:int, side:str]]
    page_map: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class PreprocessConfig:
    gutter_max_width_pct: float = 0.15
    deskew_enabled: bool = False
    deskew_max_angle: float = 5.0
    normalize_long_edge: int = 2200


@dataclass
class InferConfig:
    scale_padding_pct: float = 0.03
    confidence_warn: float = 0.6
    small_scale_item_threshold: int = 8
    whole_page_for_small_scales: bool = True


@dataclass
class InductConfig:
    expected_total_scales: int = 21
    per_page_retry: int = 1


@dataclass
class CsvConfig:
    columns_order: str = "template"
    null_token: str = ""


@dataclass
class AppConfig:
    llm: LLMConfig
    paths: PathsConfig
    pdf: PdfConfig
    preprocess: PreprocessConfig
    infer: InferConfig
    induct: InductConfig
    csv: CsvConfig
    raw: dict[str, Any] = field(default_factory=dict)


def _get(d: dict, key: str, default=None, required: bool = False, where: str = ""):
    if key not in d or d[key] is None:
        if required:
            raise SystemExit(f"[配置错误] 缺少必填项 {where}{key}")
        return default
    return d[key]


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"[配置错误] 配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    llm_raw = _get(data, "llm", required=True, where="llm.")
    llm = LLMConfig(
        api_key_env=_get(llm_raw, "api_key_env", required=True, where="llm."),
        base_url=_get(llm_raw, "base_url", required=True, where="llm."),
        model=_get(llm_raw, "model", required=True, where="llm."),
        timeout=_get(llm_raw, "timeout", 120),
        max_retries=_get(llm_raw, "max_retries", 3),
        temperature=_get(llm_raw, "temperature", 0.0),
        max_image_long_edge=_get(llm_raw, "max_image_long_edge", 2200),
        json_mode=_get(llm_raw, "json_mode", True),
    )

    paths_raw = _get(data, "paths", required=True, where="paths.")
    paths = PathsConfig(
        work_dir=_get(paths_raw, "work_dir", required=True, where="paths."),
        out_csv=_get(paths_raw, "out_csv", required=True, where="paths."),
        log_dir=_get(paths_raw, "log_dir", required=True, where="paths."),
        review_dir=_get(paths_raw, "review_dir", required=True, where="paths."),
        template_yaml=_get(paths_raw, "template_yaml", required=True, where="paths."),
    )

    pdf_raw = _get(data, "pdf", required=True, where="pdf.")
    page_map_raw = _get(pdf_raw, "page_map", required=True, where="pdf.")
    if len(page_map_raw) != 20:
        raise SystemExit(
            f"[配置错误] pdf.page_map 必须恰好 20 项(对应源问卷 20 页)，"
            f"当前 {len(page_map_raw)} 项。"
        )
    page_map: list[tuple[int, str]] = []
    for i, item in enumerate(page_map_raw):
        if len(item) != 2 or item[1] not in ("s", "L", "R"):
            raise SystemExit(
                f"[配置错误] pdf.page_map 第 {i+1} 项格式应为 [pdf页号, 's'|'L'|'R']，"
                f"当前: {item}"
            )
        page_map.append((int(item[0]), str(item[1])))
    pdf = PdfConfig(dpi=_get(pdf_raw, "dpi", 200), page_map=page_map)

    pre_raw = _get(data, "preprocess", required=True, where="preprocess.")
    preprocess = PreprocessConfig(
        gutter_max_width_pct=_get(pre_raw, "gutter_max_width_pct", 0.15),
        deskew_enabled=_get(pre_raw, "deskew_enabled", False),
        deskew_max_angle=_get(pre_raw, "deskew_max_angle", 5.0),
        normalize_long_edge=_get(pre_raw, "normalize_long_edge", 2200),
    )

    inf_raw = _get(data, "infer", required=True, where="infer.")
    infer = InferConfig(
        scale_padding_pct=_get(inf_raw, "scale_padding_pct", 0.03),
        confidence_warn=_get(inf_raw, "confidence_warn", 0.6),
        small_scale_item_threshold=_get(inf_raw, "small_scale_item_threshold", 8),
        whole_page_for_small_scales=_get(inf_raw, "whole_page_for_small_scales", True),
    )

    ind_raw = _get(data, "induct", required=True, where="induct.")
    induct = InductConfig(
        expected_total_scales=_get(ind_raw, "expected_total_scales", 21),
        per_page_retry=_get(ind_raw, "per_page_retry", 1),
    )

    csv_raw = _get(data, "csv", required=True, where="csv.")
    csv = CsvConfig(
        columns_order=_get(csv_raw, "columns_order", "template"),
        null_token=_get(csv_raw, "null_token", ""),
    )

    return AppConfig(
        llm=llm, paths=paths, pdf=pdf, preprocess=preprocess,
        infer=infer, induct=induct, csv=csv, raw=data,
    )


# ---------------- 量表模板(scales.yaml) ----------------

@dataclass
class ScaleMeta:
    id: str                       # 如 S01
    title: str                    # 量表名称
    source_pages: list[int]       # 所在源页(可跨页)
    n_items: int                  # 题数
    answer_style: str             # check|circle|fill|hatch|mixed
    option_range: list[int] | None = None  # [lo, hi]，fill 题为 None
    first_q: int = 1
    last_q: int | None = None     # 缺省 = first_q + n_items - 1
    bbox_nominal: list[float] | None = None  # [x1,y1,x2,y2] 归一化 0-1(主源页)
    bbox_page: int | None = None          # bbox 所属源页
    anchors: list[dict] | None = None
    notes: str = ""
    group: str = ""               # 所属大量表中文序号，如 "一"/"十五"
    is_sub: bool = False          # 是否为子量表/字问题
    unit_label: str = ""          # 单元标签，如 "子量表1"/"字问题1"/"主"
    sub_keys: list[str] | None = None  # 多维度作答的维度键，如 ["父亲","母亲"](EMBU)

    def __post_init__(self):
        if self.last_q is None:
            self.last_q = self.first_q + self.n_items - 1
        if self.answer_style not in ANSWER_STYLES:
            # 归纳产物可能给出非法 style，归一为 mixed 以容错(导出后人工可改)
            logging.getLogger("qr.config").warning(
                "量表 %s 的 answer_style=%r 非法，临时归一为 'mixed'", self.id, self.answer_style
            )
            self.answer_style = "mixed"
        if self.n_items != self.last_q - self.first_q + 1:
            # 投票/归纳可能产生不自洽，以 first_q..last_q 为准重算 n_items(容错)，
            # 而非抛异常导致整批模板丢失。导出后由人工核对。
            derived = self.last_q - self.first_q + 1
            logging.getLogger("qr.config").warning(
                "量表 %s 题数不自洽(n_items=%d, first..last=%d..%d)，"
                "按题号区间重算 n_items=%d，请人工核对。",
                self.id, self.n_items, self.first_q, self.last_q, derived,
            )
            self.n_items = derived

    @property
    def q_range(self) -> range:
        return range(self.first_q, self.last_q + 1)


def load_scales(path: str | Path) -> list[ScaleMeta]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"[配置错误] 量表模板不存在: {path}\n"
            f"请先运行: python -m src.run induct --dir data/ --out {path}"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    scales_raw = _get(data, "scales", required=True, where="scales.")
    out: list[ScaleMeta] = []
    for s in scales_raw:
        for k in REQUIRED_SCALE_KEYS:
            if k not in s:
                raise SystemExit(f"[配置错误] 某量表缺少必填键 {k}: {s}")
        sm = ScaleMeta(
            id=str(s["id"]),
            title=str(s.get("title", s["id"])),
            source_pages=list(s["source_pages"]),
            n_items=int(s["n_items"]),
            answer_style=str(s["answer_style"]),
            option_range=s.get("option_range"),
            first_q=int(s.get("first_q", 1)),
            last_q=s.get("last_q"),
            bbox_nominal=s.get("bbox_nominal"),
            bbox_page=s.get("bbox_page"),
            anchors=s.get("anchors"),
            notes=s.get("notes", ""),
            group=str(s.get("group", "")),
            is_sub=bool(s.get("is_sub", False)),
            unit_label=str(s.get("unit_label", "")),
            sub_keys=s.get("sub_keys"),
        )
        out.append(sm)
    return out


def save_scales(scales: list[ScaleMeta], path: str | Path,
                inducted_from: str | None = None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "inducted_from": inducted_from or "",
        "total_scales": len(scales),
        "scales": [_scale_to_dict(s) for s in scales],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=1000)


def _scale_to_dict(s: ScaleMeta) -> dict:
    d = {
        "id": s.id,
        "group": s.group,
        "unit_label": s.unit_label,
        "title": s.title,
        "source_pages": s.source_pages,
        "n_items": s.n_items,
        "answer_style": s.answer_style,
        "first_q": s.first_q,
        "last_q": s.last_q,
    }
    if s.is_sub:
        d["is_sub"] = True
    if s.sub_keys:
        d["sub_keys"] = list(s.sub_keys)
    if s.option_range is not None:
        d["option_range"] = s.option_range
    if s.bbox_nominal is not None:
        d["bbox_nominal"] = [round(v, 4) for v in s.bbox_nominal]
        d["bbox_page"] = s.bbox_page
    if s.anchors:
        d["anchors"] = s.anchors
    if s.notes:
        d["notes"] = s.notes
    return d


def safe_id(text: str) -> str:
    """把任意量表标题转为合法的列名片段。"""
    t = re.sub(r"[^\w一-鿿]+", "_", str(text)).strip("_")
    return t or "scale"
