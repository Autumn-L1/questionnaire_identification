"""第 1 页(编号/姓名/调查时间) 与 第 2 页(17 题混题) 识别。

策略：
- 第 1 页分别裁「左上角编号」「右下角签名/日期」两个 ROI 各送一次模型，
  缩小视野、降低幻觉。失败时回退为整页问。
- 第 2 页整页送一次，要求严格 17 题 JSON。
所有结果走 validate 校验，ERROR 级另存裁剪图入复核包。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image

from .llm_client import VisionLLMClient, LLMError
from .validate import Page1Result, Page2Result
from .logger import ReviewLogger

log = logging.getLogger("qr.page12")

# ROI 用相对坐标(左,上,右,下)，归一化到 0-1。
ROI_P1_ID = (0.0, 0.0, 0.32, 0.16)        # 左上角红色编号
ROI_P1_SIG = (0.45, 0.80, 1.0, 1.0)       # 右下角签名 + 调查时间


def _crop_rel(img: Image.Image, box_rel: tuple[float, float, float, float]
              ) -> bytes:
    w, h = img.size
    l, t, r, b = box_rel
    crop = img.crop((int(l * w), int(t * h), int(r * w), int(b * h)))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _img_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mode_nonempty(values: list[str]) -> str:
    """取非空值中的众数(平票取首个)；用于多次识别投票抗不稳定。"""
    from collections import Counter
    vs = [v for v in values if v and v.lower() not in ("null", "none")]
    if not vs:
        return ""
    return Counter(vs).most_common(1)[0][0]


SYS_P1_ID = (
    "你是问卷识别助手。图中是问卷第一页左上角的局部裁剪。"
    "请只识别左上角的红色编号(通常是2位数字)。"
    "严格输出JSON：{\"subject_id\":\"两位数字\", \"confidence\":0~1}。"
    "看不清就填 null。不要输出任何解释。"
)

SYS_P1_SIG = (
    "你是手写文字识别专家。图中是问卷第一页右下角的局部裁剪，含手写的「参加者签名(姓名)」"
    "和「调查时间(日期)」。请极其仔细地逐字辨认手写内容(可能连笔/潦草)：\n"
    "- 姓名：逐字辨认手写中文姓名，不要凭印象替换为字形相近的常见字。\n"
    "- 日期：年份是四位数字，逐位仔细辨认(本调查约在 2024-2026 年；警惕把 6 误读为 1/0、把 0 误读为 6)；月、日也逐位辨认。\n"
    "严格输出JSON：{\"name\":\"姓名或null\", \"date\":\"日期原文如 2026年6月7日 或 null\", "
    "\"confidence\":0~1}。看不清对应字段就填 null，不要猜测。不输出解释。"
)

SYS_P2 = (
    "你是问卷识别助手。图中是问卷第2页，共17道题，包含填空题与选择题(混合)。"
    "请按题号1~17识别每题的作答内容：选择题填选中项(数字/字母或选项文字)，填空题填手写文字。\n"
    "【重要·选择题固定选项】以下选择题必须从给定选项中选(逐字照抄选项原文，看不清填null)：\n"
    " q2性别: 男 / 女\n"
    " q7独生子女: 是 / 否(否时后跟数量，如\"否，兄弟姐妹几个：4\")\n"
    " q8父母婚姻: 已婚 / 离婚 / 丧偶 / 再婚 / 其他\n"
    " q9每天刷短视频时间: 30分钟以下 / 30-60分钟 / 1-2小时 / 2小时以上\n"
    " q10住校走读: 走读 / 住校\n"
    " q12分开居住: 是 / 否\n"
    " q14生活水平: 低 / 中等偏下 / 中等 / 中等偏上 / 高\n"
    " q15父亲文化: 小学及以下 / 初中 / 高中（或中专、技校） / 本科或大专 / 研究生及以上 / 不清楚\n"
    " q16母亲文化: 同父亲文化选项\n"
    "切勿把选项识别错(如把\"2小时以上\"误为\"2-1小时以上\")，逐字核对。\n"
    "【重要·混题】若某题是「选择 + 数量填空」结构(如兄弟姐妹情况：先选有/无，后填具体数量/人数)，"
    "必须把选择项和填写的数量都输出(如 \"是，哥哥1\"、\"否\" 等)，绝不能只输出选择而漏掉数量。\n"
    "【重要·空题】完全未作答/空白的题填 null，不要猜测或默认填值。\n"
    "严格输出JSON：{\"answers\":{\"1\":\"...\",\"2\":\"...\",...,\"17\":\"...\"}, "
    "\"issues\":[{\"q\":题号,\"type\":\"unanswered|ambiguous\",\"note\":\"...\"}], "
    "\"confidence\":0~1}。题号必须是1~17连续整数。不输出解释。"
)


def recognize_page1(img: Image.Image, client: VisionLLMClient,
                    review: ReviewLogger | None, subject_id_hint: str,
                    max_retries: int = 1) -> Page1Result:
    res = Page1Result()
    # —— 编号 ——
    id_bytes = _crop_rel(img, ROI_P1_ID)
    for attempt in range(max_retries + 1):
        try:
            r = client.ask_image(id_bytes, SYS_P1_ID, "识别左上角红色2位编号。")
            d = r.raw_json or {}
            res.subject_id = d.get("subject_id")
            try:
                res.confidence = float(d.get("confidence", 1.0))
            except (TypeError, ValueError):
                res.confidence = 1.0
            break
        except LLMError as e:
            log.warning("第1页编号识别失败(%d): %s", attempt + 1, e)
            if attempt == max_retries:
                res.issues_payload.append({"type": "id_call_fail", "note": str(e)})

    # —— 签名/日期(多次识别取众数，抗手写 OCR 不稳定) ——
    sig_bytes = _crop_rel(img, ROI_P1_SIG)
    names, dates, confs = [], [], []
    for _ in range(3):
        try:
            r = client.ask_image(sig_bytes, SYS_P1_SIG, "仔细辨认右下角手写姓名与调查时间。")
            d = r.raw_json or {}
            names.append(str(d.get("name") or "").strip())
            dates.append(str(d.get("date") or "").strip())
            try:
                confs.append(float(d.get("confidence", 1.0)))
            except (TypeError, ValueError):
                confs.append(1.0)
        except LLMError as e:
            log.warning("第1页签名/日期识别失败: %s", e)
            break
    res.name = _mode_nonempty(names) or None
    res.date = _mode_nonempty(dates) or None
    sig_conf = sum(confs) / len(confs) if confs else 1.0
    if not names:
        res.issues_payload.append({"type": "sig_call_fail", "note": "签名/日期识别全部失败"})

    res.confidence = min(res.confidence, sig_conf)
    res.raw = {"subject_id": res.subject_id, "name": res.name, "date": res.date}

    # 兜底：局部识别有缺失(编号/姓名/调查时间任一为空) → 整页补充识别。
    # 整页视野更清晰，能纠正 ROI 裁剪导致的 OCR 错误(如年份)与漏识手写姓名。
    _miss = (not (res.subject_id or "").strip()
             or not (res.name or "").strip()
             or not (res.date or "").strip())
    if _miss:
        log.info("第1页局部识别有缺失，回退整页识别补充。")
        try:
            r = client.ask_image(_img_bytes(img),
                                 "图中是问卷第一页。识别左上角红色2位编号、右下角姓名(参加者签名处手写文字)、"
                                 "调查时间(注意年份)。严格输出JSON:{\"subject_id\":\"\",\"name\":\"\","
                                 "\"date\":\"\",\"confidence\":0~1}。",
                                 "识别编号/姓名/调查时间。")
            d = r.raw_json or {}
            res.subject_id = (res.subject_id or "").strip() or d.get("subject_id")
            res.name = (res.name or "").strip() or d.get("name")
            res.date = (res.date or "").strip() or d.get("date")
            res.raw = d
        except LLMError as e:
            res.issues_payload.append({"type": "whole_call_fail", "note": str(e)})

    return res


def recognize_page2(img: Image.Image, client: VisionLLMClient,
                    review: ReviewLogger | None, subject_id: str,
                    expected: int = 17, max_retries: int = 1) -> Page2Result:
    res = Page2Result()
    img_bytes = _img_bytes(img)
    for attempt in range(max_retries + 1):
        try:
            r = client.ask_image(img_bytes, SYS_P2,
                                 f"识别第2页全部{expected}题答案。")
            d = r.raw_json or {}
            res.answers = {str(k): (v if v is not None else None)
                           for k, v in (d.get("answers") or {}).items()}
            res.issues_payload = d.get("issues") or []
            try:
                res.confidence = float(d.get("confidence", 1.0))
            except (TypeError, ValueError):
                res.confidence = 1.0
            res.raw = d
            break
        except LLMError as e:
            log.warning("第2页识别失败(%d): %s", attempt + 1, e)
            if attempt == max_retries:
                res.issues_payload.append({"type": "call_fail", "note": str(e)})
    return res
