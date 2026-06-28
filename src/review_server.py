"""识别结果可视化核对前端(标准库 HTTP 服务，零额外依赖)。

启动: python -m src.review_server  → 浏览器打开 http://localhost:8000

功能:
  - 左侧原图(可切换源页) + 右侧可编辑识别结果(按量表分组)
  - 点击量表组标题跳转到对应源页，便于对照
  - 修改单元格后点「保存修改」回写 out/results.csv
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from .config_schema import load_scales
except ImportError:
    # 支持 python src/review_server.py 直接运行
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config_schema import load_scales

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "out" / "results.csv"
SCALES_YAML = ROOT / "config" / "scales.yaml"
WORK = ROOT / "work"
STATIC = ROOT / "static"
REVIEW_STATE = WORK / "review_state.json"   # 样本核对状态持久化 {sample_idx: bool}
ISSUE_RESOLVED = WORK / "issue_resolved.json"  # 日志问题解决状态 {issue_key: bool}
WJX_STATUS = WORK / "wjx_import_status.json"   # 问卷星导入进度
WJX_SUBMIT_STATE = WORK / "wjx_submit_state.json"  # 每样本提交状态 {idx: submitted/error}
DATA_DIR = ROOT / "data"
INCREMENT_STATUS = WORK / "increment_status.json"
BACKUP_DIR = WORK / "backups"
TRASH_DIR = ROOT / "trash"   # 删除样本回收站
PORT = 8000
log = logging.getLogger("qr.review")
CAPTCHA_EVENT = threading.Event()   # 无头遇验证码→有头人工干预完成信号


def _do_backup() -> str:
    """备份 results.csv 到 backups/results_{时间戳}.csv，设只读防误改。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"results_{ts}.csv"
    if not RESULTS.exists():
        raise FileNotFoundError("results.csv 不存在")
    shutil.copy2(RESULTS, dst)
    try:
        os.chmod(dst, 0o444)  # 只读：前后端均不可修改，防数据丢失
    except OSError:
        pass
    log.info("[backup] 已备份 %s", dst.name)
    return dst.name


def _list_backups() -> list:
    if not BACKUP_DIR.exists():
        return []
    return sorted([f.name for f in BACKUP_DIR.glob("results_*.csv")], reverse=True)


def _do_restore(name: str) -> bool:
    """从只读备份恢复到 results.csv（备份本身不被修改）。"""
    src = BACKUP_DIR / name
    if not src.exists() or not src.is_file() or not name.endswith(".csv"):
        return False
    with open(src, "rb") as f:
        data = f.read()
    with open(RESULTS, "wb") as f:
        f.write(data)
    log.info("[restore] 从 %s 恢复 results.csv", name)
    return True


def _delete_sample(idx: int):
    """删除样本：移 pdf + work 目录到 trash/，删 results.csv 行，存行到 trash/deleted.jsonl。"""
    import shutil
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    cols, rows = _read_results()
    if not (0 <= idx < len(rows)):
        return False, "索引无效"
    row = rows[idx]
    row_dict = dict(zip(cols, row))  # list → dict
    sid = row_dict.get("subject_id", str(idx))
    # 存删除行（供恢复）
    with open(TRASH_DIR / "deleted_rows.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row_dict, ensure_ascii=False) + "\n")
    # 移 work 目录
    dirs = _sample_dirs()
    if idx < len(dirs) and dirs[idx].exists():
        shutil.move(str(dirs[idx]), str(TRASH_DIR / dirs[idx].name))
    # 移 pdf（匹配 stem[:24]）
    if idx < len(dirs):
        stem24 = dirs[idx].name
        for pdf in DATA_DIR.glob("*.pdf"):
            if pdf.stem[:24] == stem24:
                shutil.move(str(pdf), str(TRASH_DIR / pdf.name))
                break
    # 删 results.csv 行
    rows.pop(idx)
    with open(RESULTS, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    # 调整核对/提交状态（删 idx 后，>idx 的 key 减 1）
    for sf in [REVIEW_STATE, WJX_SUBMIT_STATE]:
        if sf.exists():
            try:
                st = json.loads(sf.read_text(encoding="utf-8"))
                new_st = {}
                for k, v in st.items():
                    ki = int(k)
                    if ki == idx:
                        continue  # 删除的样本状态丢弃
                    new_st[str(ki - 1 if ki > idx else ki)] = v
                sf.write_text(json.dumps(new_st, ensure_ascii=False), encoding="utf-8")
            except (json.JSONDecodeError, ValueError, OSError):
                pass
    log.info("[delete] 删除样本 %s(idx=%d)", sid, idx)
    return True, sid


def _trash_list() -> list:
    """列回收站里的样本（从 deleted.jsonl）。"""
    out = []
    p = TRASH_DIR / "deleted_rows.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    d = json.loads(line)
                    out.append({"subject_id": d.get("subject_id", ""), "name": d.get("name", "")})
                except json.JSONDecodeError:
                    pass
    return out


def _restore_sample(subject_id: str):
    """从回收站恢复：行加回 results.csv，移 pdf/work 回原位。"""
    import shutil
    p = TRASH_DIR / "deleted_rows.jsonl"
    if not p.exists():
        return False, "无回收站记录"
    lines = p.read_text(encoding="utf-8").splitlines()
    restored = None
    remaining = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not restored and d.get("subject_id") == subject_id:
            restored = d
        else:
            remaining.append(line)
    if not restored:
        return False, "回收站无此样本"
    # 行加回 results.csv
    cols, rows = _read_results()
    rows.append([restored.get(c, "") for c in cols])
    with open(RESULTS, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    # 移 pdf/work 回（按 subject_id/stem 匹配 trash 文件）
    for item in TRASH_DIR.iterdir():
        if item.is_dir() and item.name.startswith("_induct") is False:
            dst = WORK / item.name
            if not dst.exists():
                shutil.move(str(item), str(dst))
    for pdf in TRASH_DIR.glob("*.pdf"):
        dst = DATA_DIR / pdf.name
        if not dst.exists():
            shutil.move(str(pdf), str(dst))
    # 更新 deleted.jsonl
    p.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
    log.info("[restore] 恢复样本 %s", subject_id)
    return True, subject_id


def _scan_pdfs() -> list:
    """扫描 data/ 下 PDF，标记是否已识别（work 下有对应 pages_proc 目录）。"""
    out = []
    if not DATA_DIR.exists():
        return out
    for pdf in sorted(DATA_DIR.glob("*.pdf")):
        stem = pdf.stem[:24]
        identified = any((WORK / d / "pages_proc").exists() for d in [stem, f"_induct_{stem}"])
        out.append({"name": pdf.name, "stem": stem, "identified": identified})
    return out


def _read_increment_status() -> dict:
    if INCREMENT_STATUS.exists():
        try:
            return json.loads(INCREMENT_STATUS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"running": False, "total": 0, "done": 0, "current": None, "results": []}


def _write_increment_status(st: dict) -> None:
    try:
        INCREMENT_STATUS.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _increment_run(pdfs: list[str]):
    """后台线程：增量识别指定 PDF，追加到 results.csv。"""
    from . import run as runmod, llm_client, config_schema, logger
    st = {"running": True, "total": len(pdfs), "done": 0, "current": None, "results": []}
    _write_increment_status(st)
    try:
        cfg = config_schema.load_config(str(ROOT / "config" / "config.yaml"))
        scales = config_schema.load_scales(str(SCALES_YAML))
        client = llm_client.VisionLLMClient(cfg.llm)
        ts = logger.setup_logging(cfg.paths.log_dir)
        for name in pdfs:
            path = DATA_DIR / name
            if not path.exists():
                st["results"].append({"pdf": name, "error": "文件不存在"})
                st["done"] += 1
                _write_increment_status(st)
                continue
            st["current"] = name
            _write_increment_status(st)
            review = logger.ReviewLogger(cfg.paths.log_dir, cfg.paths.review_dir, ts)
            fallback_id = path.stem[:24]
            try:
                row = runmod.infer_one(path, cfg, scales, review, client,
                                       fallback_id=fallback_id, append_to=RESULTS)
                st["results"].append({"pdf": name, "ok": True,
                                      "subject_id": (row or {}).get("subject_id", "")})
                log.info("[incr] %s 识别完成 subject_id=%s", name, (row or {}).get("subject_id"))
            except Exception as e:
                st["results"].append({"pdf": name, "error": str(e)[:200]})
            st["done"] += 1
            _write_increment_status(st)
    except Exception as e:
        st["results"].append({"pdf": "-", "error": "运行异常: " + str(e)[:200]})
    finally:
        st["running"] = False
        st["current"] = None
        _write_increment_status(st)


def _load_submit_state() -> dict:
    if WJX_SUBMIT_STATE.exists():
        try:
            return json.loads(WJX_SUBMIT_STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_submit_state(st: dict) -> None:
    try:
        WJX_SUBMIT_STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _get_wjx_url() -> str:
    import yaml
    try:
        return (yaml.safe_load((ROOT / "config" / "wjx_mapping.yaml").read_text(encoding="utf-8"))
                or {}).get("wjx_url", "")
    except Exception:
        return ""


def _set_wjx_url(url: str) -> bool:
    import yaml
    p = ROOT / "config" / "wjx_mapping.yaml"
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d["wjx_url"] = url.strip()
        p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return True
    except Exception as e:
        log.warning("保存问卷星链接失败: %s", e)
        return False


def _read_wjx_status() -> dict:
    if WJX_STATUS.exists():
        try:
            return json.loads(WJX_STATUS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"running": False, "total": 0, "done": 0, "current": None, "results": []}


def _write_wjx_status(st: dict) -> None:
    try:
        WJX_STATUS.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _wjx_run(samples: list[int], recorder: str, submit: bool, headless: bool):
    """后台线程：逐样本打开问卷星填写(+提交)。写进度到 WJX_STATUS。"""
    import csv
    from . import wjx_import
    st = {"running": True, "total": len(samples), "done": 0, "current": None,
          "results": [], "recorder": recorder}
    _write_wjx_status(st)
    rows = list(csv.DictReader(open(RESULTS, encoding="utf-8-sig")))
    mp = wjx_import.load_mapping()
    url = mp["wjx_url"]
    try:
        p, b, pg = wjx_import.open_wjx(url, headless=headless)
        for idx in samples:
            if idx >= len(rows):
                continue
            row = rows[idx]
            st["current"] = {"idx": idx, "subject_id": row.get("subject_id", ""), "stage": "填写中"}
            _write_wjx_status(st)
            try:
                pg.goto(url, wait_until="domcontentloaded")
                pg.wait_for_timeout(2500)
                filled, skipped = wjx_import.fill_one(pg, row, mp, recorder)
                sub = ""
                if submit:
                    st["current"]["stage"] = "提交中"
                    _write_wjx_status(st)
                    # headless 提交多被反爬拦，用短超时避免长时间卡住进度条
                    sub = wjx_import.submit_one(
                        pg, timeout=6 if headless else 15,
                        debug_prefix=str(WORK / f"wjx_debug_sample{idx}"))
                    if sub == "captcha":
                        if not headless:
                            pg.wait_for_timeout(15000)
                        else:
                            # 无头遇验证码：弹有头窗口，fill 后等人工过验证码+提交
                            CAPTCHA_EVENT.clear()
                            try:
                                p2, b2, pg2 = wjx_import.open_wjx(url, headless=False)
                                wjx_import.fill_one(pg2, row, mp, recorder)
                                st["captcha_wait"] = {"idx": idx, "subject_id": row.get("subject_id", "")}
                                _write_wjx_status(st)
                                log.info("[wjx] 样本%s 遇验证码，已弹有头窗口等人工", row.get("subject_id"))
                                CAPTCHA_EVENT.wait(timeout=600)  # 最多等10分钟
                                b2.close(); p2.stop()
                            except Exception as e:
                                log.warning("有头验证码干预失败: %s", e)
                            st["captcha_wait"] = None
                            _write_wjx_status(st)
                            sst = _load_submit_state(); sst[str(idx)] = "submitted"; _save_submit_state(sst)
                            sub = "manual_ok"
                    # 更新该样本提交状态：ok→submitted，其余(captcha/pending/fail)→error
                    sst = _load_submit_state()
                    sst[str(idx)] = "submitted" if sub == "ok" else "error"
                    _save_submit_state(sst)
                    # 失败时截图 + 记录 URL，便于调试（不闪退，证据留存）
                    if sub != "ok":
                        try:
                            pg.screenshot(path=str(WORK / f"wjx_debug_sample{idx}_{sub}.png"))
                        except Exception:
                            pass
                st["results"].append({"idx": idx, "subject_id": row.get("subject_id", ""),
                                      "filled": len(filled), "skipped": len(skipped),
                                      "submit": sub or ("-" if not submit else "pending")})
                log.info("[wjx] 样本%s 填%d项 提交=%s", row.get("subject_id"), len(filled), sub or "-")
            except Exception as e:
                st["results"].append({"idx": idx, "subject_id": row.get("subject_id", ""),
                                      "error": str(e)[:200]})
            st["done"] += 1
            _write_wjx_status(st)
        if headless:
            b.close()
            p.stop()
        else:
            # headful：保留浏览器窗口，便于核对/调试，用户手动关闭
            log.info("[wjx] headful 完成，浏览器窗口保留，请手动关闭")
    except Exception as e:
        st["results"].append({"idx": -1, "error": "运行异常: " + str(e)[:200]})
    finally:
        st["running"] = False
        st["current"] = None
        _write_wjx_status(st)


def _sample_dirs() -> list[Path]:
    """work 下含 pages_proc 且非 _induct 的目录(各样本)，按名排序，与 results.csv 行序对应。"""
    out = []
    for pp in sorted(WORK.glob("*/pages_proc")):
        parent = pp.parent
        if parent.name.startswith("_induct"):
            continue
        if any((pp / f"src_{i:02d}.png").exists() for i in range(1, 21)):
            out.append(parent)
    return out


def _read_results() -> tuple[list[str], list[list[str]]]:
    with open(RESULTS, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        cols = next(r)
        rows = list(r)
    return cols, rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默日志

    def _send(self, code: int, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)
        if p in ("/", "/index.html"):
            f = STATIC / "index.html"
            if f.exists():
                self._send(200, f.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, "static/index.html 不存在")
        elif p == "/wjx":
            f = STATIC / "wjx.html"
            self._send(200, f.read_text(encoding="utf-8") if f.exists() else "wjx.html 不存在",
                       "text/html; charset=utf-8")
        elif p == "/increment":
            f = STATIC / "increment.html"
            self._send(200, f.read_text(encoding="utf-8") if f.exists() else "increment.html 不存在",
                       "text/html; charset=utf-8")
        elif p == "/api/data":
            self._send(200, json.dumps(self._data(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/issues":
            self._send(200, json.dumps(self._issues(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/wjx_status":
            self._send(200, json.dumps(_read_wjx_status(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/scan_pdfs":
            self._send(200, json.dumps({"pdfs": _scan_pdfs()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/increment_status":
            self._send(200, json.dumps(_read_increment_status(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/wjx_url":
            self._send(200, json.dumps({"url": _get_wjx_url()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/backups":
            self._send(200, json.dumps({"backups": _list_backups()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/trash":
            self._send(200, json.dumps({"trash": _trash_list()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p == "/api/img":
            si = int(qs.get("sample", ["0"])[0])
            pg = int(qs.get("page", ["1"])[0])
            dirs = _sample_dirs()
            if not (0 <= si < len(dirs)):
                self._send(404, "样本不存在")
                return
            img = dirs[si] / "pages_proc" / f"src_{pg:02d}.png"
            if not img.exists():
                self._send(404, "图片不存在")
                return
            self._send(200, img.read_bytes(), "image/png")
        else:
            self._send(404, "not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/backup":
            try:
                name = _do_backup()
                self._send(200, json.dumps({"ok": True, "name": name}))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "err": str(e)[:200]}))
            return
        if u.path == "/api/delete_sample":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            ok, info = _delete_sample(int(data.get("idx", -1)))
            self._send(200, json.dumps({"ok": ok, "info": info}))
            return
        if u.path == "/api/restore_sample":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            ok, info = _restore_sample(str(data.get("subject_id", "")))
            self._send(200, json.dumps({"ok": ok, "info": info}))
            return
        if u.path == "/api/reset_submit":
            _save_submit_state({})
            self._send(200, json.dumps({"ok": True}))
            return
        if u.path == "/api/captcha_done":
            CAPTCHA_EVENT.set()
            self._send(200, json.dumps({"ok": True}))
            return
        if u.path == "/api/restore":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            ok = _do_restore(str(data.get("name", "")))
            self._send(200, json.dumps({"ok": ok}))
            return
        if u.path == "/api/wjx_url":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            ok = _set_wjx_url(data.get("url", ""))
            self._send(200, json.dumps({"ok": ok, "url": _get_wjx_url()}),
                       "application/json; charset=utf-8")
            return
        if u.path == "/api/increment_import":
            if _read_increment_status().get("running"):
                self._send(409, json.dumps({"ok": False, "err": "已有识别任务在运行"}))
                return
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            pdfs = [str(x) for x in data.get("pdfs", [])]
            t = threading.Thread(target=_increment_run, args=(pdfs,), daemon=True)
            t.start()
            self._send(200, json.dumps({"ok": True, "started": True, "total": len(pdfs)}),
                       "application/json; charset=utf-8")
            return
        if u.path == "/api/wjx_import":
            if _read_wjx_status().get("running"):
                self._send(409, json.dumps({"ok": False, "err": "已有导入任务在运行"}))
                return
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            samples = [int(x) for x in data.get("samples", [])]
            recorder = data.get("recorder", "default") or "default"
            submit = bool(data.get("submit", False))
            headless = bool(data.get("headless", False))
            t = threading.Thread(target=_wjx_run, args=(samples, recorder, submit, headless), daemon=True)
            t.start()
            self._send(200, json.dumps({"ok": True, "started": True, "total": len(samples)}),
                       "application/json; charset=utf-8")
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            n = self._save(data.get("changes", []))
            self._send(200, json.dumps({"ok": True, "saved": n}),
                       "application/json; charset=utf-8")
        elif u.path == "/api/check":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            state = self._load_checked()
            state[str(data.get("idx"))] = bool(data.get("checked"))
            self._save_checked(state)
            self._send(200, json.dumps({"ok": True, "checked": state}),
                       "application/json; charset=utf-8")
        elif u.path == "/api/resolve":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or "{}")
            state = self._load_resolved()
            state[str(data.get("key"))] = bool(data.get("resolved"))
            self._save_resolved(state)
            self._send(200, json.dumps({"ok": True}),
                       "application/json; charset=utf-8")
        else:
            self._send(404)

    def _load_checked(self) -> dict:
        if REVIEW_STATE.exists():
            try:
                return json.loads(REVIEW_STATE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_checked(self, state: dict) -> None:
        try:
            REVIEW_STATE.write_text(json.dumps(state, ensure_ascii=False),
                                    encoding="utf-8")
        except OSError as e:
            log.warning("保存核对状态失败: %s", e)

    def _load_resolved(self) -> dict:
        if ISSUE_RESOLVED.exists():
            try:
                return json.loads(ISSUE_RESOLVED.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_resolved(self, state: dict) -> None:
        try:
            ISSUE_RESOLVED.write_text(json.dumps(state, ensure_ascii=False),
                                      encoding="utf-8")
        except OSError as e:
            log.warning("保存问题解决状态失败: %s", e)

    def _issues(self) -> list[dict]:
        """读取最新一次的 issues_*.jsonl，返回当前批次问题清单(含解决状态)。"""
        issues: list[dict] = []
        files = sorted((ROOT / "logs").glob("issues_*.jsonl"))
        if not files:
            return issues
        resolved = self._load_resolved()
        for line in files[-1].read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = f"{it.get('subject_id','')}|{it.get('scope','')}|{it.get('code','')}|{it.get('msg','')}"
            it["_key"] = key
            it["resolved"] = bool(resolved.get(key, False))
            issues.append(it)
        return issues

    def _data(self) -> dict:
        cols, rows = _read_results()
        scales = load_scales(str(SCALES_YAML))
        colset = set(cols)
        groups: list[dict] = []
        # 基本信息(第1页)
        base = [c for c in ("subject_id", "name", "date") if c in colset]
        if base:
            groups.append({"label": "第1页 基本信息(编号/姓名/调查时间)", "page": 1, "cols": base})
        # 第2页 17题
        p2 = [c for c in cols if c.startswith("p2_q")]
        if p2:
            groups.append({"label": "第2页 17题", "page": 2, "cols": p2})
        # 各量表(按模板顺序，与源页对照)
        for m in scales:
            sc = [f"{m.id}_{q}" for q in m.q_range if f"{m.id}_{q}" in colset]
            if not sc:
                continue
            page = (m.source_pages[0] if m.source_pages else (m.bbox_page or 1))
            sub = f"·{m.unit_label}" if m.unit_label and m.unit_label != "主" else ""
            title = (m.title or "").strip()[:28]
            groups.append({
                "label": f"{m.id} [量表{m.group or '?'}{sub}] {title}（{m.n_items}题）",
                "page": page,
                "cols": sc,
                "option_range": list(m.option_range) if m.option_range else None,
                "sub_keys": list(m.sub_keys) if m.sub_keys else None,
                "answer_style": m.answer_style,
            })
        dirs = _sample_dirs()
        samples = []
        sid_idx = cols.index("subject_id") if "subject_id" in cols else -1
        name_idx = cols.index("name") if "name" in cols else -1
        for i, d in enumerate(dirs):
            sid = rows[i][sid_idx] if (sid_idx >= 0 and i < len(rows)) else ""
            nm = rows[i][name_idx] if (name_idx >= 0 and i < len(rows)) else d.name
            samples.append({"idx": i, "name": nm, "subject_id": sid, "dir": d.name})
        # 问卷星特有字段：其他未尽事宜(每样本一个，第20页末填写，默认留空)
        if not any(g.get("cols") == ["wjx_other"] for g in groups):
            groups.append({"label": "其他未尽事宜 (问卷星特有，可留空)",
                           "page": 20, "cols": ["wjx_other"],
                           "option_range": None, "sub_keys": None, "answer_style": "fill"})

        return {"columns": cols, "rows": rows, "groups": groups, "samples": samples,
                "checked": self._load_checked(),
                "wjx_submit": _load_submit_state()}

    def _save(self, changes: list[dict]) -> int:
        cols, rows = _read_results()
        # 支持写入新列(如 wjx_other)：results.csv 原本没有则自动追加
        new_cols = []
        for ch in changes:
            col = str(ch["col"])
            if col not in cols and col not in new_cols:
                new_cols.append(col)
        if new_cols:
            cols = cols + new_cols
            for r in rows:
                r += [""] * len(new_cols)
        n = 0
        for ch in changes:
            ri, col, val = int(ch["row"]), str(ch["col"]), ch.get("value", "")
            if not (0 <= ri < len(rows)) or col not in cols:
                continue
            ci = cols.index(col)
            row = rows[ri]
            if ci >= len(row):
                row += [""] * (ci + 1 - len(row))
            row[ci] = val
            n += 1
        if n:
            with open(RESULTS, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)
        return n


def main(port: int = PORT):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    print(f"\n  问卷识别核对前端已启动:  http://localhost:{port}")
    print(f"  (浏览器打开上面的地址；Ctrl+C 退出)\n")
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as s:
        s.serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    main(a.port)
