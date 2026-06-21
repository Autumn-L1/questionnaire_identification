"""复核日志：运行日志 + 结构化 issues.jsonl + 复核包(异常图+原始响应)。

- ``logs/run_{ts}.log``      标准日志(INFO+)，含阶段耗时、token 用量。
- ``logs/issues_{ts}.jsonl`` 每行一个 issue，便于程序化筛选。
- ``work/review/``           每个 ERROR 附原图裁剪 + 同名 .json(原始 LLM 响应/校验上下文)，
                              人工只需扫这个目录。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def setup_logging(log_dir: str | Path, ts: str | None = None) -> str:
    """配置全局 logging，返回本次 run 的时间戳前缀(用于日志/复核文件名)。"""
    ts = ts or time.strftime("%Y%m%d_%H%M%S")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{ts}.log"
    root = logging.getLogger()
    # 清理可能重复的 handler(多次 run)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            "%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    root.setLevel(logging.INFO)
    logging.getLogger("qr").setLevel(logging.INFO)
    return ts


@dataclass
class Issue:
    subject_id: str
    scope: str            # 如 'page1'/'page2'/'scale:S02'/'pdf'/'preprocess'
    severity: str         # ERROR | WARN | INFO
    code: str             # 如 'out_of_range'/'missing_item'/'parse_fail'
    msg: str
    image: str = ""       # 复核图路径(若已存档)
    ctx: dict[str, Any] = field(default_factory=dict)


class ReviewLogger:
    def __init__(self, log_dir: str | Path, review_dir: str | Path, ts: str):
        self.log_dir = Path(log_dir)
        self.review_dir = Path(review_dir)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.ts = ts
        self.issues_path = self.log_dir / f"issues_{ts}.jsonl"
        self.log = logging.getLogger("qr.review")
        self._error_count = 0
        self._warn_count = 0

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def warn_count(self) -> int:
        return self._warn_count

    def issue(self, issue: Issue) -> None:
        """记录一条 issue，并写入 jsonl。"""
        if issue.severity == "ERROR":
            self._error_count += 1
            self.log.error("[%s/%s] %s: %s", issue.subject_id, issue.scope,
                           issue.code, issue.msg)
        elif issue.severity == "WARN":
            self._warn_count += 1
            self.log.warning("[%s/%s] %s: %s", issue.subject_id, issue.scope,
                             issue.code, issue.msg)
        else:
            self.log.info("[%s/%s] %s: %s", issue.subject_id, issue.scope,
                          issue.code, issue.msg)
        with open(self.issues_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(issue), ensure_ascii=False) + "\n")

    def save_review_image(self, subject_id: str, scope: str,
                          image_bytes: bytes, payload: dict | None = None) -> str:
        """把异常裁剪图与原始响应存入复核包，返回图片路径。"""
        base = f"{subject_id}_{scope}".replace("/", "_").replace(":", "_")
        img_path = self.review_dir / f"{base}.png"
        with open(img_path, "wb") as f:
            f.write(image_bytes)
        if payload is not None:
            with open(self.review_dir / f"{base}.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        return str(img_path)

    def summary(self) -> str:
        return (f"复核统计: ERROR={self._error_count} WARN={self._warn_count}; "
                f"详见 {self.issues_path} 与复核包 {self.review_dir}")
