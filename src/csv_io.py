"""CSV 读写：每个被试一行，列按固定模板顺序。"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable


def write_csv(rows: list[dict], fieldnames: list[str], out_path: str | Path,
              null_token: str = "") -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        # utf-8-sig 便于 Excel 直接打开不乱码
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {k: ("" if row.get(k) is None else row.get(k)) for k in fieldnames}
            # null_token 占位
            clean = {k: (null_token if v is None else v) for k, v in clean.items()}
            writer.writerow(clean)


def append_csv(row: dict, fieldnames: list[str], out_path: str | Path,
               null_token: str = "", write_header_if_new: bool = True) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not out_path.exists()
    with open(out_path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if is_new and write_header_if_new:
            writer.writeheader()
        clean = {k: row.get(k) for k in fieldnames}
        clean = {k: (null_token if v is None else v) for k, v in clean.items()}
        writer.writerow(clean)


def read_csv(in_path: str | Path) -> tuple[list[str], list[dict]]:
    in_path = Path(in_path)
    with open(in_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows
