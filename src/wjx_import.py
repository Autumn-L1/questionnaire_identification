"""问卷星批量导入：Playwright 自动填写（headful 模式，人工可校验/过验证码）。

填写策略（依据 DOM 探查）：
- 单选题：radio 隐藏，点击 label[for="qN_value"] 触发选中。
- 填空题：page.fill('[name=qN]', val)。
- 矩阵量表：text 控件，JS 注入设 .value + dispatch change（阶段2）。
- 录入人 q1（所有样本共用）、其他未尽事宜 q65（CSV wjx_other）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

log = logging.getLogger("qr.wjx")
ROOT = Path(__file__).resolve().parent.parent


def load_mapping(path: str = "config/wjx_mapping.yaml") -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _clean(v) -> str:
    return ("" if v is None else str(v)).strip()


def _parse_bool(val) -> str:
    """'否，兄弟姐妹几个：4' / '是...' → '是'/'否'"""
    s = _clean(val)
    if s.startswith("是"):
        return "是"
    if s.startswith("否"):
        return "否"
    return s[:1]


def _split_grade_class(val):
    """'七年级10班' → ('七年级','10')"""
    s = _clean(val).replace("班", "")
    m = re.match(r"([^\d]+?)(\d+)", s)
    if m:
        return m.group(1), m.group(2)
    return s, ""


def _click_scale(page, qn: str, ridx: int, val) -> bool:
    """点击矩阵某行(fid=qN_ridx)的选项 a[dval=val]，验证选中，偶发未中则重试1次。
    空值时尝试点 -99(缺失值选项)。"""
    val = str(val).strip()
    if not val or val == "null":
        # 空值：找-99的dval，用正常路径点击(verify+重试)，比直接a.click()可靠
        fid = f"{qn}_{ridx}"
        dval99 = None
        try:
            dval99 = page.evaluate(
                '() => { const tr = document.querySelector(\'tr[fid="' + fid + '"]\');'
                " if (!tr) return null;"
                " const a = [...tr.querySelectorAll('a')].find(x => (x.textContent||'').trim() === '-99');"
                " return a ? a.getAttribute('dval') : null; }")
        except Exception:
            pass
        if dval99:
            return _click_scale(page, qn, ridx, dval99)  # 递归：用-99的dval走正常点击
        return False
    fid = f"{qn}_{ridx}"
    click_js = ("() => { const a = document.querySelector("
                "'tr[fid=\"" + fid + "\"] a[dval=\"" + val + "\"]');"
                " if (!a) return false; a.click(); return true; }")
    verify_js = ("() => { const a = document.querySelector("
                 "'tr[fid=\"" + fid + "\"] a[dval=\"" + val + "\"]');"
                 " return a && a.className.includes('rate-on'); }")
    try:
        page.evaluate(click_js)
        if not page.evaluate(verify_js):
            page.wait_for_timeout(200)
            page.evaluate(click_js)
        return bool(page.evaluate(verify_js))
    except Exception as e:
        log.warning("矩阵 %s_%d=%s 点击失败: %s", qn, ridx, val, e)
        return False


def fill_matrix(page, row: dict, mp: dict) -> list:
    """填所有矩阵量表。行序按 scales 顺序、每题按 dims(双维度) 交替展开。
    CSV 双维度值形如 {"父亲":3,"母亲":2}；单维度为数字字符串。
    【关键】问卷星 a 的 dval 是 1-based(1..N)，CSV 值是 option_range 下界起；
    故 dval = CSV值 - option_range[0] + 1（opt[1,4]→不变；opt[0,4]→CSV+1）。"""
    import json as _json
    try:
        from .config_schema import load_scales
    except ImportError:
        from config_schema import load_scales
    sc_opt = {s.id: (s.option_range[0] if s.option_range else 1)
              for s in load_scales(str(ROOT / "config" / "scales.yaml"))}

    def _to_dval(val, lo):
        try:
            return str(int(val) - int(lo) + 1)
        except (ValueError, TypeError):
            return str(val)

    done = []
    for qn, cfg in mp.get("matrix", {}).items():
        ridx = 0
        # 显式列模式：归因等跨scale/部分scale的量表，直接指定 CSV 列顺序
        if "cols" in cfg:
            lo = cfg.get("lo", 1)
            for col in cfg["cols"]:
                if _click_scale(page, qn, ridx, _to_dval(row.get(col, ""), lo)):
                    done.append(f"{qn}_{ridx}")
                ridx += 1
            continue
        scales = cfg["scales"]
        dims = cfg.get("dims")  # None=单维度
        ridx = 0
        for sid in scales:
            lo = sc_opt.get(sid, 1)
            qs = sorted((k for k in row if k.startswith(sid + "_")),
                        key=lambda x: int(str(x).split("_")[1]))
            for qcol in qs:
                raw = row.get(qcol, "")
                if dims:
                    try:
                        dv = _json.loads(raw) if (raw and str(raw).strip().startswith("{")) else {}
                    except (ValueError, TypeError):
                        dv = {}
                    for d in dims:
                        if _click_scale(page, qn, ridx, _to_dval(dv.get(d, ""), lo)):
                            done.append(f"{qn}_{ridx}")
                        ridx += 1
                else:
                    if _click_scale(page, qn, ridx, _to_dval(raw, lo)):
                        done.append(f"{qn}_{ridx}")
                    ridx += 1
    return done


def fill_one(page, row: dict, mp: dict, recorder: str) -> tuple[list, list]:
    """把一份样本填到当前已打开的问卷星页。返回 (已填qN列表, 跳过qN列表)。"""
    filled, skipped = [], []

    def set_text(qn, val):
        val = _clean(val)
        if not val:
            skipped.append(qn)
            return
        # 用 JS 设 value + 触发事件（部分 text 控件隐藏，page.fill 会因 not visible 失败）
        # 问卷星 textEdit 控件：隐藏 input + label.textEdit 显示文本，需同步更新 label
        import json as _json
        js = ("() => { const e = document.querySelector('[name=\"" + qn + "\"]');"
              " if (!e) return 'no-elem';"
              " e.value = " + _json.dumps(val) + ";"
              " e.dispatchEvent(new Event('input', {bubbles: true}));"
              " e.dispatchEvent(new Event('change', {bubbles: true}));"
              " e.dispatchEvent(new Event('blur', {bubbles: true}));"
              " let lab = e.nextElementSibling;"
              " if (!(lab && lab.classList.contains('textEdit'))) lab = e.parentElement?.querySelector('label.textEdit, .textEdit');"
              " if (lab) { const sp = lab.querySelector('span');"
              "   if (sp) sp.textContent = " + _json.dumps(val) + "; else lab.textContent = " + _json.dumps(val) + ";"
              "   lab.classList.remove('initStyle', 'initStyle_default'); }"
              " return 'ok'; }")
        try:
            res = page.evaluate(js)
            (filled if res == "ok" else skipped).append(qn)
            if res != "ok":
                log.warning("填空 %s: %s", qn, res)
        except Exception as e:
            log.warning("填空 %s 失败: %s", qn, e)
            skipped.append(qn)

    def click_radio(qn, value):
        if not value:
            # 空值：点 label 文本="-99" 的缺失值选项(选择题)，无则跳过
            try:
                js99 = ('() => { const rs = document.querySelectorAll(\'input[name="' + qn + '"]\');'
                        " for (const r of rs) { const l = document.querySelector('label[for=\\'' + r.id + '\\']');"
                        " if (l && (l.textContent||'').trim() === '-99') { l.click(); r.dispatchEvent(new Event('change', {bubbles: true})); return true; } }"
                        " return false; }")
                if page.evaluate(js99):
                    filled.append(qn + "(-99)")
                    return
            except Exception:
                pass
            skipped.append(qn)
            return
        value = str(value)
        # radio 隐藏，用 JS 直接设 checked 并触发 click/change（点 label 同步 UI）
        click_js = f"""() => {{
            const r = document.getElementById('{qn}_{value}');
            if (!r) return 'no-radio';
            r.checked = true;
            const l = document.querySelector('label[for="{qn}_{value}"]');
            if (l) l.click(); else r.click();
            r.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'ok';
        }}"""
        verify_js = f"""() => {{
            const r = document.querySelector('input[name="{qn}"]:checked');
            return (r && r.value) === '{value}';
        }}"""
        try:
            page.evaluate(click_js)
            if not page.evaluate(verify_js):
                # 偶发未选中(元素未就绪)，等待后重试1次
                page.wait_for_timeout(250)
                page.evaluate(click_js)
            ok = page.evaluate(verify_js)
            (filled if ok else skipped).append(qn)
        except Exception as e:
            log.warning("单选 %s=%s 失败: %s", qn, value, e)
            skipped.append(qn)

    # 录入人 / 其他未尽事宜
    set_text(mp["recorder_q"], recorder)
    set_text(mp["other_q"], row.get("wjx_other", ""))
    # 填空题（支持 str 或 {col, clean, null_fill} 三种格式）
    for qn, spec in mp.get("text", {}).items():
        if isinstance(spec, dict) and spec.get("template"):
            # 模板字符串：替换 {列名} 占位（如 "初一-10-{name}"）
            val = spec["template"]
            for k, v in row.items():
                val = val.replace("{" + k + "}", str(v or ""))
            set_text(qn, val)
            continue
        if isinstance(spec, dict):
            col, val = spec["col"], row.get(spec["col"], "")
            if spec.get("clean") == "digit":
                m = re.search(r"\d+", str(val))
                val = m.group(0) if m else ""
            # 空值时填指定的缺失值（如年龄"0代表缺失值"）
            if not str(val).strip() and spec.get("null_fill"):
                val = spec["null_fill"]
        else:
            col, val = spec, row.get(spec, "")
        set_text(qn, val)
    # 年级班级拆分
    for qn, rule in mp.get("split", {}).items():
        g, c = _split_grade_class(row.get(rule["from"], ""))
        set_text(qn, g if rule["take"] == "grade" else c)
    # 单选题（map 模糊匹配 / numeric 数字偏移）
    for qn, cfg in mp.get("single", {}).items():
        raw = str(row.get(cfg["col"], "") or "")
        if cfg.get("parse_bool"):
            raw = _parse_bool(raw)
        if cfg.get("numeric"):
            try:
                value = str(int(float(raw)) + int(cfg.get("shift", 0)))
            except (ValueError, TypeError):
                value = ""
        else:
            value = ""
            for k, v in cfg.get("map", {}).items():
                k = str(k)
                if k and (raw == k or raw.startswith(k) or k in raw):
                    value = v
                    break
        click_radio(qn, value)
    # q9 选"否"时的兄弟姐妹数量填空(#tqq9_2，p2_q7 形如 "否，...：4")
    p2q7 = str(row.get("p2_q7", ""))
    if "否" in p2q7:
        m = re.search(r"\d+", p2q7)
        if m:
            js = ('() => { const e = document.getElementById("tqq9_2");'
                  " if (!e) return 'no';"
                  " e.value = " + repr(m.group(0)) + ";"
                  " e.dispatchEvent(new Event('input', {bubbles: true}));"
                  " e.dispatchEvent(new Event('change', {bubbles: true}));"
                  " return 'ok'; }")
            try:
                if page.evaluate(js) == "ok":
                    filled.append("tqq9_2")
            except Exception as e:
                log.warning("q9 数量填空失败: %s", e)
    # 量表矩阵：点击 a[dval] 选中
    m_done = fill_matrix(page, row, mp)
    filled.extend(m_done)
    return filled, skipped


def submit_one(page, timeout: int = 15, debug_prefix: str = None) -> str:
    """滚动到底 → 点 div#ctlNext 提交 → 检测结果。
    判定：问卷星提交成功会跳转(URL 离开原问卷页)；URL 不变=被拦(必填校验/验证码)。
    返回 'ok'(成功) / 'captcha'(验证码) / 'pending'(必填校验等未过) / 'fail'(点击失败)。
    debug_prefix 非空时，提交前后各截图 + 记录 URL，便于调试闪退。"""
    orig = page.url
    if debug_prefix:
        try:
            page.screenshot(path=debug_prefix + "_pre_submit.png", full_page=False)
        except Exception:
            pass
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.click("#ctlNext", timeout=5000)
    except Exception as e:
        log.warning("点击提交失败: %s", e)
        return "fail"
    page.wait_for_timeout(1500)  # 等提交反应(页面跳转/弹窗)
    if debug_prefix:
        try:
            page.screenshot(path=debug_prefix + "_post_submit.png", full_page=False)
        except Exception:
            pass
        log.info("[wjx] submit URL 变化: %s -> %s", orig, page.url)
    # 等 URL 离开原问卷页(成功提交会跳转到结果页)
    try:
        page.wait_for_url(lambda u: u != orig, timeout=timeout * 1000)
        return "ok"
    except Exception:
        pass
    # URL 未变 → 提交被拦；区分验证码 vs 必填校验
    try:
        txt = page.inner_text("body") or ""
    except Exception:
        txt = ""
    if "验证" in txt or "captcha" in txt.lower() or "滑块" in txt:
        return "captcha"
    return "pending"


def open_wjx(url: str, headless: bool = False):
    p = sync_playwright().start()
    b = p.chromium.launch(headless=headless, channel="msedge")
    pg = b.new_page()
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(4000)
    return p, b, pg


def run_import(rows: list[dict], recorder: str, headless: bool = False,
               submit: bool = False):
    """逐样本打开问卷星填写。headful 时保留窗口供人工核对；submit=False 不提交。"""
    mp = load_mapping()
    url = mp["wjx_url"]
    results = []
    p, b, pg = open_wjx(url, headless)
    try:
        for i, row in enumerate(rows):
            pg.goto(url, wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            filled, skipped = fill_one(pg, row, mp, recorder)
            log.info("[wjx] 样本%s: 填%d项, 跳过%s",
                     row.get("subject_id"), len(filled), skipped)
            results.append({"subject_id": row.get("subject_id"),
                            "filled": len(filled), "skipped": skipped})
            if submit:
                res = submit_one(pg)
                results[-1]["submit"] = res
                log.info("[wjx] 样本%s 提交结果: %s", row.get("subject_id"), res)
                if res == "captcha" and not headless:
                    input("遇验证码，请在浏览器人工完成后回车继续...")
                pg.wait_for_timeout(2000)
            else:
                if not headless:
                    pg.wait_for_timeout(1500)
    finally:
        if headless:
            b.close()
            p.stop()
    return p, b, pg, results
