# -*- coding: utf-8 -*-
"""电子价签改价与回滚核验系统 —— 本地 Web 后台（仅标准库）。

白 / 零售红 / 深灰 配色；分段批次进度条；差异表固定旧价·新价·生效时间列；
现场核验页大号扫码入口、价格等宽数字。
启动：python3 -m app.server  （或 run_demo.py --serve）
"""
from __future__ import annotations

import html
import io
import os
import sys
import warnings
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning)
import cgi  # noqa: E402  Python 3.11 可用

from . import db, services
from .scheduler import Scheduler

RED = "#D7001A"        # 零售红
DARK = "#2A2A2E"       # 深灰
GRAY = "#6E6E73"
LIGHT = "#F2F2F4"
GREEN_OK = "#1F7A3D"   # 仅文字小点缀

scheduler = Scheduler(interval_seconds=2.0, verbose=False)


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def money(x) -> str:
    if x is None:
        return "—"
    return f"{float(x):,.2f}"


# ---------------------------------------------------------------- 页面骨架

def layout(title: str, active: str, body: str, flash: str = "") -> str:
    nav = [
        ("/", "总览"),
        ("/imports", "调价导入"),
        ("/approvals", "审批队列"),
        ("/batches", "批次进度"),
        ("/inspect", "现场核验"),
        ("/rollback", "版本回滚"),
    ]
    nav_html = "".join(
        f'<a href="{u}" class="nav{" on" if u == active else ""}">{n}</a>' for u, n in nav
    )
    flash_cls = "flash good" if flash.startswith(("✅", "已", "OK")) else "flash"
    flash_html = f'<div class="{flash_cls}">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - 电子价签改价核验台</title>
<style>
:root {{ --red:{RED}; --dark:{DARK}; --gray:{GRAY}; --light:{LIGHT}; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#fff; color:var(--dark); }}
header {{ background:var(--dark); color:#fff; padding:0 28px; display:flex; align-items:center;
         height:58px; gap:24px; }}
header .logo {{ color:var(--red); font-weight:800; font-size:19px; letter-spacing:1px;
   white-space:nowrap; }}
header .logo small {{ color:#bbb; font-weight:400; margin-left:8px; font-size:12px; }}
.nav {{ color:#cfcfd2; text-decoration:none; padding:18px 10px; font-size:15px; }}
.nav:hover {{ color:#fff; }}
.nav.on {{ color:#fff; border-bottom:3px solid var(--red); }}
main {{ max-width:1180px; margin:0 auto; padding:26px 24px 60px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:var(--gray); font-size:13px; margin-bottom:20px; }}
.card {{ border:1px solid #e5e5e8; border-radius:10px; padding:18px 20px; margin-bottom:22px;
        background:#fff; }}
.card h2 {{ font-size:16px; margin:0 0 14px; padding-left:10px; border-left:4px solid var(--red); }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th,td {{ padding:9px 10px; border-bottom:1px solid #ededf0; text-align:left; vertical-align:middle;}}
th {{ background:var(--light); color:#444; font-weight:600; font-size:13px; }}
tr:hover td {{ background:#fafafa; }}
.num,.price {{ font-family:"SF Mono","Consolas","Courier New",monospace;
   font-variant-numeric:tabular-nums; letter-spacing:1px; white-space:nowrap; }}
.price {{ font-size:15px; font-weight:600; }}
.diff-table {{ table-layout:fixed; }}
.diff-table col.c-old {{ width:110px; }}
.diff-table col.c-new {{ width:110px; }}
.diff-table col.c-eff {{ width:175px; }}
.diff-table col.c-line {{ width:52px; }}
.diff-table col.c-store {{ width:90px; }}
.diff-table col.c-prod {{ width:100px; }}
.badge {{ display:inline-block; padding:2px 9px; border-radius:20px; font-size:12px;
   font-weight:600; white-space:nowrap; }}
.b-ok {{ background:#e8f4ec; color:{GREEN_OK}; }}
.b-err {{ background:#fde8eb; color:var(--red); }}
.b-wait {{ background:#eeeef0; color:#666; }}
.b-dark {{ background:#e3e3e6; color:var(--dark); }}
.btn {{ display:inline-block; border:none; cursor:pointer; border-radius:6px; padding:8px 16px;
   font-size:14px; background:var(--red); color:#fff; font-weight:600; text-decoration:none; }}
.btn:hover {{ filter:brightness(1.08); }}
.btn.gray {{ background:var(--dark); }}
.btn.light {{ background:#fff; color:var(--dark); border:1px solid #ccc; }}
.btn.sm {{ padding:4px 11px; font-size:12px; }}
input,select,textarea {{ font:inherit; padding:8px 10px; border:1px solid #c9c9cf;
   border-radius:6px; background:#fff; }}
textarea {{ width:100%; font-family:"SF Mono",Consolas,monospace; font-size:13px; }}
.flash {{ background:#fde8eb; border-left:4px solid var(--red); padding:10px 14px;
   border-radius:6px; margin-bottom:18px; font-size:14px; }}
.flash.good {{ background:#e8f4ec; border-color:{GREEN_OK}; color:#14532a; }}
/* 分段进度条 */
.segbar {{ display:flex; height:22px; border-radius:5px; overflow:hidden; background:#eeeef0;
   min-width:220px; }}
.segbar .s-ok {{ background:var(--dark); }}
.segbar .s-wait {{ background:#d3d3d8; }}
.segbar .s-fail {{ background:var(--red); }}
.legend {{ font-size:12px; color:var(--gray); margin-top:5px; }}
.legend b {{ font-weight:700; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin:0 4px 0 10px;}}
.kpis {{ display:flex; gap:16px; flex-wrap:wrap; }}
.kpi {{ flex:1; min-width:150px; border:1px solid #ececef; border-radius:10px; padding:14px 18px;}}
.kpi .v {{ font-size:28px; font-weight:800; font-family:"SF Mono",Consolas,monospace; }}
.kpi .l {{ color:var(--gray); font-size:13px; }}
.kpi .v.red {{ color:var(--red); }}
/* 核验页大号扫码 */
.scan-hero {{ text-align:center; padding:34px 20px 26px; border:2px dashed var(--red);
   border-radius:14px; background:#fffafa; }}
.scan-hero input[type=text] {{ width:min(560px,90%); font-size:30px; letter-spacing:3px;
   text-align:center; font-family:"SF Mono",Consolas,monospace; padding:16px;
   border-width:2px; border-color:var(--red); }}
.compare {{ display:flex; justify-content:center; gap:40px; margin-top:26px; flex-wrap:wrap;}}
.pricebox {{ text-align:center; min-width:230px; }}
.pricebox .tag {{ color:var(--gray); font-size:13px; }}
.pricebox .big {{ font-size:52px; font-weight:800;
   font-family:"SF Mono","Consolas","Courier New",monospace; letter-spacing:4px; }}
.pricebox .big.exp {{ color:var(--dark); }}
.pricebox .big.act {{ color:var(--red); }}
.pricebox .big.match {{ color:{GREEN_OK}; }}
.muted {{ color:var(--gray); }}
.row {{ display:flex; gap:14px; align-items:center; flex-wrap:wrap; }}
form.inline {{ display:inline; }}
img.thumb {{ max-width:90px; max-height:90px; border-radius:6px; border:1px solid #ddd; }}
.ver-current {{ background:#e8f4ec; }}
</style></head>
<body>
<header>
  <div class="logo">ESL 改价核验台<small>超市电子价签 · 批量改价 / 抽检 / 回滚</small></div>
  <nav>{nav_html}</nav>
</header>
<main>
<h1>{esc(title)}</h1>
<div class="sub">{esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))} · 演示数据 · SQLite 价格版本留痕</div>
{flash_html}
{body}
</main></body></html>"""


def badge(status: str) -> str:
    err = {"DUPLICATE_PRODUCT", "PRODUCT_NOT_FOUND", "STORE_NOT_FOUND", "PRICE_LIMIT",
           "TAG_OFFLINE", "PROMO_OVERLAP", "INVALID_TIME", "BATCH_CONFLICT", "BAD_NUMBER",
           "failed", "inconsistent", "rejected"}
    ok = {"ok", "success", "done", "consistent", "approved", "batched"}
    label = db.RESULT_LABELS.get(status, status)
    cls = "b-ok" if status in ok else ("b-err" if status in err else "b-wait")
    return f'<span class="badge {cls}">{esc(label)}</span>'


def segbar(p: dict) -> str:
    total = max(p["total"], 1)
    w = lambda k: f"{p[k] / total * 100:.2f}%"
    return (
        f'<div class="segbar" title="成功 {p["success"]} / 等待 {p["waiting"]} / 失败 {p["failed"]}">'
        f'<div class="s-ok" style="width:{w("success")}"></div>'
        f'<div class="s-wait" style="width:{w("waiting")}"></div>'
        f'<div class="s-fail" style="width:{w("failed")}"></div></div>'
        f'<div class="legend">成功 <b>{p["success"]}</b><span class="dot" style="background:{DARK}"></span>'
        f'等待 <b>{p["waiting"]}</b><span class="dot" style="background:#d3d3d8"></span>'
        f'失败 <b>{p["failed"]}</b><span class="dot" style="background:{RED}"></span>'
        f' ｜ 共 {p["total"]} 条</div>'
    )


# ---------------------------------------------------------------- 各页面

def page_dashboard(conn) -> str:
    k = conn.execute(
        "SELECT (SELECT COUNT(*) FROM tags WHERE status='online') online,"
        " (SELECT COUNT(*) FROM tags WHERE status='offline') offline,"
        " (SELECT COUNT(*) FROM batches WHERE status IN ('pending','active','partial')) inflight,"
        " (SELECT COUNT(*) FROM batch_items WHERE dispatch_status='failed') failed,"
        " (SELECT COUNT(*) FROM inspections WHERE result='inconsistent') bad_insp").fetchone()
    kpis = f"""
    <div class="kpis">
      <div class="kpi"><div class="v">{k['online']}</div><div class="l">在线价签</div></div>
      <div class="kpi"><div class="v red">{k['offline']}</div><div class="l">离线价签</div></div>
      <div class="kpi"><div class="v">{k['inflight']}</div><div class="l">在途批次</div></div>
      <div class="kpi"><div class="v red">{k['failed']}</div><div class="l">待重试失败项</div></div>
      <div class="kpi"><div class="v red">{k['bad_insp']}</div><div class="l">现场价格不一致</div></div>
    </div>"""
    tags = conn.execute(
        "SELECT t.*, p.name product_name, s.name store_name FROM tags t"
        " JOIN products p ON p.id=t.product_id JOIN stores s ON s.id=t.store_id ORDER BY t.id").fetchall()
    rows = ""
    for t in tags:
        cv = services.current_version(conn, t["store_id"], t["product_id"])
        drift = (t["displayed_version_id"] != cv["id"]) if cv else False
        exp = money(cv["price"]) if cv else "—"
        disp = money(t["displayed_price"])
        drift_html = '<span class="badge b-err">显示≠期望</span>' if drift else '<span class="badge b-ok">已同步</span>'
        st = badge("TAG_OFFLINE") if t["status"] != "online" else '<span class="badge b-ok">在线</span>'
        rows += (f"<tr><td class='num'>{esc(t['id'])}</td><td>{esc(t['store_name'])}</td>"
                 f"<td>{esc(t['product_name'])}</td><td class='price'>¥{disp}</td>"
                 f"<td class='price'>¥{exp}</td><td>{st}</td><td>{drift_html}</td></tr>")
    table = f"""
    <div class="card"><h2>价签实际显示价 vs 系统期望价（任何时候都保留两份）</h2>
    <table><thead><tr><th>价签编号</th><th>门店</th><th>商品</th><th>实际显示价</th>
    <th>系统期望价</th><th>状态</th><th>一致性</th></tr></thead><tbody>{rows}</tbody></table></div>"""
    return layout("总览", "/", kpis + table)


def _store_product_options(conn):
    stores = conn.execute("SELECT * FROM stores ORDER BY id").fetchall()
    products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return stores, products


def page_imports(conn, flash="") -> str:
    stores, _ = _store_product_options(conn)
    store_opts = "".join(f"<option value='{s['id']}'>{esc(s['id'])} {esc(s['name'])}</option>" for s in stores)
    sample = ("store_id,product_id,new_price,effective_at,promo_start,promo_end\n"
              "S001,100001,1.80,now,,\n"
              "S001,100002,3.20,now,,\n"
              "# 下面几行故意触发各类独立结果：\n"
              "S001,100002,3.10,now,,            ← 重复商品\n"
              "S001,100004,999.00,now,,          ← 价格越限\n"
              "S001,100005,9.90,now,,            ← 价签离线\n"
              "S999,100001,1.80,now,,            ← 门店不存在\n"
              "S001,888888,1.80,now,,            ← 商品不存在\n"
              "S002,100002,2.99,2020-01-01 00:00:00,,   ← 生效时间无效（过去时间）")
    lists = conn.execute(
        "SELECT il.*, u.display_name submitter FROM import_lists il JOIN users u ON u.id=il.submitted_by"
        " ORDER BY il.id DESC LIMIT 10").fetchall()
    blocks = ""
    for il in lists:
        items = services.import_diff_items(conn, il["id"])
        trows = ""
        for it in items:
            trows += (
                f"<tr><td>{it['line_no']}</td>"
                f"<td class='num'>{esc(it['store_id'])}</td>"
                f"<td class='num'>{esc(it['product_id'])}</td>"
                f"<td>{esc(it['product_name'] or '—')}</td>"
                f"<td class='price'>¥{money(it['old_price'])}</td>"
                f"<td class='price'>¥{money(it['new_price'])}</td>"
                f"<td class='num'>{esc(it['effective_at'] or '—')}</td>"
                f"<td>{badge(it['check_status'])}</td>"
                f"<td class='muted'>{esc(it['check_message'] or '')}</td></tr>")
        blocks += f"""
        <div class="card"><h2>{esc(il['code'])}　{badge(il['status'])}
          <span class="muted" style="font-weight:400;font-size:13px">
          提交人 {esc(il['submitter'])} · {esc(il['submitted_at'])} · {esc(il['note'] or '')}</span></h2>
        <table class="diff-table">
          <colgroup><col class="c-line"><col class="c-store"><col class="c-prod">
          <col><col class="c-old"><col class="c-new"><col class="c-eff"><col style="width:120px"><col></colgroup>
          <thead><tr><th>#</th><th>门店</th><th>商品</th><th>名称</th>
          <th>旧价(固定列)</th><th>新价(固定列)</th><th>生效时间(固定列)</th><th>校验结果</th><th>说明</th></tr></thead>
          <tbody>{trows}</tbody></table></div>"""
    body = f"""
    <div class="card"><h2>导入调价清单（CSV）</h2>
      <form method="post" action="/actions/import" enctype="multipart/form-data">
        <div class="row" style="margin-bottom:10px">
          <label>提交人
            <select name="submitter">
              <option value="u_op">u_op 运营员·小周</option>
              <option value="u_mgr">u_mgr 审批经理·老陈</option>
            </select></label>
          <label>生效基准（演示）<select name="when">
            <option value="now">立即生效</option>
            <option value="future">定时生效（+1 小时，可观察等待）</option>
          </select></label>
          <label>CSV 文件 <input type="file" name="csv"></label>
          <button class="btn" type="submit">上传并校验</button>
        </div>
        <label>或直接粘贴 CSV（列：store_id,product_id,new_price,effective_at,promo_start,promo_end；
        effective_at 填 now / +1h / 或 2026-10-01 09:00:00）</label>
        <textarea name="csv_text" rows="9">{esc(sample)}</textarea>
      </form>
      <p class="muted" style="font-size:13px">提示：示例中的中文备注行（#…）会被自动忽略；
      “明天/昨天”仅为注释提示，正式提交请用 <b>now</b> 或 <b>+1h</b> 或标准日期。</p>
    </div>
    {blocks}"""
    return layout("调价导入", "/imports", body, flash)


def page_approvals(conn, flash="") -> str:
    pend = services.pending_approvals(conn)
    rows = ""
    for il in pend:
        items = services.import_diff_items(conn, il["id"])
        oks = [i for i in items if i["check_status"] == "ok"]
        errs = [i for i in items if i["check_status"] != "ok"]
        lines = "".join(
            f"<tr><td class='num'>{esc(i['store_id'])}</td><td class='num'>{esc(i['product_id'])}</td>"
            f"<td class='price'>¥{money(i['old_price'])}</td><td class='price'>¥{money(i['new_price'])}</td>"
            f"<td class='num'>{esc(i['effective_at'])}</td><td>{badge(i['check_status'])}</td></tr>"
            for i in items)
        rows += f"""
        <div class="card"><h2>{esc(il['code'])}　提交人：{esc(il['submitter_name'])}
          <span class="muted" style="font-weight:400;font-size:13px">{esc(il['submitted_at'])}</span></h2>
        <p>通过 <b class="num">{len(oks)}</b> 条，拦截 <b class="num" style="color:var(--red)">{len(errs)}</b> 条。
           审批通过后仅“校验通过”的明细生成改价批次。</p>
        <table class="diff-table"><colgroup><col class="c-store"><col class="c-prod">
        <col class="c-old"><col class="c-new"><col class="c-eff"><col style="width:120px"></colgroup>
        <thead><tr><th>门店</th><th>商品</th><th>旧价</th><th>新价</th><th>生效时间</th><th>结果</th></tr></thead>
        <tbody>{lines}</tbody></table>
        <div class="row" style="margin-top:14px">
          <form class="inline" method="post" action="/actions/approve">
            <input type="hidden" name="import_id" value="{il['id']}">
            <label>审批人 <select name="approver">
              <option value="u_mgr">u_mgr 审批经理·老陈</option>
              <option value="u_op">u_op 运营员·小周（提交人本人，应被拒绝）</option>
            </select></label>
            <button class="btn" type="submit">审批通过并生成批次</button>
          </form>
          <form class="inline" method="post" action="/actions/reject">
            <input type="hidden" name="import_id" value="{il['id']}">
            <input type="hidden" name="approver" value="u_mgr">
            <input name="reason" placeholder="驳回原因" style="width:220px">
            <button class="btn light" type="submit">驳回</button>
          </form>
        </div></div>"""
    if not rows:
        rows = '<div class="card muted">审批队列为空。先到“调价导入”提交清单。</div>'
    return layout("审批队列", "/approvals", rows, flash)


def page_batches(conn, flash="") -> str:
    batches = services.list_batches(conn)
    type_label = {"price_change": "改价批次", "promo_restore": "促销恢复", "emergency_rollback": "紧急回滚"}
    blocks = ""
    for b in batches:
        p = services.batch_progress(conn, b["id"])
        items = conn.execute(
            "SELECT bi.*, p.name product_name FROM batch_items bi"
            " JOIN products p ON p.id=bi.product_id WHERE bi.batch_id=? ORDER BY bi.id",
            (b["id"],)).fetchall()
        trows = ""
        for it in items:
            st_badge = {"success": "success", "failed": "failed"}.get(it["dispatch_status"], "ok")
            cell = badge(st_badge if it["dispatch_status"] != "waiting" else "waiting")
            retry = ""
            if it["dispatch_status"] == "failed":
                retry = (f'<form class="inline" method="post" action="/actions/retry">'
                         f'<input type="hidden" name="item_id" value="{it["id"]}">'
                         f'<button class="btn sm" type="submit">单项重试</button></form>')
            trows += (f"<tr><td class='num'>{esc(it['tag_id'])}</td><td>{esc(it['product_name'])}</td>"
                      f"<td class='price'>¥{money(it['old_price'])}</td>"
                      f"<td class='price'>¥{money(it['new_price'])}</td>"
                      f"<td class='num'>v{it['version_id'] or '—'}</td>"
                      f"<td class='num'>{it['attempts']}</td><td>{cell}</td>"
                      f"<td class='muted'>{esc(it['last_error'] or '')}</td><td>{retry}</td></tr>")
        blocks += f"""
        <div class="card"><h2>{esc(b['code'])}　<span class="badge b-dark">{esc(type_label.get(b['batch_type'], b['batch_type']))}</span>
          {badge(b['status'])}
          <span class="muted" style="font-weight:400;font-size:13px">
          创建 {esc(b['created_at'])} · {esc(b['note'] or '')}</span></h2>
          {segbar(p)}
          <table style="margin-top:12px"><thead><tr><th>价签</th><th>商品</th><th>旧价</th><th>新价</th>
          <th>期望版本</th><th>尝试</th><th>下发结果</th><th>错误</th><th>操作</th></tr></thead>
          <tbody>{trows}</tbody></table>
        </div>"""
    if not blocks:
        blocks = '<div class="card muted">暂无批次。审批通过后在此查看分段进度。</div>'
    return layout("批次进度", "/batches", blocks, flash)


def page_inspect(conn, tag_id="", flash="", result=None) -> str:
    find_box = f"""
    <div class="scan-hero">
      <form method="get" action="/inspect">
        <div style="font-size:18px;font-weight:700;color:var(--red);margin-bottom:14px">
          ▍ 现场扫码核验入口</div>
        <input type="text" name="tag" value="{esc(tag_id)}" placeholder="扫描 / 输入价签编号，如 ESL-0001"
               autofocus autocomplete="off">
        <div style="margin-top:14px"><button class="btn" type="submit" style="padding:12px 34px;font-size:17px">
          查 找 价 签</button></div>
      </form>
    </div>"""
    detail = ""
    tag = None
    if tag_id:
        tag = conn.execute(
            "SELECT t.*, p.name product_name, p.barcode, s.name store_name FROM tags t"
            " JOIN products p ON p.id=t.product_id JOIN stores s ON s.id=t.store_id"
            " WHERE t.id=?", (tag_id.strip(),)).fetchone()
        if not tag:
            detail = f'<div class="card flash">未找到价签 {esc(tag_id)}，请核对编号。</div>'
        else:
            cv = services.current_version(conn, tag["store_id"], tag["product_id"])
            drift = abs((tag["displayed_price"] or 0) - (cv["price"] if cv else 0)) >= 0.005
            disp_cls = "act" if drift else "match"
            recent = conn.execute(
                "SELECT * FROM inspections WHERE tag_id=? ORDER BY id DESC LIMIT 5", (tag["id"],)).fetchall()
            rrows = "".join(
                f"<tr><td class='num'>{esc(r['checked_at'])}</td>"
                f"<td class='price'>¥{money(r['expected_price'])}</td>"
                f"<td class='price'>¥{money(r['actual_price'])}</td>"
                f"<td>{badge(r['result'])}</td><td>{esc(r['inspector'] or '')}</td></tr>"
                for r in recent)
            off = tag["status"] != "online"
            detail = f"""
            <div class="card"><h2>{esc(tag['id'])} · {esc(tag['store_name'])} · {esc(tag['product_name'])}
              {"　" + badge("TAG_OFFLINE") if off else "　<span class='badge b-ok'>在线</span>"}</h2>
              <div class="compare">
                <div class="pricebox"><div class="tag">系统期望价（逐位核对）</div>
                  <div class="big exp">¥ {cv['price']:>8.2f}</div></div>
                <div class="pricebox"><div class="tag">价签实际显示{'（≠期望）' if drift else '（一致）'}</div>
                  <div class="big {disp_cls}">¥ {float(tag['displayed_price'] or 0):>8.2f}</div></div>
              </div>
              <form method="post" action="/actions/inspect" enctype="multipart/form-data" style="margin-top:22px">
                <input type="hidden" name="tag" value="{esc(tag['id'])}">
                <div class="row">
                  <label>现场录入实际价 <input name="actual" inputmode="decimal"
                     style="font-family:'SF Mono',Consolas,monospace;font-size:20px;width:150px;letter-spacing:2px"
                     placeholder="0.00" {'disabled' if off else ''}></label>
                  <label>巡检员 <input name="inspector" value="u_insp 巡检员·小吴" style="width:180px"></label>
                  <label>抽检照片 <input type="file" name="photo" accept="image/*"></label>
                  <button class="btn gray" type="submit" {'disabled' if off else ''}>提交核验</button>
                </div>
              </form>
              <p class="muted" style="font-size:13px;margin-top:10px">
                条形码 {esc(tag['barcode'])} · 货架 {esc(tag['shelf_id'])} ·
                照片保存于本地目录 data/photos/</p>
            </div>
            <div class="card"><h2>本签最近抽检记录</h2>
              <table><thead><tr><th>时间</th><th>期望价</th><th>现场价</th><th>结果</th><th>巡检员</th></tr></thead>
              <tbody>{rrows or '<tr><td colspan=5 class=muted>暂无记录</td></tr>'}</tbody></table></div>"""
    return layout("现场核验", "/inspect", find_box + detail, flash)


def page_rollback(conn, store="", product="", flash="") -> str:
    stores, products = _store_product_options(conn)
    s_opts = "".join(f"<option value='{s['id']}' {'selected' if s['id']==store else ''}>{s['id']} {esc(s['name'])}</option>" for s in stores)
    p_opts = "".join(f"<option value='{p['id']}' {'selected' if p['id']==product else ''}>{p['id']} {esc(p['name'])}</option>" for p in products)
    pick = f"""
    <div class="card"><h2>选择回滚对象</h2>
      <form method="get" action="/rollback" class="row">
        <label>门店 <select name="store">{s_opts}</select></label>
        <label>商品 <select name="product">{p_opts}</select></label>
        <button class="btn gray" type="submit">查看价格版本链</button>
      </form></div>"""
    chain = ""
    if store and product:
        tag = services.get_tag(conn, store, product)
        cv = services.current_version(conn, store, product)
        vers = services.version_history(conn, store, product)
        vrows = ""
        type_label = {"regular": "常规价", "promo": "促销价", "restore": "到期恢复", "rollback": "回滚版本"}
        for v in vers:
            cur = v["id"] == cv["id"]
            cls = " class='ver-current'" if cur else ""
            tgt = f" → 指向 v{v['target_version_id']}" if v["target_version_id"] else ""
            cur_tag = "<span class='badge b-ok'>当前期望</span>" if cur else ""
            btn = ""
            if not cur:
                btn = (f'<form class="inline" method="post" action="/actions/rollback">'
                       f'<input type="hidden" name="store" value="{store}">'
                       f'<input type="hidden" name="product" value="{product}">'
                       f'<input type="hidden" name="target" value="{v["id"]}">'
                       f'<button class="btn sm" type="submit">紧急回滚到此版本</button></form>')
            vrows += (f"<tr{cls}><td class='num'>v{v['id']} {cur_tag}</td>"
                      f"<td>{esc(type_label.get(v['change_type'], v['change_type']))}{esc(tgt)}</td>"
                      f"<td class='price'>¥{money(v['price'])}</td>"
                      f"<td class='num'>{esc(v['effective_at'])}</td>"
                      f"<td class='num'>{esc(v['promo_start'] or '—')} ~ {esc(v['promo_end'] or '—')}</td>"
                      f"<td>{btn}</td></tr>")
        rbs = conn.execute(
            "SELECT * FROM rollbacks WHERE store_id=? AND product_id=? ORDER BY id DESC LIMIT 10",
            (store, product)).fetchall()
        rb_rows = "".join(
            f"<tr><td class='num'>{esc(r['code'])}</td><td class='num'>v{r['from_version_id']} → v{r['target_version_id']}</td>"
            f"<td>{badge('success' if r['status']=='success' else ('ok' if r['status']=='idempotent_skip' else 'failed'))}</td>"
            f"<td class='num'>{esc(r['executed_at'])}</td><td>{esc(r['note'] or '')}</td></tr>"
            for r in rbs)
        chain = f"""
        <div class="card"><h2>{esc(tag['id']) if tag else store} · 价格版本链
          <span class="muted" style="font-weight:400;font-size:13px">
          实际显示 ¥{money(tag['displayed_price']) if tag else None} ｜ 当前期望 ¥{money(cv['price']) if cv else None}</span></h2>
        <p class="muted" style="font-size:13px">回滚必须指向一个明确的历史版本；重复执行同一目标只记录
        <b>idempotent_skip</b>，不产生新版本、不重复下发。</p>
        <table><thead><tr><th>版本</th><th>类型</th><th>价格</th><th>生效时间</th><th>促销时段</th><th>操作</th></tr></thead>
        <tbody>{vrows}</tbody></table></div>
        <div class="card"><h2>回滚记录</h2>
        <table><thead><tr><th>单号</th><th>版本走向</th><th>结果</th><th>时间</th><th>备注</th></tr></thead>
        <tbody>{rb_rows or '<tr><td colspan=5 class=muted>暂无回滚</td></tr>'}</tbody></table></div>"""
    return layout("版本回滚", "/rollback", pick + chain, flash)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ESLDemo/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, content: str, code: int = 200, redirect: str | None = None):
        if redirect:
            from urllib.parse import quote
            # Location 头只能是 latin-1：对路径后的查询串做百分号编码
            if "?" in redirect:
                base, query = redirect.split("?", 1)
                redirect = base + "?" + quote(query, safe="=&")
            self.send_response(303)
            self.send_header("Location", redirect)
            self.end_headers()
            return
        data = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        flash = q.pop("_m", "")
        conn = db.get_conn()
        try:
            scheduler.tick()
            if u.path == "/":
                out = page_dashboard(conn)
            elif u.path == "/imports":
                out = page_imports(conn, flash)
            elif u.path == "/approvals":
                out = page_approvals(conn, flash)
            elif u.path == "/batches":
                out = page_batches(conn, flash)
            elif u.path == "/inspect":
                out = page_inspect(conn, q.get("tag", ""), flash)
            elif u.path == "/rollback":
                out = page_rollback(conn, q.get("store", "S001"), q.get("product", "100001"), flash)
            else:
                out = layout("404", "", '<div class="card">页面不存在</div>'); code = 404
                self._send(out, 404); return
        finally:
            conn.close()
        self._send(out)

    def do_POST(self):
        u = urlparse(self.path)
        ctype = self.headers.get("Content-Type", "")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype})

        def fv(name, default=""):
            return form.getfirst(name, default)

        conn = db.get_conn()
        try:
            if u.path == "/actions/import":
                loc = self._action_import(conn, form, fv)
            elif u.path == "/actions/approve":
                import_id = int(fv("import_id", "0"))
                r = services.approve_import(import_id, fv("approver"), conn)
                good = r.get("ok")
                loc = (f"/batches?_m={'已生成批次 '+r['batch']['code'] if good else esc(r['message'])}"
                       f"&{'good=1' if good else ''}")
            elif u.path == "/actions/reject":
                services.reject_import(int(fv("import_id", "0")), fv("approver"), fv("reason"), conn)
                loc = "/approvals?_m=已驳回"
            elif u.path == "/actions/retry":
                r = services.retry_item(int(fv("item_id", "0")), conn)
                loc = f"/batches?_m={esc(r['message'])}"
            elif u.path == "/actions/inspect":
                loc = self._action_inspect(conn, form, fv)
            elif u.path == "/actions/rollback":
                r = services.rollback(fv("store"), fv("product"), int(fv("target", "0")),
                                      executed_by="u_mgr", conn=conn)
                msg = ("✅ " if r.get("ok") else "") + r["message"]
                loc = (f"/rollback?store={fv('store')}&product={fv('product')}"
                       f"&_m={esc(msg)}")
            else:
                loc = "/"
            conn.commit()
        finally:
            conn.close()
        self._send("", redirect=loc)

    # -- 动作辅助 --

    def _action_import(self, conn, form, fv):
        submitter = fv("submitter", "u_op")
        when = fv("when", "now")
        ref = datetime.now()
        # 保存文件 / 文本到本地导入目录
        raw_text = fv("csv_text", "")
        upload = form["csv"] if "csv" in form else None
        ts = ref.strftime("%Y%m%d%H%M%S")
        path = os.path.join(db.IMPORT_DIR, f"manual_{ts}.csv")
        saved = False
        if upload is not None and getattr(upload, "filename", ""):
            data = upload.file.read()
            with open(path, "wb") as f:
                f.write(data)
            saved = True
        lines = [ln for ln in raw_text.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        # 去掉示例注释尾巴 “  ← ...”
        clean = []
        for ln in lines:
            if "←" in ln:
                ln = ln.split("←")[0]
            clean.append(ln)
        if clean:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(clean) + "\n")
            saved = True
        if not saved:
            return "/imports?_m=请先选择文件或粘贴清单内容"
        # 时间宏替换：now / +Nh
        eff_ref = ref if when == "now" else ref.replace(minute=0, second=0, microsecond=0)
        rows = list(_read_clean_csv(path))
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = None
            import csv as _csv
            for r in rows:
                if w is None:
                    w = _csv.DictWriter(f, fieldnames=r.keys())
                    w.writeheader()
                eff = (r.get("effective_at") or "").strip()
                r["effective_at"] = _time_macro(eff, ref, when)
                for k in ("promo_start", "promo_end"):
                    if (r.get(k) or "").strip():
                        r[k] = _time_macro(r[k].strip(), ref, when)
                w.writerow(r)
        res = services.import_price_list(path, submitter, f"手动导入/{when}",
                                         conn=conn, ref_time=ref)
        return f"/imports?_m={esc(res['code'] + ' 校验完成：通过 ' + str(res['ok']) + ' / 拦截 ' + str(res['rejected']))}"

    def _action_inspect(self, conn, form, fv):
        tag = fv("tag")
        actual_raw = fv("actual", "")
        try:
            actual = float(actual_raw) if actual_raw else None
        except ValueError:
            actual = None
        photo_path = ""
        if "photo" in form and getattr(form["photo"], "filename", ""):
            up = form["photo"]
            ext = os.path.splitext(up.filename)[1] or ".jpg"
            fname = f"{tag.replace('/', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
            p = os.path.join(db.PHOTO_DIR, fname)
            with open(p, "wb") as f:
                f.write(up.file.read())
            photo_path = os.path.relpath(p, db.BASE_DIR)
        r = services.inspect_tag(tag, actual, fv("inspector", "u_insp"),
                                 photo_path, "", conn=conn)
        if not r.get("ok"):
            msg = r["message"]
        else:
            icon = {"consistent": "✅ 一致", "inconsistent": "⚠ 不一致", "offline": "⏻ 离线"}[r["result"]]
            msg = f"{icon}：{r['message']}"
        return f"/inspect?tag={tag}&_m={esc(msg)}"


def _read_clean_csv(path):
    import csv
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            yield row


def _time_macro(s: str, ref: datetime, when: str) -> str:
    s = s.strip()
    from datetime import timedelta
    if s.lower() == "now":
        return ref.strftime(services.DT_FMT)
    if s.startswith("+") and s.endswith("h"):
        try:
            h = float(s[1:-1])
            return (ref + timedelta(hours=h)).strftime(services.DT_FMT)
        except ValueError:
            pass
    return s


def serve(host="127.0.0.1", port=8000):
    scheduler.start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"电子价签改价核验台已启动: http://{host}:{port}")
    print("页面：/ 总览  /imports 调价导入  /approvals 审批队列  /batches 批次进度  /inspect 现场核验  /rollback 版本回滚")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    serve(port=port)
