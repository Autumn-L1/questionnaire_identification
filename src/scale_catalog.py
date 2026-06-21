"""量表模板归纳(离线、做一次)。

两步：
1. Pass A 页级清单：对每份样本的 src_03..20 逐页问大模型——本页含哪些量表、
   每个量表的 名称/题数/选项范围/作答方式/归一化bbox/首末题号。
2. Pass B 跨样本 reconcile：对「同源页同位置」的量表按 题数/选项/作答方式 投票取众数
   (单份样本误判被多数否决)；合并跨页量表；全局约束(总量表≈21、题号连续)。

产物：config/scales.yaml + work/induct/scales_review.html(每个量表 bbox 画框叠原图)。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from .config_schema import ScaleMeta, save_scales
from .llm_client import VisionLLMClient, LLMError

log = logging.getLogger("qr.catalog")

SYS_INDUCT_PAGE = (
    "你是问卷版式分析助手。本问卷由约21个大量表组成，每个大量表由一个中文序号统领"
    "(标题形如「一、…」「二、…」「十五、…」「二十一、…」)。一些大量表下含若干「子量表」。"
    "一个大量表/子量表可能跨多页。\n\n"
    "现在给你问卷的【一页】图片。请识别本页出现的每一个「量表」或「子量表」。\n"
    "【重要】一个量表/子量表是【一个有标题、或同主题的多道题目组成的小节】，"
    "绝不要把单道题作为独立条目。如果本页是某个量表/子量表的延续部分"
    "(同一主题的多道连续题目、无新的小节标题)，请把这些题目【合并为一个】条目。\n\n"
    "对每个条目输出：\n"
    "1) group: 所属大量表的中文序号，如 \"一\"/\"十五\"/\"二十一\"。"
    "延续部分仍填它实际所属的序号；实在无法判断填 \"未知\"。\n"
    "2) is_sub: 是否子量表(true/false)。大量表主体填 false。\n"
    "3) title: 本条目的标题或首题文本(逐字照抄)。\n"
    "4) n_items: 本条目【在本页】的题目数量。\n"
    "5) option_range: 选项数值范围如 [1,5]；无固定数值选项则填 null。\n"
    "6) answer_style: 作答方式，取值 check(打勾) | circle(圈数字) | hatch(涂黑方框) | fill(手写填空) | mixed。\n"
    "7) bbox: 本条目在本页的边界框，必须是【归一化到0~1】的坐标 [x1,y1,x2,y2] "
    "(左上为0,0，右下为1,1)。绝对不要输出像素值，任何坐标都必须是0到1之间的小数。\n"
    "8) first_q / last_q: 本条目在本页的题号起止(无显式题号则 first_q=1、last_q=n_items)。\n\n"
    "严格输出JSON：{\"units\":[{上述字段},...]}。本页没有任何量表就返回 {\"units\":[]}。"
    "只报告肉眼可见的内容，题数看不清就在 title 后用括号标注「(题数不确定)」，不要编造。"
)
# prompt 版本号，变更时缓存自动失效
PROMPT_VERSION = "v4"


@dataclass
class RawScale:
    title: str
    n_items: int
    option_range: list | None
    answer_style: str
    bbox: list[float]
    first_q: int
    last_q: int
    group: str = ""
    is_sub: bool = False


def _img_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _raw_to_obj(rs: RawScale) -> dict:
    return {"title": rs.title, "n_items": rs.n_items,
            "option_range": rs.option_range, "answer_style": rs.answer_style,
            "bbox": rs.bbox, "first_q": rs.first_q, "last_q": rs.last_q,
            "group": rs.group, "is_sub": rs.is_sub}


def _normalize_bbox(bbox: list[float], w: int, h: int) -> list[float]:
    """强制 bbox 归一化到 0-1。若任一坐标>1.5 视为像素，按图像尺寸归一化。"""
    if not bbox or len(bbox) != 4:
        return [0.0, 0.0, 1.0, 1.0]
    try:
        b = [float(x) for x in bbox]
    except (TypeError, ValueError):
        return [0.0, 0.0, 1.0, 1.0]
    if max(abs(v) for v in b) > 1.5:
        # 像素坐标 → 归一化
        b = [b[0] / w, b[1] / h, b[2] / w, b[3] / h]
    # 钳到 [0,1] 并保证 x1<x2, y1<y2
    x1, x2 = sorted([min(max(b[0], 0), 1), min(max(b[2], 0), 1)])
    y1, y2 = sorted([min(max(b[1], 0), 1), min(max(b[3], 0), 1)])
    if x2 - x1 < 1e-3:
        x1, x2 = 0.0, 1.0
    if y2 - y1 < 1e-3:
        y1, y2 = 0.0, 1.0
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def _parse_scales_raw(units_raw: list, w: int, h: int) -> list[RawScale]:
    """解析模型返回的 units 列表，强制归一化 bbox。"""
    out: list[RawScale] = []
    for s in units_raw:
        try:
            n_items = int(s.get("n_items", 0) or 0)
            fq = int(s.get("first_q", 1) or 1)
            lq = int(s.get("last_q", n_items or 1) or (n_items or 1))
            out.append(RawScale(
                title=str(s.get("title", "")).strip(),
                n_items=n_items,
                option_range=s.get("option_range"),
                answer_style=str(s.get("answer_style", "fill")),
                bbox=_normalize_bbox(s.get("bbox"), w, h),
                first_q=fq,
                last_q=lq,
                group=str(s.get("group", "") or "").strip(),
                is_sub=bool(s.get("is_sub", False)),
            ))
        except (TypeError, ValueError):
            log.warning("丢弃一条解析失败的作答单元条目: %s", s)
    return out


def induct_page(img: Image.Image, client: VisionLLMClient,
                retries: int = 1) -> list[RawScale]:
    """对单页归纳，返回该页作答单元清单(无缓存版)。"""
    w, h = img.size
    for attempt in range(retries + 1):
        try:
            r = client.ask_image(_img_bytes(img), SYS_INDUCT_PAGE,
                                 "分析本页含哪些作答单元，严格输出JSON(units)。")
            units_raw = (r.raw_json or {}).get("units") or (r.raw_json or {}).get("scales") or []
            return _parse_scales_raw(units_raw, w, h)
        except LLMError as e:
            log.warning("页归纳失败(%d): %s", attempt + 1, e)
            if attempt == retries:
                return []
    return []


def induct_page_cached(img: Image.Image, client: VisionLLMClient,
                       cache_path: Path, key: str, retries: int = 1
                       ) -> list[RawScale]:
    """带 JSON 缓存的页归纳。cache_path 为缓存文件路径(每页一个)。

    命中缓存则直接返回，避免重复 API 调用；未命中则调用并写入缓存。
    key 应包含 prompt 版本，版本变更时缓存自动失效。
    """
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("key") == key and isinstance(data.get("units"), list):
                log.info("命中缓存: %s", cache_path.name)
                w, h = img.size
                return _parse_scales_raw(data["units"], w, h)
        except (json.JSONDecodeError, OSError):
            pass
    rs = induct_page(img, client, retries)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"key": key, "units": [_raw_to_obj(r) for r in rs]},
            ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("写缓存失败 %s: %s", cache_path, e)
    return rs


# ---------------- 跨样本 reconcile ----------------
def _vote(values: list):
    """投票取众数；平票取首个出现值。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    # option_range 用元组化以便计数
    conv = []
    for v in vals:
        conv.append(tuple(v) if isinstance(v, list) else v)
    return Counter(conv).most_common(1)[0][0]


def reconcile(per_sample: dict[int, list[list[RawScale]]], expected_total: int,
              scale_pages: list[int]) -> list[ScaleMeta]:
    """按源页聚合多份样本的归纳结果，投票生成 ScaleMeta 列表。

    per_sample: {src_page: [ [样本0的量表列表], [样本1...], ... ]}
    scale_pages: 要归纳的源页号(如 3..20)
    """
    metas: list[ScaleMeta] = []
    sid_counter = 0
    for page in scale_pages:
        sample_lists = per_sample.get(page, [])
        n_scales_per_sample = [len(s) for s in sample_lists]
        if not n_scales_per_sample:
            log.warning("源页 %d 无任何样本归纳结果，跳过", page)
            continue
        # 该页量表数取众数
        n_scales = Counter(n_scales_per_sample).most_common(1)[0][0]
        if n_scales == 0:
            continue
        # 对每个「位置槽」聚合各样本对应量表
        for slot in range(n_scales):
            cands = [s[slot] for s in sample_lists if len(s) > slot]
            if not cands:
                continue
            n_items = _vote([c.n_items for c in cands]) or 0
            style = _vote([c.answer_style for c in cands]) or "fill"
            opt = _vote([c.option_range for c in cands])
            first_q = _vote([c.first_q for c in cands]) or 1
            # last_q 不独立投票：以 first_q + n_items 推导，确保三者自洽
            # (题数 n_items 是最稳定的特征，last_q 各样本可能因跨页而分歧)
            last_q = first_q + n_items - 1
            # title 取出现次数最多
            title = Counter([c.title for c in cands if c.title]).most_common(1)
            title = title[0][0] if title else f"scale_p{page}_{slot + 1}"
            # bbox 取各样本均值
            bboxes = [c.bbox for c in cands if len(c.bbox) == 4]
            bbox = [sum(b[i] for b in bboxes) / len(bboxes) for i in range(4)] \
                if bboxes else None
            sid_counter += 1
            group = _vote([c.group for c in cands]) or ""
            is_sub = _vote([c.is_sub for c in cands]) or False
            metas.append(ScaleMeta(
                id=f"S{sid_counter:02d}",
                title=title,
                source_pages=[page],
                n_items=n_items,
                answer_style=style,
                option_range=list(opt) if isinstance(opt, tuple) else opt,
                first_q=first_q,
                last_q=last_q,
                bbox_nominal=bbox,
                bbox_page=page,
                notes=f"归纳自{len(cands)}份样本(源页{page}槽{slot + 1})",
                group=group,
                is_sub=bool(is_sub),
            ))

    # 为同 group 内的单元设置 unit_label(主/子量表1/字问题1...)，并按 group 重排 id
    _assign_unit_labels(metas)
    metas = _sort_and_renumber(metas)

    n_groups = len({m.group for m in metas if m.group and m.group != "未知"})
    log.info("归纳完成: 共 %d 个作答单元，归属 %d 个大量表(期望大量表约%d)。",
             len(metas), n_groups, expected_total)
    log.info("大量表分组: %s", _group_summary(metas))
    return metas


_CN_ORDER = "一二三四五六七八九十"
_CN_MAP = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,
           "十一":11,"十二":12,"十三":13,"十四":14,"十五":15,"十六":16,"十七":17,
           "十八":18,"十九":19,"二十":20,"二十一":21,"二十二":22,"二十三":23}


def _group_rank(g: str) -> int:
    """中文序号排序键；未知/空排到末尾。"""
    return _CN_MAP.get(g, 999)


def _assign_unit_labels(metas: list[ScaleMeta]) -> None:
    """为每个大量表内的单元打标签：主体=主，子量表=子量表1..，字问题类归并。"""
    by_group: dict[str, list[ScaleMeta]] = {}
    for m in metas:
        by_group.setdefault(m.group, []).append(m)
    for g, items in by_group.items():
        subs = [m for m in items if m.is_sub]
        mains = [m for m in items if not m.is_sub]
        # 主体
        if len(items) == 1:
            items[0].unit_label = "主"
        else:
            for m in mains:
                m.unit_label = "主"
            for i, m in enumerate(subs, start=1):
                m.unit_label = f"子量表{i}" if "子" not in m.title and "字问题" not in m.title else f"子单元{i}"


def _sort_and_renumber(metas: list[ScaleMeta]) -> list[ScaleMeta]:
    """按页面顺序排序后重新编号 S01..SNN，便于对照原图查看。

    主键 = 起始源页；页内按 y(上→下) 再 x(左→右)；主体优先于子量表。
    大量表归属(group)仅作信息保留，不参与排序。
    """
    metas.sort(key=lambda m: (min(m.source_pages or [99]),
                              m.bbox_nominal[1] if m.bbox_nominal else 0,
                              m.bbox_nominal[0] if m.bbox_nominal else 0,
                              0 if not m.is_sub else 1))
    for i, m in enumerate(metas, start=1):
        m.id = f"S{i:02d}"
    return metas


def _group_summary(metas: list[ScaleMeta]) -> str:
    by_group: dict[str, list[ScaleMeta]] = {}
    for m in metas:
        by_group.setdefault(m.group or "未知", []).append(m)
    parts = []
    for g in sorted(by_group, key=_group_rank):
        items = by_group[g]
        parts.append(f"{g}({len(items)})")
    return ", ".join(parts)


# ---------------- 可视化核对页 ----------------
def render_review_html(metas: list[ScaleMeta], sample_pages: dict[int, Path],
                       out_html: Path):
    """把每个量表的 bbox 画框叠在样本原图上，拼成一个 HTML 供肉眼核对。"""
    out_html.parent.mkdir(parents=True, exist_ok=True)
    by_page: dict[int, list[ScaleMeta]] = {}
    for m in metas:
        if m.bbox_page:
            by_page.setdefault(m.bbox_page, []).append(m)

    # 按 group 分配颜色
    groups = sorted({m.group or "未知" for m in metas}, key=_group_rank)
    palette = ["red", "blue", "green", "purple", "orange", "brown", "magenta",
               "teal", "navy", "olive", "maroon", "sienna", "indigo", "crimson",
               "darkgreen", "darkorange", "darkmagenta", "darkslateblue",
               "chocolate", "darkcyan", "goldenrod"]
    gcolor = {g: palette[i % len(palette)] for i, g in enumerate(groups)}

    parts = ["<html><head><meta charset='utf-8'><style>"
             "body{font-family:sans-serif} img{max-width:49%;border:1px solid #ccc;margin:4px}"
             ".p{clear:both}h3{margin:6px 0}"
             ".lg{font-size:13px;margin:8px 0}</style></head><body>"]
    # 图例
    lg = " ".join(f"<span style='color:{gcolor[g]}'>■ {g}({sum(1 for m in metas if (m.group or '未知')==g)})</span>"
                  for g in groups)
    parts.append(f"<div class='lg'><b>大量表图例(颜色)：</b>{lg}</div>")
    for page in sorted(by_page):
        src_path = sample_pages.get(page)
        if not src_path or not Path(src_path).exists():
            continue
        img = Image.open(src_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        for m in by_page[page]:
            if not m.bbox_nominal:
                continue
            x1, y1, x2, y2 = m.bbox_nominal
            box = (x1 * w, y1 * h, x2 * w, y2 * h)
            col = gcolor.get(m.group or "未知", "red")
            draw.rectangle(box, outline=col, width=max(3, w // 400))
            label = f"{m.id}[{m.group or '?'}|{m.unit_label}] {m.n_items}项 {m.answer_style}"
            draw.text((box[0] + 4, box[1] + 2), label, fill=col)
        ann_path = out_html.parent / f"_annot_p{page:02d}.png"
        img.save(ann_path)
        parts.append(f"<div class='p'><h3>源页 {page}</h3>"
                     f"<img src='_annot_p{page:02d}.png'></div>")
    parts.append("</body></html>")
    out_html.write_text("".join(parts), encoding="utf-8")
    log.info("核对可视化已写出: %s", out_html)


# ============================================================
# 聚焦归纳：按「大量表」逐个精确归纳(修正归属 + 正确切分子量表)
# ============================================================
# 21 个大量表 → (所在源页, 内容特征提示)。源页/特征基于内容分析与领域结构。
SCALE_LAYOUT: list[tuple[str, list[int], str]] = [
    ("一", [3, 4, 5], "儿童归因风格问卷：若干个情景题(如'你参加考试拿到差分')，每个情景下有"
                      "若干归因维度问题(选项1-7)。每个情景是一个子量表。"),
    ("二", [5], "「二、请认真阅读…打√」量表(选项1-4)。"),
    ("三", [6], "短视频使用：1个主体量表(如'我会花时间思考刷短视频') + 2个字问题(每天频率、单次时长)。"),
    ("四", [6], "「四、」量表(如'我猜不出他人的想法'，选项1-7)。"),
    ("五", [7, 8], "父母养育方式(EMBU/s-EMBU)：关于父亲/母亲养育方式，跨源页7-8。源页8是题13-21延续。"),
    ("六", [9], "「六、」量表(选项1-5)。"),
    ("七", [9], "「七、」量表(选项0-4)。"),
    ("八", [10, 11], "非自杀性自伤：主体(是否实施自伤) + 原因字问题(为什么自伤)。跨源页10-11。"),
    ("九", [11], "「九、」本学期在学校被同学以各种方式欺负(选项1-5)。"),
    ("十", [12], "「十、」本学期在学校以各种方式欺负同学(选项1-5)。"),
    ("十一", [12], "「十一、请认真阅读…打√」量表(选项1-4)。"),
    ("十二", [13], "「十二、」过去两周1-10分评分(选项1-10)。"),
    ("十三", [14], "「十三、」最近两周事件发生频率(选项1-4)。"),
    ("十四", [14], "「十四、」成长经历/家庭暴力频率(选项0-4)。"),
    ("十五", [15, 16], "PTSS/儿童创伤后应激：2个子量表——「问题部分」(困扰程度) +「影响部分」"
                       "(有没有干扰影响到你的生活)。跨源页15-16。"),
    ("十六", [16], "「十六、」最近两周事件频率(选项1-4)。"),
    ("十七", [17], "「十七、」心理意象生动性：闭眼呈现画面(如太阳升起/商店/山水)并评分。"),
    ("十八", [18, 19], "VVIQ视觉意象鲜明性：2个子量表——「部分一」(回忆过去场景) +「部分二」"
                       "(想象未来场景)。跨源页18-19。"),
    ("十九", [19], "「十九、请认真阅读…打√」量表(选项1-5)。"),
    ("二十", [20], "「二十、请认真阅读…打√」量表(选项1-5)。"),
    ("二十一", [20], "「二十一、」韧性/应对方式量表(选项1-4)。"),
]
FOCUSED_PROMPT_VERSION = "fv1"

SYS_INDUCT_GROUP = (
    "你是问卷版式分析助手。这是问卷【量表{group}】所在的【一页】图片。该量表特征：{hint}\n"
    "注意：本页可能还含有其他量表，你【只需识别量表{group}】在本页的部分，完全忽略其他量表。\n\n"
    "请精确识别量表{group}在本页的作答单元(主体/子量表/字问题)：\n"
    "- 若该量表在本页是单一连续题组，输出1个单元；\n"
    "- 若它含多个子量表/字问题(如主体+字问题、部分一/部分二、多个情景)，分别输出各1个单元；\n"
    "- 【绝不要】把单道题作为独立单元；同一小节的多道题必须合并为1个单元。\n\n"
    "对每个单元输出：\n"
    "1) is_sub: 是否子量表/字问题(true/false)，主体填false\n"
    "2) title: 本单元标题或首题文本(逐字照抄)\n"
    "3) n_items: 本单元【在本页】的题目数\n"
    "4) option_range: 选项数值范围如[1,5]，无固定数值填null\n"
    "5) answer_style: check|circle|fill|hatch|mixed\n"
    "6) bbox: 本单元在本页边界框，【归一化0~1】坐标[x1,y1,x2,y2]，必须是0-1小数，禁止像素值\n"
    "7) first_q/last_q: 本页题号起止(无显式题号则1..n_items)\n\n"
    "严格输出JSON：{{\"units\":[{{...}}]}}。量表{group}在本页无内容就返回 {{\"units\":[]}}。不要编造。"
)


def induct_group_on_page(img: Image.Image, client: VisionLLMClient,
                         group: str, hint: str, retries: int = 1) -> list[RawScale]:
    """对单页聚焦识别【指定大量表group】在该页的作答单元，强制归属group。"""
    w, h = img.size
    sys_p = SYS_INDUCT_GROUP.format(group=group, hint=hint)
    for attempt in range(retries + 1):
        try:
            r = client.ask_image(_img_bytes(img), sys_p,
                                 f"识别量表{group}在本页的作答单元，严格输出JSON(units)。")
            units = (r.raw_json or {}).get("units") or []
            out = _parse_scales_raw(units, w, h)
            for u in out:
                u.group = group          # 强制正确归属
                if not u.title:
                    u.title = f"量表{group}"
            return out
        except LLMError as e:
            log.warning("量表%s聚焦归纳失败(%d): %s", group, attempt + 1, e)
            if attempt == retries:
                return []
    return []


def induct_group_on_page_cached(img: Image.Image, client: VisionLLMClient,
                                group: str, hint: str, cache_path: Path,
                                key: str, retries: int = 1) -> list[RawScale]:
    """带缓存的聚焦归纳。key 应含 prompt 版本，变更时缓存失效。"""
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data.get("key") == key and isinstance(data.get("units"), list):
                w, h = img.size
                out = _parse_scales_raw(data["units"], w, h)
                for u in out:
                    u.group = group
                return out
        except (json.JSONDecodeError, OSError):
            pass
    rs = induct_group_on_page(img, client, group, hint, retries)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"key": key, "units": [_raw_to_obj(r) for r in rs]},
            ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("写缓存失败 %s: %s", cache_path, e)
    return rs


def reconcile_focused(per_group_page: dict[tuple[str, int], list[list[RawScale]]]
                      ) -> list[ScaleMeta]:
    """聚焦归纳结果的跨样本投票。

    per_group_page: {(group, src_page): [ [样本0的units], [样本1...], ... ]}
    """
    metas: list[ScaleMeta] = []
    sid_counter = 0
    # 按源页为主、group为次排序，保持页面顺序
    for (group, page), sample_lists in sorted(per_group_page.items(),
                                              key=lambda kv: (kv[0][1], _group_rank(kv[0][0]))):
        n_per = [len(s) for s in sample_lists]
        if not n_per:
            log.warning("量表%s 源页%d 无样本结果，跳过", group, page)
            continue
        # 单元数投票：0 通常代表漏识别(而非真的无内容)，优先取非零众数，
        # 仅当所有样本都为0时才判为无内容。
        nonzero = [n for n in n_per if n > 0]
        if nonzero:
            n_units = Counter(nonzero).most_common(1)[0][0]
        else:
            n_units = 0
        for slot in range(n_units):
            cands = [s[slot] for s in sample_lists if len(s) > slot]
            if not cands:
                continue
            n_items = _vote([c.n_items for c in cands]) or 0
            style = _vote([c.answer_style for c in cands]) or "fill"
            opt = _vote([c.option_range for c in cands])
            first_q = _vote([c.first_q for c in cands]) or 1
            last_q = first_q + n_items - 1
            is_sub = _vote([c.is_sub for c in cands]) or False
            title = Counter([c.title for c in cands if c.title]).most_common(1)
            title = title[0][0] if title else f"量表{group}"
            # bbox 用中位数(而非均值)，抵抗个别样本拍摄偏移导致的整体偏移
            bboxes = [c.bbox for c in cands if len(c.bbox) == 4]
            bbox = [statistics.median([b[i] for b in bboxes]) for i in range(4)] \
                if bboxes else None
            sid_counter += 1
            metas.append(ScaleMeta(
                id=f"S{sid_counter:02d}", title=title, source_pages=[page],
                n_items=n_items, answer_style=style,
                option_range=list(opt) if isinstance(opt, tuple) else opt,
                first_q=first_q, last_q=last_q, bbox_nominal=bbox, bbox_page=page,
                notes=f"聚焦归纳自{len(cands)}份样本(量表{group}源页{page})",
                group=group, is_sub=bool(is_sub),
            ))
    _assign_unit_labels(metas)
    metas = _sort_and_renumber(metas)
    n_groups = len({m.group for m in metas if m.group})
    log.info("聚焦归纳完成: 共 %d 个作答单元，覆盖 %d 个大量表。",
             len(metas), n_groups)
    log.info("大量表分组: %s", _group_summary(metas))
    return metas
