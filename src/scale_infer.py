"""单量表识别：受控 prompt + 严格 JSON 输出。

元数据(题数/选项/作答方式/题号范围)直接喂给模型，模型只负责「读数」，
不需要理解版式——这是稳健性核心。越界/未答填 null 并写 issues。
"""
from __future__ import annotations

import logging

from .config_schema import ScaleMeta
from .llm_client import VisionLLMClient, LLMError
from .validate import ScaleResult

log = logging.getLogger("qr.scale")


_STYLE_HINT = {
    "circle": "作答方式=圈选：填被圈起来的那个数字。",
    "check": "作答方式=打勾/勾选：填被勾选的那一项对应的数字或选项值。",
    "hatch": "作答方式=涂黑方框：填被涂黑方框对应的数字/选项值。",
    "fill": "作答方式=手写填空：逐题转录手写文字内容。",
    "mixed": "作答方式=混合：选择题填选中项值，填空题转录手写文字。",
}


def _loc_hint(meta: ScaleMeta) -> str:
    """根据 bbox 生成位置提示，帮助模型在整页中定位量表(仅参考，非精确)。"""
    if not meta.bbox_nominal:
        return "本页可能只有一个量表，约占满整页"
    x1, y1, x2, y2 = meta.bbox_nominal
    cy, cx = (y1 + y2) / 2, (x1 + x2) / 2
    vb = "上" if cy < 0.34 else ("中" if cy < 0.67 else "下")
    hs = "左" if cx < 0.34 else ("中" if cx < 0.67 else "右")
    return (f"页面{vb}部偏{hs}侧(参考区域 纵向y≈{y1:.2f}-{y2:.2f}，"
            f"横向x≈{x1:.2f}-{x2:.2f})")


def _system_prompt(meta: ScaleMeta) -> str:
    opt = f"选项数值范围{meta.option_range[0]}~{meta.option_range[1]}，超出范围的值无效填null" \
        if meta.option_range else "无固定数值选项"
    if meta.sub_keys:
        dims = "、".join(meta.sub_keys)
        opt += (f"。本量表每题区分多个维度[{dims}]：每题对每个维度各选一项，"
                f"answers 每题返回一个对象 {{{', '.join(f'\"{k}\":值' for k in meta.sub_keys)}}}；"
                f"某维度未作答则该维度填 null。")
    return (
        "你是问卷识别助手。给你一张问卷的【整页】图片及某个量表的元数据。"
        "请在本页中【定位】该量表(以其标题和题号为锚)，只识别它的答案，"
        "完全忽略本页其它量表。\n"
        f"待识别量表：{meta.title}。共{meta.n_items}题，题号{meta.first_q}~{meta.last_q}。{opt}。\n"
        f"位置参考：{_loc_hint(meta)}(仅供参考，最终以标题和题号为准)。\n"
        f"{_STYLE_HINT.get(meta.answer_style, '')}\n"
        "规则：1) 只输出JSON，不解释。2) answers 的 key 必须是题号(连续整数)。"
        "3) 只有当某题【确实有作答标记】(勾选/圈选/涂黑/手写填写)时才填该值；"
        "完全空白、未勾选、看不出选择的题必须填 null，绝不默认填1或猜测。"
        "看不清/越界的也填 null。"
        "4) 若实际题数与元数据不符，把差异写进 issues，不要编造答案。"
        "5) 同时输出整体置信度 confidence(0~1)。\n"
        "JSON格式：{\"answers\":{\"1\":值,...},\"issues\":[{\"q\":题号,\"type\":"
        "\"unanswered|out_of_range|ambiguous|missing_item\",\"note\":\"...\"}],\"confidence\":0~1}"
    )


def infer_scale(image_bytes: bytes, meta: ScaleMeta, client: VisionLLMClient,
                max_retries: int = 1) -> ScaleResult:
    res = ScaleResult(scale_id=meta.id)
    sys_prompt = _system_prompt(meta)
    for attempt in range(max_retries + 1):
        try:
            r = client.ask_image(image_bytes, sys_prompt,
                                 f"识别量表 {meta.id}({meta.title})的{meta.n_items}题答案。")
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
            log.warning("量表 %s 识别失败(%d): %s", meta.id, attempt + 1, e)
            if attempt == max_retries:
                res.issues_payload.append({"type": "call_fail", "note": str(e)})
    return res
