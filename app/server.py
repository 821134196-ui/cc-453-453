"""电子价签改价后台（标准库 http.server）。

白 / 零售红 / 深灰三色主题；分段批次进度条；差异表固定旧价·新价·生效时间列；
现场核验页大号扫码入口，价格全部等宽排版。
"""
from __future__ import annotations

import html
import io
import json
import re
import sqlite3
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import results as R
from . import services as S
from .adapter import EslAdapter
from .db import PHOTO_DIR, connect, iso, now, parse_dt
from .importer import ImportError_, create_import, save_upload, validate

RETAIL_RED = "#D7263D"
DARK_GRAY = "#2B2B2E"
WAIT_GRAY = "#E6E6E6"

# ----------------------------------------------------------------- 小工具

def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def money(x) -> str:
    if x is None:
        return "—"
    return f"￥{float(x):.2f}"


def qs(parsed, key, default=""):
    vals = urllib.parse.parse_qs(parsed.query).get(key)
    return vals[0] if vals else default


def parse_form(body: bytes, header: str) -> dict:
    """支持 application/x-www-form-urlencoded 与 multipart/form-data。"""
    ctype = header or ""
    if "multipart/form-data" in ctype:
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return {"_fields": {}, "_files": {}}
        return _parse_multipart(body, m.group(1).strip().strip('"'))
    fields = {k: v[0] for k, v in
              urllib.parse.parse_qs(body.decode("utf-8")).items()}
    return {"_fields": fields, "_files": {}}


def _parse_multipart(body: bytes, boundary: str) -> dict:
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delim = b"--" + boundary.encode()
    for part in body.split(delim):
        if not part or part == b"--" or part == b"--\r\n":
            continue
        if part.startswith(b"--"):
            break
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        data = data[:-2] if data.endswith(b"\r\n") else data
        headers = {}
        for line in head.decode("latin1").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        if file_m and file_m.group(1):
            files[name] = (Path(file_m.group(1)).name, data)
        else:
            fields[name] = data.decode("utf-8")
    return {"_fields": fields, "_files": files}


# ----------------------------------------------------------------- HTML 骨架

def layout(title: str, body: str, flash: str = "") -> bytes:
    flash_html = (
        f'<div class="flash">{esc(flash)}</div>' if flash else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 电子价签改价台</title>
<style>{STYLE}</style></head>
<body>
<header class="topbar">
  <div class="brand"><span class="logo">价</span>
    <div><b>红价签 · 电子价签批量改价台</b>
    <small>门店价签版本中枢 / 演示数据</small></div></div>
  <nav>
    <a href="/">总览</a>
    <a href="/approvals">审批队列</a>
    <a href="/imports/new">导入调价</a>
    <a href="/batches">批次进度</a>
    <a href="/inspect">现场核验</a>
    <a href="/rollbacks">回滚记录</a>
    <a href="/tags">价签台账</a>
  </nav>
</header>
<main>
{flash_html}
{body}
</main>
<footer class="foot">SQLite 价格版本 · 本地导入文件/抽检照片 · 进程内定时任务 · 模拟 ESL 网关回执</footer>
</body></html>""".encode("utf-8")


STYLE = """
:root{--red:#D7263D;--ink:#2B2B2E;--line:#E4E4E4;--bg:#FFFFFF;--soft:#F7F7F8;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}
.topbar{display:flex;justify-content:space-between;align-items:center;
  padding:14px 28px;border-bottom:3px solid var(--red);background:#fff;
  position:sticky;top:0;z-index:5;}
.brand{display:flex;gap:12px;align-items:center}
.brand small{display:block;color:#888;font-size:12px}
.logo{display:inline-flex;width:38px;height:38px;border-radius:6px;
  background:var(--red);color:#fff;font-weight:700;font-size:20px;
  align-items:center:center;justify-content:center;align-items:center;}
nav a{color:var(--ink);text-decoration:none;margin-left:20px;font-weight:600;
  padding:6px 2px;border-bottom:2px solid transparent}
nav a:hover{color:var(--red);border-color:var(--red)}
main{max-width:1180px;margin:26px auto 60px;padding:0 28px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:26px 0 10px}
.sub{color:#777;margin:0 0 18px}
.foot{text-align:center;color:#9a9aa0;font-size:12px;padding:26px}
.flash{background:#FDECEF;border:1px solid var(--red);color:var(--red);
  padding:10px 14px;border-radius:6px;margin-bottom:18px;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0 8px}
.card{border:1px solid var(--line);border-radius:8px;padding:14px 16px;background:#fff}
.card .n{font-size:26px;font-weight:700;font-family:ui-monospace,Consolas,monospace}
.card .l{color:#777;font-size:12px;margin-top:2px}
.card.red .n{color:var(--red)} .card.ink .n{color:var(--ink)}
table{width:100%;border-collapse:collapse;background:#fff;margin:6px 0 18px}
th,td{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;
  vertical-align:middle;font-size:13.5px}
th{background:var(--soft);font-weight:700;white-space:nowrap}
tr:hover td{background:#FCFCFD}
.num{font-family:ui-monospace,"SF Mono",Consolas,"Liberation Mono",monospace;
  font-variant-numeric:tabular-nums;letter-spacing:.04em}
.price{font-family:ui-monospace,"SF Mono",Consolas,monospace;
  font-variant-numeric:tabular-nums;font-weight:700;letter-spacing:.05em}
.price.big{font-size:40px}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
  font-weight:700;white-space:nowrap}
.b-red{background:var(--red);color:#fff}
.b-ink{background:var(--ink);color:#fff}
.b-gray{background:#EEE;border:1px solid #DDD;color:#555}
.b-softred{background:#FDECEF;color:var(--red);border:1px solid #F3C2CB}
.b-softink{background:#ECECED;color:var(--ink)}
.row-err{background:#FFF8F9}
.row-warn{background:#FFFCF2}
.btn{display:inline-block;border:1px solid var(--red);background:var(--red);
  color:#fff;padding:7px 16px;border-radius:6px;font-weight:700;font-size:13px;
  cursor:pointer;text-decoration:none}
.btn.ghost{background:#fff;color:var(--red)}
.btn.ink{background:var(--ink);border-color:var(--ink)}
.btn.sm{padding:3px 10px;font-size:12px}
.btn:disabled{opacity:.45;cursor:default}
.segbar{display:flex;height:22px;border-radius:4px;overflow:hidden;
  border:1px solid var(--line);min-width:170px;background:#fff}
.segbar .s-s{background:var(--ink)} .segbar .s-w{background:#D9D9DB}
.segbar .s-f{background:var(--red)}
.segbar span{display:block;height:100%}
.seglegend{font-size:12px;color:#666;margin-top:4px}
.seglegend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 4px 0 12px}
form.inline{display:inline}
.panel{border:1px solid var(--line);border-radius:8px;padding:18px 20px;background:#fff;margin:14px 0}
label{display:block;font-weight:700;margin:12px 0 5px;font-size:13px}
input[type=text],input[type=number],input[type=datetime-local],input[type=date],
select,textarea{width:100%;padding:9px 11px;border:1px solid #CCC;border-radius:6px;
  font-size:14px;font-family:inherit}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 22px}
.diff-wrap{overflow-x:auto} table.diff{min-width:760px}
table.diff th.fixed,table.diff td.fixed{background:#fff;position:sticky}
.scan-hero{text-align:center;border:2px dashed var(--red);border-radius:14px;
  padding:34px 20px 28px;background:#FFFDFD;margin:10px 0 22px}
.scan-hero .qricon{font-size:64px;line-height:1}
.scan-hero input{font-size:22px;padding:14px 18px;text-align:center;
  font-family:ui-monospace,Consolas,monospace;letter-spacing:.12em}
.scan-grid{display:grid;grid-template-columns:1.2fr .8fr;gap:26px}
.verdict{border-radius:10px;padding:22px;margin:8px 0}
.verdict.ok{background:#ECECED;border:1px solid #D6D6D9}
.verdict.bad{background:#FDECEF;border:2px solid var(--red)}
.verdict h3{margin:0 0 8px;font-size:18px}
.verdict.bad h3{color:var(--red)}
.bignum{font-size:44px;font-weight:800;font-family:ui-monospace,Consolas,monospace;
  letter-spacing:.06em}
.muted{color:#888} .right{text-align:right}
a{color:var(--red)}
.kvs{display:grid;grid-template-columns:110px 1fr;gap:6px 14px;margin:8px 0}
.kvs dt{color:#777}.kvs dd{margin:0}
img.shot{max-width:100%;border:1px solid var(--line);border-radius:8px}
.tagpill{font-family:ui-monospace,Consolas,monospace;background:var(--soft);
  border:1px solid var(--line);border-radius:4px;padding:1px 7px;font-size:12px}
"""


# ----------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "ESL/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- 基础设施 ---
    def _send(self, body: bytes, status=200, ctype="text/html; charset=utf-8",
              extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str):
        self._send(b"", HTTPStatus.SEE_OTHER,
                   extra={"Location": path})

    def _render(self, title, body, flash=""):
        self._send(layout(title, body, flash))

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self, "_conn"):
            self._conn = connect()
        return self._conn

    # --- 路由 ---
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path, q = p.path, qs
        try:
            if path == "/":
                return self.page_home()
            if path == "/imports/new":
                return self.page_import_new()
            if path == "/imports":
                return self.page_imports()
            m = re.fullmatch(r"/imports/(\d+)", path)
            if m:
                return self.page_import_detail(int(m.group(1)))
            if path == "/approvals":
                return self.page_approvals()
            if path == "/batches":
                return self.page_batches()
            m = re.fullmatch(r"/batches/(\d+)", path)
            if m:
                return self.page_batch_detail(int(m.group(1)))
            if path == "/inspect":
                return self.page_inspect()
            if path == "/rollbacks":
                return self.page_rollbacks()
            m = re.fullmatch(r"/rollbacks/(\d+)", path)
            if m:
                return self.page_rollback_detail(int(m.group(1)))
            if path == "/tags":
                return self.page_tags()
            m = re.fullmatch(r"/photos/([^/]+)", path)
            if m:
                return self.page_photo(m.group(1))
            self._send(layout("404", "<h1>页面不存在</h1>").encode(), 404)
        except ServiceErrorPage as e:
            self._render("操作被拒绝", f'<div class="flash">{esc(e.msg)}</div>'
                         '<p><a href="javascript:history.back()">返回</a></p>')

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        form = parse_form(body, self.headers.get("Content-Type", ""))
        f, files = form["_fields"], form["_files"]
        try:
            if path == "/imports/upload":
                return self.act_upload(f, files)
            if path == "/approvals/approve":
                return self.act_approve(f, reject=False)
            if path == "/approvals/reject":
                return self.act_approve(f, reject=True)
            m = re.fullmatch(r"/items/(\d+)/retry", path)
            if m:
                S.retry_item(self.conn, int(m.group(1)), EslAdapter(self.conn))
                return self._redirect(f"/batches/{f.get('batch_id','')}")
            m = re.fullmatch(r"/batches/(\d+)/retry", path)
            if m:
                S.retry_batch_failed(self.conn, int(m.group(1)),
                                     EslAdapter(self.conn))
                return self._redirect(path.rsplit("/retry", 1)[0])
            m = re.fullmatch(r"/batches/(\d+)/rollback", path)
            if m:
                return self.act_rollback(int(m.group(1)), f)
            if path == "/inspect/submit":
                return self.act_inspect(f, files)
            self._send(b"not found", 404, "text/plain")
        except S.ServiceError as e:
            self._render("操作被拒绝",
                         f'<div class="flash">{esc(e.code)} · {esc(e.message)}</div>'
                         '<p><a class="btn ghost" href="javascript:history.back()">返回</a></p>')
        except (ValueError, ImportError_) as e:
            self._render("提交失败",
                         f'<div class="flash">{esc(str(e))}</div>'
                         '<p><a class="btn ghost" href="javascript:history.back()">返回</a></p>')

    # ------------------------------------------------------------- 总览
    def page_home(self):
        c = self.conn
        kpi = {
            "stores": c.execute("SELECT COUNT(*) n FROM stores").fetchone()["n"],
            "tags": c.execute("SELECT COUNT(*) n FROM tags").fetchone()["n"],
            "offline": c.execute("SELECT COUNT(*) n FROM tags WHERE online=0").fetchone()["n"],
            "waiting": c.execute(
                "SELECT COUNT(*) n FROM batch_items WHERE status='waiting'").fetchone()["n"],
        }
        recent_batches = c.execute(
            """SELECT b.*, u.display_name approver_name FROM batches b
               LEFT JOIN users u ON u.id=b.created_by
               ORDER BY b.id DESC LIMIT 6""").fetchall()
        drift = c.execute(
            """SELECT t.*, s.name store_name FROM tags t JOIN stores s ON s.id=t.store_id
               WHERE ABS(t.expected_price-t.display_price)>0.001""").fetchall()
        rows_html = "".join(
            f"<tr><td><a href='/batches/{b['id']}'>{esc(b['no'])}</a></td>"
            f"<td>{kind_label(b['kind'])}</td><td>{status_badge(b['status'])}</td>"
            f"<td>{seg_bar(c, b['id'])}</td>"
            f"<td class='num'>{esc(b['scheduled_at'] or '')}</td></tr>"
            for b in recent_batches)
        drift_html = "".join(
            f"<tr><td class='num'>{esc(t['tag_no'])}</td><td>{esc(t['store_name'])}</td>"
            f"<td>{esc(t['sku'])}</td>"
            f"<td class='price'>{money(t['expected_price'])}</td>"
            f"<td class='price' style='color:var(--red)'>{money(t['display_price'])}</td></tr>"
            for t in drift) or '<tr><td colspan="5" class="muted">无偏离，期望价与显示价一致。</td></tr>'
        body = f"""
<h1>门店价签总览</h1><p class="sub">每一次回执成功都同时推进系统期望价与价签实际显示价，两者永久留痕。</p>
<div class="cards">
  <div class="card"><div class="n">{kpi['stores']}</div><div class="l">在营门店</div></div>
  <div class="card"><div class="n">{kpi['tags']}</div><div class="l">电子价签</div></div>
  <div class="card red"><div class="n">{kpi['offline']}</div><div class="l">离线价签</div></div>
  <div class="card ink"><div class="n">{kpi['waiting']}</div><div class="l">等待下发条目</div></div>
</div>
<h2>最近批次</h2>
<table><thead><tr><th>批次号</th><th>类型</th><th>状态</th><th style="min-width:220px">成功 / 等待 / 失败</th><th>计划生效</th></tr></thead>
<tbody>{rows_html or '<tr><td colspan="5" class="muted">暂无批次</td></tr>'}</tbody></table>
<h2>期望价 / 显示价偏离</h2>
<table><thead><tr><th>价签编号</th><th>门店</th><th>商品</th><th>系统期望价</th><th>价签实际显示价</th></tr></thead>
<tbody>{drift_html}</tbody></table>
<p><a class="btn" href="/imports/new">导入调价清单</a>
   <a class="btn ink" href="/inspect">前往现场核验</a></p>"""
        self._render("总览", body)

    # ------------------------------------------------------------- 导入
    def page_import_new(self):
        c = self.conn
        users = c.execute("SELECT * FROM users WHERE role='operator'").fetchall()
        sample = ("tag_no,new_price\n"
                  "ESL-A-0001,1.80\nESL-A-0002,2.99\n")
        body = f"""
<h1>导入调价清单</h1>
<p class="sub">CSV 必须包含 <span class="tagpill">tag_no</span> 与
<span class="tagpill">new_price</span> 两列；导入即做商品、门店、限价、互斥与促销时段校验。</p>
<div class="panel">
<form method="post" action="/imports/upload" enctype="multipart/form-data">
 <div class="grid2">
  <div><label>清单文件（.csv）</label>
   <input type="file" name="file" accept=".csv" required></div>
  <div><label>提交人（运营）</label><select name="username">
   {''.join(f'<option value="{u["username"]}">{esc(u["display_name"])}（{u["username"]}）</option>' for u in users)}
  </select></div>
  <div><label>生效时间</label><input type="datetime-local" name="effective_at"
     value="{now().strftime("%Y-%m-%dT%H:%M")}" required></div>
  <div><label>促销结束时间（留空 = 普通调价）</label>
   <input type="datetime-local" name="promo_end_at"></div>
 </div>
 <p style="margin-top:16px"><button class="btn" type="submit">上传并校验</button>
 <a class="btn ghost" href="/data"> </a></p>
</form>
</div>
<div class="panel"><h2 style="margin-top:0">清单格式样例</h2>
<pre class="num" style="background:var(--soft);padding:12px;border-radius:6px">{esc(sample)}</pre>
<p class="muted">文件保存在本地 data/imports/ 目录；同一清单内重复价签、超出商品限价、
价签离线、与在途批次互斥、促销时段重叠都会给出独立结果行。</p></div>"""
        self._render("导入调价", body)

    def act_upload(self, f, files):
        fn, data = files.get("file", ("", b""))
        if not fn:
            raise ValueError("请选择 CSV 文件")
        path = save_upload(fn, data)
        eff = parse_dt(f.get("effective_at", ""))
        promo_s = f.get("promo_end_at", "").strip()
        promo = parse_dt(promo_s) if promo_s else None
        imp_id = create_import(self.conn, path, f.get("username", "u_yang"),
                               eff, promo)
        validate(self.conn, imp_id)
        self._redirect(f"/imports/{imp_id}")

    def page_imports(self):
        rows = self.conn.execute(
            """SELECT i.*, u.display_name submitter_name,
               a.display_name approver_name
               FROM imports i JOIN users u ON u.id=i.submitted_by
               LEFT JOIN users a ON a.id=i.approved_by
               ORDER BY i.id DESC""").fetchall()
        trs = ""
        for r in rows:
            summ = json.loads(r["summary"] or "{}")
            trs += (f"<tr><td><a href='/imports/{r['id']}'>#{r['id']} {esc(r['file_name'])}</a></td>"
                    f"<td>{esc(r['submitter_name'])}</td>"
                    f"<td>{import_status_badge(r['status'])}</td>"
                    f"<td class='num'>{summ.get('ok','—')}/{summ.get('total','—')}"
                    f"{' <span style=color:var(--red)>(错'+str(summ['errors'])+')</span>' if summ.get('errors') else ''}</td>"
                    f"<td class='num'>{esc(r['effective_at'])}</td>"
                    f"<td>{esc(r['approver_name'] or '—')}</td></tr>")
        body = f"""<h1>调价清单</h1>
<p class="sub">导入 → 差异校验 → 审批通过 → 生成批次；审批前不产生任何价格版本。</p>
<table><thead><tr><th>清单</th><th>提交人</th><th>状态</th><th>通过行/总行</th>
<th>生效时间</th><th>审批人</th></tr></thead><tbody>
{trs or '<tr><td colspan="6" class="muted">暂无导入</td></tr>'}</tbody></table>
<p><a class="btn" href="/imports/new">导入新清单</a></p>"""
        self._render("调价清单", body)

    def page_import_detail(self, iid):
        c = self.conn
        imp = c.execute(
            """SELECT i.*, u.display_name submitter_name FROM imports i
               JOIN users u ON u.id=i.submitted_by WHERE i.id=?""",
            (iid,)).fetchone()
        if not imp:
            return self._render("未找到", "<h1>清单不存在</h1>")
        rows = c.execute(
            """SELECT ir.*, p.name product_name FROM import_rows ir
               LEFT JOIN products p ON p.sku=ir.sku
               WHERE ir.import_id=? ORDER BY ir.line_no""", (iid,)).fetchall()
        trs = ""
        for r in rows:
            cls = {"error": "row-err", "warning": "row-warn"}.get(r["status"], "")
            trs += (
                f'<tr class="{cls}"><td class="num">{r["line_no"]}</td>'
                f'<td class="num">{esc(r["tag_no"])}</td>'
                f'<td>{esc(r["product_name"] or r["sku"] or "—")}</td>'
                f'<td class="price">{money(r["old_price"])}</td>'
                f'<td class="price">{money(r["new_price"])}</td>'
                f'<td class="num">{esc(imp["effective_at"])}</td>'
                f'<td>{result_badge(r["result_code"], r["status"])}</td>'
                f'<td>{esc(r["message"])}</td></tr>')
        summ = json.loads(imp["summary"] or "{}")
        actions = ""
        if imp["status"] == "validated":
            if summ.get("errors", 0):
                actions = ('<p class="flash">存在校验错误行，不能进入审批；'
                           '请修正清单后重新导入。</p>')
            else:
                managers = c.execute("SELECT * FROM users WHERE role='manager'").fetchall()
                opts = "".join(
                    f'<option value="{u["username"]}">{esc(u["display_name"])}（{u["username"]}）</option>'
                    for u in managers)
                actions = f"""
<div class="panel"><h2 style="margin-top:0">审批队列动作</h2>
<p>提交人 <b>{esc(imp['submitter_name'])}</b> 不能自审；请由店长账号审批：</p>
<form class="inline" method="post" action="/approvals/approve">
 <input type="hidden" name="import_id" value="{iid}">
 <select name="username" style="width:auto;display:inline-block">{opts}</select>
 <button class="btn" type="submit">审批通过并生成批次</button></form>
<form class="inline" method="post" action="/approvals/reject">
 <input type="hidden" name="import_id" value="{iid}">
 <input type="text" name="reason" placeholder="驳回原因" style="width:240px;display:inline-block" required>
 <input type="hidden" name="username" value="u_zhou">
 <button class="btn ghost" type="submit">驳回</button></form>
<p class="muted" style="margin-top:8px">试试用提交人自己的账号审批，系统会以「{R.SELF_APPROVAL}」拒绝。</p>
</div>"""
        body = f"""
<h1>清单 #{iid} · {esc(imp['file_name'])}</h1>
<p class="sub">提交人 {esc(imp['submitter_name'])} ｜ 状态 {import_status_badge(imp['status'])}
 ｜ 促销结束 {esc(imp['promo_end_at'] or '—')}</p>
<div class="cards">
 <div class="card"><div class="n">{summ.get('total',0)}</div><div class="l">总行数</div></div>
 <div class="card ink"><div class="n">{summ.get('ok',0)}</div><div class="l">校验通过</div></div>
 <div class="card"><div class="n">{summ.get('warnings',0)}</div><div class="l">空变化提示</div></div>
 <div class="card red"><div class="n">{summ.get('errors',0)}</div><div class="l">错误行</div></div>
</div>
<h2>商品差异表（旧价 / 新价 / 生效时间）</h2>
<div class="diff-wrap"><table class="diff"><thead><tr>
 <th>行号</th><th>价签编号</th><th>商品</th>
 <th>旧价（当前期望价）</th><th>新价</th><th>生效时间</th>
 <th>校验结果</th><th>说明</th></tr></thead><tbody>{trs}</tbody></table></div>
{actions}
<p><a href="/imports">← 返回清单列表</a></p>"""
        self._render("差异校验", body)

    # ------------------------------------------------------------- 审批
    def page_approvals(self):
        c = self.conn
        pending = c.execute(
            """SELECT i.*, u.display_name submitter_name FROM imports i
               JOIN users u ON u.id=i.submitted_by
               WHERE i.status='validated' ORDER BY i.id""").fetchall()
        cards = ""
        for r in pending:
            summ = json.loads(r["summary"] or "{}")
            n = c.execute(
                "SELECT COUNT(*) n FROM import_rows WHERE import_id=? AND status='ok'",
                (r["id"],)).fetchone()["n"]
            cards += f"""
<div class="panel"><b>#{r['id']} {esc(r['file_name'])}</b>
 <span class="muted">提交人 {esc(r['submitter_name'])} · 生效 {esc(r['effective_at'])}
 {('· 促销至 ' + esc(r['promo_end_at'])) if r['promo_end_at'] else ''}</span>
 <div style="margin-top:8px">通过 <b class="num">{summ.get('ok',0)}</b> 行 ·
 提示 <b class="num">{summ.get('warnings',0)}</b> ·
 错误 <b class="num" style="color:var(--red)">{summ.get('errors',0)}</b> ·
 将生成 <b class="num">{n}</b> 个改价条目</div>
 <p style="margin-bottom:0"><a class="btn" href="/imports/{r['id']}">查看差异并审批</a></p>
</div>"""
        body = f"""<h1>审批队列</h1>
<p class="sub">只有校验完成（validated）且无错误行的清单可审批；审批通过才生成批次和价格版本。</p>
{cards or '<p class="muted">队列为空。</p>'}"""
        self._render("审批队列", body)

    def act_approve(self, f, reject):
        iid = int(f["import_id"])
        if reject:
            S.reject(self.conn, iid, f.get("username", "u_zhou"),
                     f.get("reason", ""))
        else:
            S.approve(self.conn, iid, f.get("username", "u_zhou"))
        self._redirect(f"/imports/{iid}" if reject else "/batches")

    # ------------------------------------------------------------- 批次
    def page_batches(self):
        rows = self.conn.execute(
            """SELECT b.*, u.display_name approver_name FROM batches b
               LEFT JOIN users u ON u.id=b.created_by ORDER BY b.id DESC""").fetchall()
        trs = ""
        for b in rows:
            trs += (f"<tr><td><a href='/batches/{b['id']}'>{esc(b['no'])}</a></td>"
                    f"<td>{kind_label(b['kind'])}</td>"
                    f"<td>{status_badge(b['status'])}</td>"
                    f"<td style='min-width:230px'>{seg_bar(self.conn, b['id'])}</td>"
                    f"<td class='num'>{esc(b['scheduled_at'] or '-')}</td>"
                    f"<td>{esc(b['approver_name'] or '系统')}</td>"
                    f"<td class='num'>{esc(b['promo_end_at'] or '—')}</td></tr>")
        body = f"""<h1>批次进度</h1>
<p class="sub">分段条：深灰=回执成功，浅灰=等待下发，零售红=失败。失败项可单独重试。</p>
<table><thead><tr><th>批次号</th><th>类型</th><th>状态</th>
<th>成功 / 等待 / 失败</th><th>计划生效</th><th>创建人</th><th>促销结束</th></tr></thead>
<tbody>{trs or '<tr><td colspan="7" class="muted">暂无批次</td></tr>'}</tbody></table>"""
        self._render("批次进度", body)

    def page_batch_detail(self, bid):
        c = self.conn
        b = c.execute(
            """SELECT b.*, u.display_name creator FROM batches b
               LEFT JOIN users u ON u.id=b.created_by WHERE b.id=?""",
            (bid,)).fetchone()
        if not b:
            return self._render("未找到", "<h1>批次不存在</h1>")
        items = c.execute(
            """SELECT bi.*, t.tag_no, s.name store_name, p.name product_name
               FROM batch_items bi
               JOIN tags t ON t.id=bi.tag_id
               JOIN stores s ON s.id=bi.store_id
               JOIN products p ON p.sku=bi.sku
               WHERE bi.batch_id=? ORDER BY bi.id""", (bid,)).fetchall()
        trs = ""
        for it in items:
            retry = ""
            if it["status"] == "failed" and b["status"] != "rolled_back":
                retry = (f'<form class="inline" method="post" action="/items/{it["id"]}/retry">'
                         f'<input type="hidden" name="batch_id" value="{bid}">'
                         '<button class="btn sm">单独重试</button></form>')
            trs += (
                f'<tr class="{"row-err" if it["status"]=="failed" else ""}">'
                f'<td class="num">{esc(it["tag_no"])}</td>'
                f'<td>{esc(it["store_name"])}</td>'
                f'<td>{esc(it["product_name"])}</td>'
                f'<td class="price">{money(it["old_price"])} <span class="muted">v{it["old_version"]}</span></td>'
                f'<td class="price">{money(it["target_price"])} <span class="muted">v{it["target_version"]}</span></td>'
                f'<td>{item_status_badge(it["status"], it["last_result_code"])}</td>'
                f'<td class="num">{it["attempts"]}</td>'
                f'<td>{esc(it["last_error"])}</td><td>{retry}</td></tr>')
        n_fail = sum(1 for it in items if it["status"] == "failed")
        bulk = ""
        if n_fail and b["status"] != "rolled_back":
            bulk = f'''
<form class="inline" method="post" action="/batches/{bid}/retry">
 <button class="btn">一键重试全部失败项（{n_fail}）</button></form>'''
        rollback = ""
        if b["status"] != "rolled_back":
            rollback = f"""
<form class="inline" method="post" action="/batches/{bid}/rollback"
 onsubmit="return confirm('紧急回滚将把价签恢复到本批次之前的明确版本，确定？')">
 <input type="hidden" name="username" value="u_yang">
 <input type="text" name="reason" placeholder="回滚原因，例如：促销设置错误"
  style="width:260px;display:inline-block" required>
 <button class="btn ink" type="submit">紧急回滚本批次</button></form>"""
        versions = c.execute(
            """SELECT pv.*, t.tag_no FROM price_versions pv JOIN tags t ON t.id=pv.tag_id
               WHERE pv.batch_id=? ORDER BY pv.id""", (bid,)).fetchall()
        ver_html = "".join(
            f"<tr><td class='num'>{esc(v['tag_no'])}</td>"
            f"<td>v{v['version']}</td><td class='price'>{money(v['price'])}</td>"
            f"<td>{change_label(v['change_type'])}</td>"
            f"<td class='num'>{esc(v['effective_at'])}</td></tr>" for v in versions)
        body = f"""
<h1>批次 {esc(b['no'])}</h1>
<p class="sub">{kind_label(b['kind'])} ｜ {status_badge(b['status'])}
 ｜ 创建 {esc(b['creator'] or '系统定时任务')} ｜ 计划生效 {esc(b['scheduled_at'])}
  {'｜ 促销结束 '+esc(b['promo_end_at']) if b['promo_end_at'] else ''}</p>
<div class="panel"><div style="margin-bottom:12px">{seg_bar(c, bid, detailed=True)}</div>
 {bulk} {rollback}
 <p class="muted" style="margin-bottom:0">回滚始终指向每个价签改价前的版本；重复执行只产生空操作记录，价格不再变动。</p>
</div>
<h2>批次明细</h2>
<table><thead><tr><th>价签编号</th><th>门店</th><th>商品</th>
<th>旧价 / 版本</th><th>目标价 / 版本</th><th>下发状态</th><th>尝试</th>
<th>最后回执</th><th>操作</th></tr></thead><tbody>{trs}</tbody></table>
<h2>本批次产生的价格版本（SQLite 永久留痕）</h2>
<table><thead><tr><th>价签</th><th>版本</th><th>价格</th><th>变更类型</th><th>生效时间</th></tr></thead>
<tbody>{ver_html or '<tr><td colspan="5" class="muted">尚无成功回执，未产生版本。</td></tr>'}</tbody></table>
<p><a href="/batches">← 返回批次列表</a></p>"""
        self._render("批次详情", body)

    def act_rollback(self, bid, f):
        rb_id = S.rollback_batch(self.conn, bid, f.get("username", "u_yang"),
                                 f.get("reason", "紧急回滚"), EslAdapter(self.conn))
        self._redirect(f"/rollbacks/{rb_id}")

    # ------------------------------------------------------------- 现场核验
    def page_inspect(self, tag_no="", verdict="", observed=""):
        c = self.conn
        recent = c.execute(
            "SELECT * FROM inspections ORDER BY id DESC LIMIT 8").fetchall()
        trs = ""
        for r in recent:
            bad = r["result"] == "mismatch"
            photo = (f"<a href='/photos/{Path(r['photo_path']).name}' target='_blank'>查看照片</a>"
                     if r["photo_path"] and not r["photo_path"].startswith("（") else "—")
            trs += (
                f'<tr class="{"row-err" if bad else ""}">'
                f'<td class="num">{esc(r["tag_no"])}</td>'
                f'<td class="price">{money(r["observed_price"])}</td>'
                f'<td class="price">{money(r["display_price"])}</td>'
                f'<td>{insp_badge(r["result"])}</td>'
                f'<td>{photo}</td>'
                f'<td class="num">{esc(r["inspected_at"])}</td></tr>')
        verdict_html = ""
        if verdict:
            v = verdict
            ok = v.get("result") == "match"
            cls = "ok" if ok else "bad"
            if v.get("result") == "tag_not_found":
                detail = f"价签编号 <b class='num'>{esc(v.get('tag_no',''))}</b> 在系统中不存在。"
                big = "扫码价签不存在"
            elif v.get("result") == "invalid":
                detail = "请录入标签上看到的有效价格数字。"
                big = "价格录入非法"
            elif ok:
                t = v["tag"]
                big = money(v["observed"])
                detail = (f"价签 <b class='num'>{esc(t['tag_no'])}</b> 现场显示与系统一致，"
                          f"当前展示版本 v{t['display_version']}。")
            else:
                t = v["tag"]
                big = f'{money(v["observed"])} ≠ {money(t["display_price"])}'
                detail = (f"系统期望价 <b class='price'>{money(t['expected_price'])}</b>，"
                          f"价签显示价 <b class='price'>{money(t['display_price'])}</b>，"
                          f"现场抄录 <b class='price'>{money(v['observed'])}</b>。请拍照留证并发起复核/回滚。")
            head = "核验通过" if ok else "发现价格不一致"
            verdict_html = f"""
<div class="verdict {cls}"><h3>{head}</h3>
 <div class="bignum">{big}</div><p style="margin:8px 0 0">{detail}</p></div>"""
        body = f"""
<h1>现场抽检核验</h1>
<p class="sub">用扫码枪或手动输入价签编号，逐位核对标签上的实际价格。</p>
<div class="scan-grid">
 <div>
  <div class="scan-hero">
   <div class="qricon">▦</div>
   <form method="post" action="/inspect/submit" enctype="multipart/form-data">
    <label>扫描 / 输入价签编号</label>
    <input type="text" name="tag_no" value="{esc(tag_no)}" placeholder="ESL-A-0001"
      autofocus required>
    <label>标签上看到的价格（元）</label>
    <input type="text" name="observed_price" value="{esc(observed)}" inputmode="decimal"
      placeholder="3.50" required>
    <label>抽检照片（可选）</label>
    <input type="file" name="photo" accept="image/*">
    <p style="margin-top:18px"><button class="btn" type="submit" style="font-size:16px;padding:11px 34px">
     提交核验</button></p>
   </form>
  </div>
  {verdict_html}
 </div>
 <div>
  <h2 style="margin-top:0">最近抽检</h2>
  <table><thead><tr><th>价签</th><th>现场价</th><th>显示价</th><th>结果</th><th>照片</th><th>时间</th></tr></thead>
  <tbody>{trs or '<tr><td colspan="6" class="muted">暂无记录</td></tr>'}</tbody></table>
 </div>
</div>"""
        self._render("现场核验", body)

    def act_inspect(self, f, files):
        photo_name = None
        if "photo" in files and files["photo"][1]:
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            original, data = files["photo"]
            suffix = Path(original).suffix or ".jpg"
            photo_name = f"insp_{now().strftime('%Y%m%d_%H%M%S')}_{f.get('tag_no','')}{suffix}"
            (PHOTO_DIR / photo_name).write_bytes(data)
        v = S.inspect(self.conn, f.get("tag_no", ""),
                      f.get("observed_price", ""),
                      f.get("inspector", "现场店员"), photo_name,
                      f.get("note", ""))
        self.page_inspect(f.get("tag_no", ""), v, f.get("observed_price", ""))

    def page_photo(self, name):
        p = (PHOTO_DIR / Path(name).name).resolve()
        if PHOTO_DIR.resolve() not in p.parents or not p.exists():
            return self._send(b"not found", 404, "text/plain")
        ctype = "image/svg+xml" if p.suffix == ".svg" else "image/png"
        if p.suffix in (".jpg", ".jpeg"):
            ctype = "image/jpeg"
        self._send(p.read_bytes(), 200, ctype)

    # ------------------------------------------------------------- 回滚记录
    def page_rollbacks(self):
        rows = self.conn.execute(
            """SELECT r.*, b.no batch_no, u.display_name op_name,
               (SELECT COUNT(*) FROM rollback_items WHERE rollback_id=r.id
                 AND status='success') ns,
               (SELECT COUNT(*) FROM rollback_items WHERE rollback_id=r.id
                 AND status='no_op') nn,
               (SELECT COUNT(*) FROM rollback_items WHERE rollback_id=r.id
                 AND status='failed') nf
               FROM rollbacks r JOIN batches b ON b.id=r.batch_id
               JOIN users u ON u.id=r.operator_id ORDER BY r.id DESC""").fetchall()
        trs = "".join(
            f"<tr><td><a href='/rollbacks/{r['id']}'>#{r['id']}</a></td>"
            f"<td><a href='/batches/{r['batch_id']}'>{esc(r['batch_no'])}</a></td>"
            f"<td>{rollback_status_badge(r['status'])}</td>"
            f"<td class='num'>{r['ns']} / {r['nn']} / {r['nf']}</td>"
            f"<td>{esc(r['reason'])}</td><td>{esc(r['op_name'])}</td>"
            f"<td class='num'>{esc(r['created_at'])}</td></tr>" for r in rows)
        body = f"""<h1>回滚记录</h1>
<p class="sub">列内数字依次为 成功 / 空操作（幂等）/ 失败。对同一批次重复回滚不会改变任何价格。</p>
<table><thead><tr><th>回滚单</th><th>原批次</th><th>结果</th><th>成功/空操作/失败</th>
<th>原因</th><th>操作人</th><th>时间</th></tr></thead>
<tbody>{trs or '<tr><td colspan="7" class="muted">暂无回滚</td></tr>'}</tbody></table>"""
        self._render("回滚记录", body)

    def page_rollback_detail(self, rid):
        c = self.conn
        head = c.execute(
            """SELECT r.*, b.no batch_no FROM rollbacks r
               JOIN batches b ON b.id=r.batch_id WHERE r.id=?""",
            (rid,)).fetchone()
        if not head:
            return self._render("未找到", "<h1>回滚单不存在</h1>")
        items = c.execute(
            """SELECT ri.*, t.tag_no FROM rollback_items ri
               JOIN tags t ON t.id=ri.tag_id
               WHERE ri.rollback_id=? ORDER BY ri.id""", (rid,)).fetchall()
        trs = "".join(
            f'<tr class="{"row-warn" if it["status"]=="no_op" else ""}>'
            f'<td class="num">{esc(it["tag_no"])}</td>'
            f'<td>v{it["from_version"]} → v{it["target_version"]}</td>'
            f'<td class="price">{money(it["target_price"])}</td>'
            f'<td>{rollback_item_badge(it["status"])}</td>'
            f'<td>{result_badge(it["result_code"], "ok" if it["status"]=="success" else ("warning" if it["status"]=="no_op" else "error"))}</td>'
            f'<td>{esc(it["message"])}</td></tr>' for it in items)
        body = f"""
<h1>回滚单 #{rid} · 批次 {esc(head['batch_no'])}</h1>
<p class="sub">结果 {rollback_status_badge(head['status'])} ｜ 原因 {esc(head['reason'])}
 ｜ 时间 {esc(head['created_at'])}</p>
<table><thead><tr><th>价签</th><th>版本走向（指向明确历史版本）</th>
<th>回滚目标价</th><th>执行情况</th><th>结果码</th><th>说明</th></tr></thead>
<tbody>{trs}</tbody></table>
<p><a href="/batches/{head['batch_id']}">← 返回批次</a></p>"""
        self._render("回滚详情", body)

    # ------------------------------------------------------------- 价签台账
    def page_tags(self):
        rows = self.conn.execute(
            """SELECT t.*, s.name store_name, sh.name shelf_name, p.name product_name,
                      p.min_price, p.max_price
               FROM tags t JOIN stores s ON s.id=t.store_id
               JOIN shelves sh ON sh.id=t.shelf_id
               JOIN products p ON p.sku=t.sku
               ORDER BY s.code, t.tag_no""").fetchall()
        trs = ""
        for t in rows:
            drift = abs(t["expected_price"] - t["display_price"]) > 0.001
            trs += (
                f'<tr class="{"row-err" if drift else ""}">'
                f'<td class="num">{esc(t["tag_no"])}</td>'
                f'<td>{esc(t["store_name"])}</td><td>{esc(t["shelf_name"])}</td>'
                f'<td>{esc(t["sku"])} {esc(t["product_name"])}</td>'
                f'<td class="num">{"在线" if t["online"] else "<b style=color:var(--red)>离线</b>"}</td>'
                f'<td class="price">{money(t["expected_price"])}</td>'
                f'<td class="price" {"style=color:var(--red)" if drift else ""}>{money(t["display_price"])}</td>'
                f'<td class="num">v{t["display_version"]}</td>'
                f'<td class="num muted">{money(t["min_price"])} ~ {money(t["max_price"])}</td></tr>')
        body = f"""<h1>价签台账</h1>
<p class="sub">每个价签同时维护<b>系统期望价</b>与<b>实际显示价</b>；
只有收到成功回执，显示价才会追上期望价。</p>
<table><thead><tr><th>价签编号</th><th>门店</th><th>货架</th><th>商品</th>
<th>链路状态</th><th>系统期望价</th><th>实际显示价</th><th>展示版本</th><th>限价区间</th></tr></thead>
<tbody>{trs}</tbody></table>"""
        self._render("价签台账", body)


class ServiceErrorPage(Exception):
    def __init__(self, msg):
        self.msg = msg


# ----------------------------------------------------------------- 徽章/条

def kind_label(k):
    return {"normal": "普通调价", "promo_restore": "促销到期恢复",
            "rollback": "回滚"}.get(k, k)


def change_label(k):
    return {"normal": "普通", "promo": "促销", "restore": "到期恢复",
            "rollback": "紧急回滚"}.get(k, k)


def status_badge(s):
    m = {"pending": ("等待中", "b-gray"), "partial": ("部分成功", "b-softred"),
         "succeeded": ("已完成", "b-softink"), "failed": ("全部失败", "b-red"),
         "rolled_back": ("已回滚", "b-ink")}
    text, cls = m.get(s, (s, "b-gray"))
    return f'<span class="badge {cls}">{text}</span>'


def import_status_badge(s):
    m = {"validating": ("校验中", "b-gray"), "validated": ("待审批", "b-softred"),
         "rejected": ("已驳回", "b-gray"), "approved": ("已批准", "b-softink"),
         "expired": ("已失效", "b-gray")}
    text, cls = m.get(s, (s, "b-gray"))
    return f'<span class="badge {cls}">{text}</span>'


def item_status_badge(s, code=""):
    m = {"waiting": ("等待下发", "b-gray"), "success": ("回执成功", "b-softink"),
         "failed": ("下发失败", "b-red"), "canceled": ("已撤销", "b-gray")}
    text, cls = m.get(s, (s, "b-gray"))
    title = R.label(code) if code else ""
    return f'<span class="badge {cls}" title="{esc(title)}">{text}</span>'


def insp_badge(s):
    m = {"match": ("一致", "b-softink"), "mismatch": ("价格不一致", "b-red"),
         "tag_not_found": ("价签不存在", "b-red"),
         "invalid": ("录入异常", "b-gray"), "no_photo": ("录入异常", "b-gray")}
    text, cls = m.get(s, (s, "b-gray"))
    return f'<span class="badge {cls}">{text}</span>'


def rollback_status_badge(s):
    m = {"succeeded": ("回滚完成", "b-softink"), "partial": ("部分成功", "b-softred"),
         "no_op": ("全部空操作（幂等）", "b-gray"), "pending": ("执行中", "b-gray")}
    text, cls = m.get(s, (s, "b-gray"))
    return f'<span class="badge {cls}">{text}</span>'


def rollback_item_badge(s):
    m = {"success": ("已恢复", "b-softink"), "no_op": ("空操作", "b-gray"),
         "failed": ("失败", "b-red")}
    text, cls = m.get(s, (s, "b-gray"))
    return f'<span class="badge {cls}">{text}</span>'


def result_badge(code, status):
    if code == R.OK:
        return '<span class="badge b-softink">通过</span>'
    cls = "b-red" if status == "error" else "b-gray"
    return f'<span class="badge {cls}">{esc(R.label(code))}</span>'


def seg_bar(conn, batch_id, detailed=False):
    st = S.batch_stats(conn, batch_id)
    total = st["total"] or 1
    segs = "".join(
        f'<span class="s-{k}" style="flex:{n} 0 0" title="{title}"></span>'
        for k, n, title in (("s", st["success"], "成功"),
                            ("w", st["waiting"], "等待"),
                            ("f", st["failed"], "失败")) if n)
    legend = (
        f'<div class="seglegend">'
        f'<b class="num" style="color:var(--ink)">{st["success"]}</b><i style="background:var(--ink)"></i>成功'
        f'<b class="num">{st["waiting"]}</b><i style="background:#D9D9DB"></i>等待'
        f'<b class="num" style="color:var(--red)">{st["failed"]}</b><i style="background:var(--red)"></i>失败'
        f'　共 <b class="num">{st["total"]}</b></div>') if detailed else ""
    inline = (f'<span class="num" style="font-size:12px;margin-left:8px">'
              f'{st["success"]}/{st["waiting"]}/{st["failed"]}</span>')
    return (f'<div style="display:flex;align-items:center">'
            f'<div class="segbar" style="flex:1">{segs or "<span class=s-w style=flex:1></span>"}</div>'
            f'{inline}{"" if detailed else ""}</div>{legend}')


def serve(host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"电子价签改价台已启动: http://{host}:{port}")
    return httpd
