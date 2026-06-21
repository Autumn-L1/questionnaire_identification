"""headful 验证：打开问卷星，自动填 1 份样本，保留窗口供人工核对 + 手动提交。
用法: python wjx_fill_one.py [样本序号0-4] [录入人]
例如: python wjx_fill_one.py 0 yap
"""
import sys, csv, logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
from src import wjx_import

idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
recorder = sys.argv[2] if len(sys) > 2 else "default"

rows = list(csv.DictReader(open("out/results.csv", encoding="utf-8-sig")))
row = rows[idx]
print(f"填写样本 {idx}: subject_id={row.get('subject_id')} name={row.get('name')}")

mp = wjx_import.load_mapping()
p, b, pg = wjx_import.open_wjx(mp["wjx_url"], headless=False)  # headful 弹窗
filled, skipped = wjx_import.fill_one(pg, row, mp, recorder=recorder)
print(f"\n已自动填 {len(filled)} 项，跳过 {len(skipped)}: {skipped}")
print("\n>>> 请在弹出的浏览器窗口中：")
print("    1) 滚动核对各题填写是否正确")
print("    2) 补填跳过的题（如需要）")
print("    3) 手动点击问卷星「提交」按钮")
print("    4) 确认提交成功后，回到这里按回车关闭浏览器\n")
input("按回车关闭浏览器...")
b.close(); p.stop()
