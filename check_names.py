"""对比 results.csv 的 name 与 names.txt 名单，指出异常（缺失/重复/不存在）。

用法:
  python check_names.py              # 默认初一/10班
  python check_names.py 初一/10      # 指定班级
  python check_names.py 初二/03      # 其他班
"""
import csv
import sys
import difflib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
grade_class = sys.argv[1] if len(sys.argv) > 1 else "初一/10"
grade, cls = grade_class.split("/")

# 读名单
names_set = set()
for line in open(ROOT / "names.txt", encoding="utf-8"):
    p = line.strip().split("/")
    if len(p) >= 3 and p[0] == grade and p[1] == cls:
        names_set.add(p[2])

# 读数据
rows = list(csv.DictReader(open(ROOT / "out" / "results.csv", encoding="utf-8-sig")))
data_names = [r["name"].strip() for r in rows if r.get("name", "").strip()]

print(f"名单 {grade_class}: {len(names_set)}人 | 数据: {len(data_names)}个\n")

# 1. 数据中不存在于名单（疑似匹配错误/OCR误差）
not_in_list = [n for n in data_names if n not in names_set]
print(f"=== 数据中不存在于名单（{len(not_in_list)}个，疑似匹配错误）===")
for n in not_in_list:
    close = difflib.get_close_matches(n, names_set, n=1, cutoff=0.4)
    print(f"  {n}  →  建议: {close[0] if close else '无匹配，需人工核实'}")

# 2. 重复
dups = {n: c for n, c in Counter(data_names).items() if c > 1}
print(f"\n=== 数据中重复（{len(dups)}个）===")
for n, c in dups.items():
    print(f"  {n}: {c}次")
if not dups:
    print("  无重复")

# 3. 名单中缺失（名单有但数据没有=未识别/未提交）
missing = names_set - set(data_names)
print(f"\n=== 名单中缺失（{len(missing)}人未在数据中）===")
for n in sorted(missing):
    print(f"  {n}")
