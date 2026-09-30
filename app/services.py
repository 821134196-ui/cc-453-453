# -*- coding: utf-8 -*-
"""核心业务服务：导入校验、审批、批次下发/重试、定时生效与到期恢复、抽检、版本回滚。

结果码（独立结果，互斥）：
  DUPLICATE_PRODUCT  重复商品
  PRICE_LIMIT        价格越限
  TAG_OFFLINE        价签离线
  PROMO_OVERLAP      促销时段重叠
  PRODUCT_NOT_FOUND / STORE_NOT_FOUND
  INVALID_TIME       生效时间无效
  BATCH_CONFLICT     同门店同商品存在在途批次（两个批次不能同时改）
  BAD_NUMBER         价格格式错误
审批：APPROVAL_SELF（不能审批自己提交的清单）
回滚：ROLLBACK_NO_TARGET（必须指向明确版本），重复执行幂等无副作用
"""
from __future__ import annotations

import csv
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

from . import db

DT_FMT = "%Y-%m-%d %H:%M:%S"
D_FMT = "%Y-%m-%d"

# 批次在途状态（互斥判定依据）
ACTIVE_BATCH_STATUSES = ("pending", "active", "partial")


# ---------------------------------------------------------------- 工具

def parse_dt(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for f in (DT_FMT, D_FMT):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def fmt_dt(dt: datetime) -> str:
    return dt.strftime(DT_FMT)


def make_code(prefix: str, at: datetime | None = None) -> str:
    at = at or datetime.now()
    return f"{prefix}-{at.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"


def row_to_dict(row: sqlite3.Row | None):
    return dict(row) if row is not None else None


def resolve_user(conn, ident) -> int | None:
    """用户标识兼容数字 id 与用户名（u_op 等）。"""
    if ident is None:
        return None
    if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
        row = conn.execute("SELECT id FROM users WHERE id=?", (int(ident),)).fetchone()
        return row["id"] if row else None
    row = conn.execute("SELECT id FROM users WHERE username=?", (str(ident),)).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------- 查询

def get_tag(conn, store_id: str, product_id: str):
    return conn.execute(
        "SELECT t.*, p.name AS product_name, s.name AS store_name FROM tags t"
        " JOIN products p ON p.id=t.product_id JOIN stores s ON s.id=t.store_id"
        " WHERE t.store_id=? AND t.product_id=?",
        (store_id, product_id),
    ).fetchone()


def current_version(conn, store_id: str, product_id: str):
    return conn.execute(
        "SELECT pv.* FROM current_prices cp"
        " JOIN price_versions pv ON pv.id=cp.current_version_id"
        " WHERE cp.store_id=? AND cp.product_id=?",
        (store_id, product_id),
    ).fetchone()


def version_history(conn, store_id: str, product_id: str, limit: int = 20):
    return conn.execute(
        "SELECT pv.* FROM price_versions pv WHERE pv.store_id=? AND pv.product_id=?"
        " ORDER BY pv.id DESC LIMIT ?",
        (store_id, product_id, limit),
    ).fetchall()


def active_batch_for(conn, store_id: str, product_id: str, exclude_batch_id: int | None = None):
    """在途批次（pending/active/partial）即构成互斥。"""
    sql = (
        "SELECT b.* FROM batch_items bi JOIN batches b ON b.id=bi.batch_id"
        " WHERE bi.store_id=? AND bi.product_id=? AND b.status IN (?,?,?)"
    )
    args = [store_id, product_id, *ACTIVE_BATCH_STATUSES]
    if exclude_batch_id:
        sql += " AND b.id<>?"
        args.append(exclude_batch_id)
    sql += " ORDER BY b.id DESC LIMIT 1"
    return conn.execute(sql, args).fetchone()


# ---------------------------------------------------------------- 导入 + 差异校验

def _read_csv_rows(path: str):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def import_price_list(csv_path: str, submitted_by: int, note: str = "",
                      conn: sqlite3.Connection | None = None,
                      ref_time: datetime | None = None) -> dict:
    """读取调价清单 CSV → 落原始行 → 逐行差异校验 → 返回导入单。

    CSV 列：store_id,product_id,new_price,effective_at[,promo_start,promo_end]
    """
    own = False
    if conn is None:
        conn = db.get_conn(); own = True
    ref = ref_time or datetime.now()
    uid = resolve_user(conn, submitted_by)
    if uid is None:
        raise ValueError(f"未知提交人: {submitted_by}")
    rows = _read_csv_rows(csv_path)
    code = make_code("IMP", ref)
    cur = conn.execute(
        "INSERT INTO import_lists(code,filename,submitted_by,submitted_at,note,status)"
        " VALUES (?,?,?,?,?, 'submitted')",
        (code, os.path.basename(csv_path), uid, fmt_dt(ref), note),
    )
    imp_id = cur.lastrowid

    seen: set[tuple[str, str]] = set()
    ok_count = 0
    for idx, r in enumerate(rows, start=1):
        store_id = (r.get("store_id") or "").strip()
        product_id = (r.get("product_id") or "").strip()
        raw_new = (r.get("new_price") or "").strip()
        eff_s = (r.get("effective_at") or "").strip()
        ps_s = (r.get("promo_start") or "").strip()
        pe_s = (r.get("promo_end") or "").strip()
        status, msg, old_price, eff_dt, ps_dt, pe_dt = _validate_row(
            conn, store_id, product_id, raw_new, eff_s, ps_s, pe_s, seen, ref
        )
        if status == "ok":
            ok_count += 1
            seen.add((store_id, product_id))
        conn.execute(
            "INSERT INTO import_items(import_list_id,line_no,store_id,product_id,old_price,"
            "new_price,effective_at,promo_start,promo_end,check_status,check_message)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (imp_id, idx, store_id or None, product_id or None, old_price,
             _to_float(raw_new), fmt_dt(eff_dt) if eff_dt else (eff_s or None),
             fmt_dt(ps_dt) if ps_dt else (ps_s or None),
             fmt_dt(pe_dt) if pe_dt else (pe_s or None), status, msg),
        )

    final_status = "validated" if (rows and ok_count > 0) else "rejected"
    conn.execute(
        "UPDATE import_lists SET status=?, validated_at=? WHERE id=?",
        (final_status, fmt_dt(ref), imp_id),
    )
    if own:
        conn.commit(); conn.close()
    return {"import_id": imp_id, "code": code, "total": len(rows),
            "ok": ok_count, "rejected": len(rows) - ok_count, "status": final_status}


def _to_float(s: str):
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


def _validate_row(conn, store_id, product_id, raw_new, eff_s, ps_s, pe_s, seen, ref):
    """返回 (status, message, old_price, eff_dt, promo_start_dt, promo_end_dt)。"""
    store = conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone() if store_id else None
    if not store:
        return "STORE_NOT_FOUND", f"门店编号 {store_id or '(空)'} 不存在", None, None, None, None
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone() if product_id else None
    if not product:
        return "PRODUCT_NOT_FOUND", f"商品编号 {product_id or '(空)'} 不存在", None, None, None, None
    new_price = _to_float(raw_new)
    if new_price is None or new_price <= 0:
        return "BAD_NUMBER", f"新价格 '{raw_new}' 不是有效正数", None, None, None, None
    if (store_id, product_id) in seen:
        return "DUPLICATE_PRODUCT", "同一清单内重复出现的商品行", None, None, None, None

    tag = get_tag(conn, store_id, product_id)
    cv = current_version(conn, store_id, product_id)
    old_price = cv["price"] if cv else (tag["displayed_price"] if tag else None)

    # 价格越限：相对当前期望价
    if old_price is not None:
        if new_price < old_price * db.MIN_PRICE_RATIO - 1e-9 or \
           new_price > old_price * db.MAX_PRICE_RATIO + 1e-9:
            return ("PRICE_LIMIT",
                    f"新价 {new_price:.2f} 超出允许范围 "
                    f"[{old_price * db.MIN_PRICE_RATIO:.2f}, {old_price * db.MAX_PRICE_RATIO:.2f}]",
                    old_price, None, None, None)

    # 时间
    eff_dt = parse_dt(eff_s)
    ps_dt = parse_dt(ps_s)
    pe_dt = parse_dt(pe_s)
    if eff_dt is None:
        return "INVALID_TIME", f"生效时间 '{eff_s}' 无法解析（YYYY-MM-DD HH:MM:SS）", old_price, None, None, None
    if eff_dt < ref - timedelta(minutes=1):
        return "INVALID_TIME", "生效时间早于当前时间", old_price, eff_dt, None, None
    if (ps_dt and not pe_dt) or (pe_dt and not ps_dt):
        return "INVALID_TIME", "促销开始/结束必须成对填写", old_price, eff_dt, ps_dt, pe_dt
    if ps_dt and pe_dt:
        if not (eff_dt <= ps_dt < pe_dt):
            return "INVALID_TIME", "促销时段需满足 生效时间≤促销开始<促销结束", old_price, eff_dt, ps_dt, pe_dt
        # 促销重叠：与该商品历史促销版本的时段交叉
        if _promo_overlaps(conn, store_id, product_id, ps_dt, pe_dt):
            return ("PROMO_OVERLAP",
                    f"促销 {ps_s}~{pe_s} 与已有促销时段重叠",
                    old_price, eff_dt, ps_dt, pe_dt)

    # 价签离线
    if tag and tag["status"] != "online":
        return "TAG_OFFLINE", f"价签 {tag['id']} 当前离线，无法下发", old_price, eff_dt, ps_dt, pe_dt

    # 批次互斥
    ab = active_batch_for(conn, store_id, product_id)
    if ab:
        return "BATCH_CONFLICT", f"在途批次 {ab['code']} 正在修改该商品，禁止并行", old_price, eff_dt, ps_dt, pe_dt

    return "ok", f"旧价 {old_price:.2f} → 新价 {new_price:.2f}", old_price, eff_dt, ps_dt, pe_dt


def _promo_overlaps(conn, store_id, product_id, start: datetime, end: datetime) -> bool:
    rows = conn.execute(
        "SELECT promo_start, promo_end FROM price_versions"
        " WHERE store_id=? AND product_id=? AND promo_start IS NOT NULL AND promo_end IS NOT NULL",
        (store_id, product_id),
    ).fetchall()
    for r in rows:
        s = parse_dt(r["promo_start"]); e = parse_dt(r["promo_end"])
        if s and e and start < e and s < end:
            return True
    return False


def import_diff_items(conn, import_id: int):
    return conn.execute(
        "SELECT ii.*, p.name AS product_name, s.name AS store_name, t.id AS tag_id"
        " FROM import_items ii"
        " LEFT JOIN products p ON p.id=ii.product_id"
        " LEFT JOIN stores s ON s.id=ii.store_id"
        " LEFT JOIN tags t ON t.store_id=ii.store_id AND t.product_id=ii.product_id"
        " WHERE ii.import_list_id=? ORDER BY ii.line_no",
        (import_id,),
    ).fetchall()


# ---------------------------------------------------------------- 审批

def pending_approvals(conn):
    return conn.execute(
        "SELECT il.*, u.display_name AS submitter_name FROM import_lists il"
        " JOIN users u ON u.id=il.submitted_by"
        " WHERE il.status='validated' ORDER BY il.id"
    ).fetchall()


def approve_import(import_id: int, approver_id: int,
                   conn: sqlite3.Connection | None = None,
                   ref_time: datetime | None = None) -> dict:
    own = False
    if conn is None:
        conn = db.get_conn(); own = True
    ref = ref_time or datetime.now()
    approver_uid = resolve_user(conn, approver_id)
    il = conn.execute("SELECT * FROM import_lists WHERE id=?", (import_id,)).fetchone()
    if not il:
        r = {"ok": False, "code": "NOT_FOUND", "message": "导入单不存在"}
    elif approver_uid is None:
        r = {"ok": False, "code": "UNKNOWN_USER", "message": "未知审批人"}
    elif il["status"] != "validated":
        r = {"ok": False, "code": "NOT_PENDING",
             "message": f"导入单状态为 {il['status']}，不可审批"}
    elif il["submitted_by"] == approver_uid:
        r = {"ok": False, "code": "APPROVAL_SELF", "message": "审批人不能审批自己提交的清单"}
    else:
        conn.execute(
            "UPDATE import_lists SET status='approved', approved_by=?, approved_at=? WHERE id=?",
            (approver_uid, fmt_dt(ref), import_id),
        )
        batch = _create_batch_from_import(conn, il, approver_uid, ref)
        r = {"ok": True, "code": "ok", "batch": batch}
    if own:
        conn.commit(); conn.close()
    return r


def reject_import(import_id: int, approver_id: int, reason: str,
                  conn: sqlite3.Connection | None = None):
    own = False
    if conn is None:
        conn = db.get_conn(); own = True
    approver_uid = resolve_user(conn, approver_id)
    il = conn.execute("SELECT * FROM import_lists WHERE id=?", (import_id,)).fetchone()
    if il and approver_uid is None:
        r = {"ok": False, "code": "UNKNOWN_USER", "message": "未知审批人"}
    elif il and il["submitted_by"] == approver_uid:
        r = {"ok": False, "code": "APPROVAL_SELF", "message": "不能审批（含驳回）自己提交的清单"}
    elif il and il["status"] == "validated":
        conn.execute(
            "UPDATE import_lists SET status='rejected', approved_by=?, approved_at=?, reject_reason=?"
            " WHERE id=?",
            (approver_uid, fmt_dt(datetime.now()), reason, import_id),
        )
        r = {"ok": True, "code": "ok"}
    else:
        r = {"ok": False, "code": "NOT_PENDING", "message": "导入单不可驳回"}
    if own:
        conn.commit(); conn.close()
    return r


def _create_batch_from_import(conn, il, approver_id: int, ref: datetime) -> dict:
    items = conn.execute(
        "SELECT * FROM import_items WHERE import_list_id=? AND check_status='ok' ORDER BY line_no",
        (il["id"],),
    ).fetchall()
    code = make_code("B", ref)
    cur = conn.execute(
        "INSERT INTO batches(code,import_list_id,batch_type,status,created_by,created_at,note)"
        " VALUES (?,?,'price_change','pending',?,?,'审批通过自动生成')",
        (code, il["id"], approver_id, fmt_dt(ref)),
    )
    bid = cur.lastrowid
    included = 0
    for it in items:
        tag = get_tag(conn, it["store_id"], it["product_id"])
        # 审批生成批次时再做一次权威互斥判定（导入后可能已有别的批次在途）
        ab = active_batch_for(conn, it["store_id"], it["product_id"], exclude_batch_id=bid)
        if ab:
            conn.execute(
                "UPDATE import_items SET check_status='BATCH_CONFLICT',"
                " check_message=? WHERE id=?",
                (f"审批时发现在途批次 {ab['code']}，该明细被排除出批次", it["id"]),
            )
            continue
        conn.execute(
            "INSERT INTO batch_items(batch_id,store_id,product_id,tag_id,old_price,new_price,"
            "promo_start,promo_end,dispatch_status) VALUES (?,?,?,?,?,?,?,?, 'waiting')",
            (bid, it["store_id"], it["product_id"], tag["id"] if tag else None,
             it["old_price"], it["new_price"], it["promo_start"], it["promo_end"]),
        )
        included += 1
    conn.execute("UPDATE import_lists SET status='batched' WHERE id=?", (il["id"],))
    if included == 0:
        # 全部明细被互斥等原因排除：空批次直接取消，不占用互斥锁
        conn.execute("UPDATE batches SET status='cancelled', finished_at=? WHERE id=?",
                     (fmt_dt(ref), bid))
    return {"id": bid, "code": code, "items": included}


# ---------------------------------------------------------------- 模拟价签适配器

class MockTagAdapter:
    """模拟价签下发。默认 70% 成功；可通过离线/强制失败制造案例。

    fail_tags: 本次调用强制失败的价签集合（模拟部分成功）。
    """

    def __init__(self, fail_rate: float = 0.0, fail_tags: set[str] | None = None):
        self.fail_rate = fail_rate
        self.fail_tags = fail_tags or set()

    def push(self, tag_id: str, price: float) -> tuple[bool, str]:
        # 不真正随机：用价签编号做确定性“抖动”，并由 fail_tags 强制
        if tag_id in self.fail_tags:
            return False, "价签未在规定时间内应答（模拟信号弱）"
        # 确定性伪随机：编号末位决定，默认全部成功，便于演示复现
        if self.fail_rate > 0:
            tail = int("".join(ch for ch in tag_id if ch.isdigit())[-2:])
            if (tail % 100) / 100 < self.fail_rate:
                return False, "价签通信超时（模拟）"
        return True, "回执成功"


# ---------------------------------------------------------------- 批次下发

ADAPTER = MockTagAdapter()


def activate_due_batches(conn, ref: datetime | None = None, adapter: MockTagAdapter | None = None) -> list[int]:
    """到生效时间则激活：为每个明细追加期望价格版本并尝试下发。"""
    ref = ref or datetime.now()
    adapter = adapter or ADAPTER
    activated = []
    for b in conn.execute("SELECT * FROM batches WHERE status='pending' ORDER BY id").fetchall():
        items = conn.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY id", (b["id"],)).fetchall()
        # 取该批次最早生效时间（导入明细行上）；此处批次内统一用 created_at 之后即可下发，
        # 生效时间存于 import_items；演示中以批次创建即生效，定时调度用 due 判定。
        due = True
        if b["import_list_id"]:
            eff = conn.execute(
                "SELECT MIN(effective_at) AS m FROM import_items"
                " WHERE import_list_id=? AND check_status='ok'", (b["import_list_id"],),
            ).fetchone()["m"]
            due = eff is not None and parse_dt(eff) <= ref
        if not due:
            continue
        conn.execute("UPDATE batches SET status='active', activated_at=? WHERE id=?",
                     (fmt_dt(ref), b["id"]))
        activated.append(b["id"])
        for it in items:
            _dispatch_item(conn, b, it, ref, adapter)
        _finalize_batch(conn, b["id"], ref)
    return activated


def _append_version(conn, bi, batch_row, ref: datetime) -> int:
    cv = current_version(conn, bi["store_id"], bi["product_id"])
    cur = conn.execute(
        "INSERT INTO price_versions(store_id,product_id,price,change_type,promo_start,promo_end,"
        "effective_at,source_batch_id,supersedes_version_id,created_at,target_version_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (bi["store_id"], bi["product_id"], bi["new_price"],
         "promo" if bi["promo_start"] else "regular",
         bi["promo_start"], bi["promo_end"], fmt_dt(ref),
         batch_row["id"], cv["id"] if cv else None, fmt_dt(ref),
         None),
    )
    vid = cur.lastrowid
    conn.execute(
        "INSERT INTO current_prices(store_id,product_id,current_version_id) VALUES (?,?,?)"
        " ON CONFLICT(store_id,product_id) DO UPDATE SET current_version_id=excluded.current_version_id",
        (bi["store_id"], bi["product_id"], vid),
    )
    return vid


def _dispatch_item(conn, batch_row, bi, ref: datetime, adapter: MockTagAdapter):
    # 期望版本只追加一次（重试不再重复追加）
    vid = bi["version_id"]
    if not vid:
        vid = _append_version(conn, bi, batch_row, ref)
        conn.execute("UPDATE batch_items SET version_id=? WHERE id=?", (vid, bi["id"]))
    tag = conn.execute("SELECT * FROM tags WHERE id=?", (bi["tag_id"],)).fetchone() if bi["tag_id"] else None
    if tag and tag["status"] != "online":
        ok, detail = False, "价签离线"
    else:
        ok, detail = adapter.push(bi["tag_id"], bi["new_price"])
    attempts = (bi["attempts"] or 0) + 1
    if ok:
        conn.execute(
            "UPDATE tags SET displayed_price=?, displayed_version_id=?, updated_at=? WHERE id=?",
            (bi["new_price"], vid, fmt_dt(ref), bi["tag_id"]),
        )
        conn.execute(
            "UPDATE batch_items SET dispatch_status='success', attempts=?, last_error=NULL, acked_at=?"
            " WHERE id=?", (attempts, fmt_dt(ref), bi["id"]),
        )
    else:
        conn.execute(
            "UPDATE batch_items SET dispatch_status='failed', attempts=?, last_error=? WHERE id=?",
            (attempts, detail, bi["id"]),
        )
    conn.execute(
        "INSERT INTO dispatch_logs(batch_item_id,attempt,result,detail,created_at)"
        " VALUES (?,?,?,?,?)",
        (bi["id"], attempts, "success" if ok else "failed", detail, fmt_dt(ref)),
    )


def retry_item(batch_item_id: int, conn: sqlite3.Connection | None = None,
               adapter: MockTagAdapter | None = None, ref: datetime | None = None) -> dict:
    """失败项单独重试。成功则更新价签展示版本；幂等：已成功项不再重复下发。"""
    own = False
    if conn is None:
        conn = db.get_conn(); own = True
    ref = ref or datetime.now()
    adapter = adapter or ADAPTER
    bi = conn.execute("SELECT * FROM batch_items WHERE id=?", (batch_item_id,)).fetchone()
    if not bi:
        r = {"ok": False, "code": "NOT_FOUND", "message": "明细不存在"}
    elif bi["dispatch_status"] == "success":
        r = {"ok": True, "code": "ALREADY_SUCCESS", "message": "已成功，跳过重试（幂等）"}
    else:
        b = conn.execute("SELECT * FROM batches WHERE id=?", (bi["batch_id"],)).fetchone()
        _dispatch_item(conn, b, bi, ref, adapter)
        bi2 = conn.execute("SELECT * FROM batch_items WHERE id=?", (batch_item_id,)).fetchone()
        _finalize_batch(conn, bi["batch_id"], ref)
        r = {"ok": bi2["dispatch_status"] == "success",
             "code": bi2["dispatch_status"],
             "message": bi2["last_error"] or "重试成功",
             "attempts": bi2["attempts"]}
    if own:
        conn.commit(); conn.close()
    return r


def batch_progress(conn, batch_id: int) -> dict:
    row = conn.execute(
        "SELECT"
        " SUM(CASE WHEN dispatch_status='success' THEN 1 ELSE 0 END) AS success,"
        " SUM(CASE WHEN dispatch_status='waiting' THEN 1 ELSE 0 END) AS waiting,"
        " SUM(CASE WHEN dispatch_status='failed' THEN 1 ELSE 0 END) AS failed,"
        " COUNT(*) AS total FROM batch_items WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    return {"success": row["success"] or 0, "waiting": row["waiting"] or 0,
            "failed": row["failed"] or 0, "total": row["total"] or 0}


def _finalize_batch(conn, batch_id: int, ref: datetime):
    p = batch_progress(conn, batch_id)
    if p["waiting"] > 0:
        status = "active"
    elif p["failed"] > 0 and p["success"] > 0:
        status = "partial"
    elif p["failed"] > 0:
        status = "failed"
    else:
        status = "done"
    if status in ("partial", "failed", "done"):
        conn.execute("UPDATE batches SET status=?, finished_at=? WHERE id=?",
                     (status, fmt_dt(ref), batch_id))
    else:
        conn.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))


def list_batches(conn, limit: int = 50):
    return conn.execute("SELECT b.*, u.display_name AS creator_name FROM batches b"
                        " LEFT JOIN users u ON u.id=b.created_by"
                        " ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()


# ---------------------------------------------------------------- 促销到期自动恢复

def expire_promotions(conn, ref: datetime | None = None,
                      adapter: MockTagAdapter | None = None,
                      created_by: int | None = None) -> list[dict]:
    """到期促销：自动发起恢复批次，把期望价恢复为促销前一个常规版本。

    判定：当前期望版本是 promo 且 promo_end <= now，且尚无恢复/回滚版本指向它。
    """
    ref = ref or datetime.now()
    adapter = adapter or ADAPTER
    out = []
    rows = conn.execute(
        "SELECT pv.* FROM current_prices cp JOIN price_versions pv ON pv.id=cp.current_version_id"
        " WHERE pv.change_type='promo' AND pv.promo_end IS NOT NULL",
    ).fetchall()
    for pv in rows:
        if parse_dt(pv["promo_end"]) > ref:
            continue
        # 已恢复过则跳过（幂等）
        dup = conn.execute(
            "SELECT 1 FROM price_versions WHERE store_id=? AND product_id=? AND change_type='restore'"
            " AND supersedes_version_id=? LIMIT 1",
            (pv["store_id"], pv["product_id"], pv["id"]),
        ).fetchone()
        if dup:
            continue
        # 促销前一版本（优先常规价）
        target = conn.execute(
            "SELECT * FROM price_versions WHERE store_id=? AND product_id=? AND id<?"
            " AND change_type IN ('regular','rollback') ORDER BY id DESC LIMIT 1",
            (pv["store_id"], pv["product_id"], pv["id"]),
        ).fetchone()
        if not target:
            target = conn.execute(
                "SELECT * FROM price_versions WHERE store_id=? AND product_id=? AND id<?"
                " ORDER BY id DESC LIMIT 1",
                (pv["store_id"], pv["product_id"], pv["id"]),
            ).fetchone()
        tag = get_tag(conn, pv["store_id"], pv["product_id"])
        code = make_code("B", ref)
        cur = conn.execute(
            "INSERT INTO batches(code,batch_type,store_id,product_id,target_version_id,status,"
            "created_by,created_at,activated_at,note) VALUES (?, 'promo_restore', ?,?,?, 'active',?,?,?,?)",
            (code, pv["store_id"], pv["product_id"], target["id"], created_by,
             fmt_dt(ref), fmt_dt(ref), f"促销到期自动恢复 → 版本#{target['id']} ({target['price']:.2f})"),
        )
        bid = cur.lastrowid
        bi_cur = conn.execute(
            "INSERT INTO batch_items(batch_id,store_id,product_id,tag_id,old_price,new_price,dispatch_status)"
            " VALUES (?,?,?,?,?,?,'waiting')",
            (bid, pv["store_id"], pv["product_id"], tag["id"] if tag else None,
             pv["price"], target["price"]),
        )
        bi = conn.execute("SELECT * FROM batch_items WHERE id=?", (bi_cur.lastrowid,)).fetchone()
        b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        _dispatch_item(conn, b, bi, ref, adapter)
        _finalize_batch(conn, bid, ref)
        out.append({"batch_id": bid, "code": code, "store": pv["store_id"],
                    "product": pv["product_id"], "restore_to": target["price"]})
    return out


# ---------------------------------------------------------------- 现场抽检

def inspect_tag(tag_or_code: str, actual_price: float | None, inspector: str,
                photo_path: str | None, note: str,
                conn: sqlite3.Connection | None = None, ref: datetime | None = None) -> dict:
    """按价签编号扫码抽检。actual_price 与系统期望价逐位比对。"""
    own = False
    if conn is None:
        conn = db.get_conn(); own = True
    ref = ref or datetime.now()
    tag = conn.execute(
        "SELECT t.*, p.name AS product_name FROM tags t JOIN products p ON p.id=t.product_id"
        " WHERE t.id=?", (tag_or_code.strip(),),
    ).fetchone()
    if not tag:
        r = {"ok": False, "code": "TAG_NOT_FOUND", "message": f"未找到价签 {tag_or_code}"}
    else:
        cv = current_version(conn, tag["store_id"], tag["product_id"])
        expected = cv["price"]
        if tag["status"] != "online":
            result, msg = "offline", "价签离线，无法核对显示价"
        elif actual_price is None:
            result, msg = "offline", "未录入现场价格"
        elif abs(float(actual_price) - float(expected)) < 0.005:
            result, msg = "consistent", "现场价与系统期望价一致"
        else:
            result, msg = "inconsistent", (
                f"现场价 {float(actual_price):.2f} 与期望价 {float(expected):.2f} 不一致；"
                f"价签实际显示 {tag['displayed_price']:.2f}")
        code = make_code("INS", ref)
        conn.execute(
            "INSERT INTO inspections(code,tag_id,store_id,product_id,expected_price,actual_price,"
            "result,photo_path,inspector,checked_at,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, tag["id"], tag["store_id"], tag["product_id"], expected, actual_price,
             result, photo_path, inspector, fmt_dt(ref), note or msg),
        )
        r = {"ok": True, "code": code, "result": result, "message": msg,
             "tag": dict(tag), "expected": expected, "actual": actual_price,
             "displayed": tag["displayed_price"]}
    if own:
        conn.commit(); conn.close()
    return r


# ---------------------------------------------------------------- 版本回滚（幂等）

def rollback(store_id: str, product_id: str, target_version_id: int,
             executed_by: str = "manager", conn: sqlite3.Connection | None = None,
             adapter: MockTagAdapter | None = None, ref: datetime | None = None,
             note: str = "") -> dict:
    """紧急回滚：必须指向明确历史版本。重复执行无副作用（幂等）。

    - 目标版本必须存在且属于该门店商品
    - 若当前期望版本已经是该价格的回滚版本 → idempotent_skip
    - 否则生成 emergency_rollback 批次，追加 rollback 版本并下发
    """
    own = False
    if conn is None:
        conn = db.get_conn(); own = True
    ref = ref or datetime.now()
    adapter = adapter or ADAPTER

    target = conn.execute(
        "SELECT * FROM price_versions WHERE id=? AND store_id=? AND product_id=?",
        (target_version_id, store_id, product_id),
    ).fetchone()
    if not target:
        r = {"ok": False, "code": "ROLLBACK_NO_TARGET",
             "message": "回滚必须指向该商品一个明确存在的历史版本"}
    else:
        cv = current_version(conn, store_id, product_id)
        tag = get_tag(conn, store_id, product_id)
        # 幂等 1：当前期望版本已由回滚指向同一目标版本
        already = cv and cv["change_type"] == "rollback" and cv["target_version_id"] == target["id"]
        # 幂等 2：当前价已等于目标价（现场也已显示）
        same_price = cv and abs(cv["price"] - target["price"]) < 0.005
        if already or (same_price and tag and tag["displayed_version_id"] == cv["id"]
                       and abs((tag["displayed_price"] or 0) - target["price"]) < 0.005):
            code = make_code("RB", ref)
            conn.execute(
                "INSERT INTO rollbacks(code,store_id,product_id,from_version_id,target_version_id,"
                "status,executed_by,executed_at,note) VALUES (?,?,?,?,?, 'idempotent_skip',?,?,?)",
                (code, store_id, product_id, cv["id"] if cv else None, target["id"],
                 executed_by, fmt_dt(ref), "重复回滚，系统已处于目标版本，无副作用"))
            r = {"ok": True, "code": "IDEMPOTENT_SKIP", "rollback_code": code,
                 "message": f"当前已是目标版本 #{target['id']}（{target['price']:.2f}），重复执行无副作用"}
        else:
            bcode = make_code("B", ref)
            bcur = conn.execute(
                "INSERT INTO batches(code,batch_type,store_id,product_id,target_version_id,status,"
                "created_at,activated_at,note) VALUES (?, 'emergency_rollback', ?,?,?, 'active',?,?,?)",
                (bcode, store_id, product_id, target["id"], fmt_dt(ref), fmt_dt(ref),
                 f"紧急回滚 → 版本 #{target['id']}（{target['price']:.2f}） {note}"),
            )
            bid = bcur.lastrowid
            icur = conn.execute(
                "INSERT INTO batch_items(batch_id,store_id,product_id,tag_id,old_price,new_price,dispatch_status)"
                " VALUES (?,?,?,?,?,?,'waiting')",
                (bid, store_id, product_id, tag["id"] if tag else None,
                 cv["price"] if cv else None, target["price"]),
            )
            bi = conn.execute("SELECT * FROM batch_items WHERE id=?", (icur.lastrowid,)).fetchone()
            # 追加 rollback 版本，明确指向目标
            vcur = conn.execute(
                "INSERT INTO price_versions(store_id,product_id,price,change_type,effective_at,"
                "source_batch_id,supersedes_version_id,target_version_id,created_at)"
                " VALUES (?,?,?, 'rollback',?,?,?,?,?)",
                (store_id, product_id, target["price"], fmt_dt(ref), bid,
                 cv["id"] if cv else None, target["id"], fmt_dt(ref)),
            )
            vid = vcur.lastrowid
            conn.execute(
                "INSERT INTO current_prices(store_id,product_id,current_version_id) VALUES (?,?,?)"
                " ON CONFLICT(store_id,product_id) DO UPDATE SET current_version_id=excluded.current_version_id",
                (store_id, product_id, vid),
            )
            conn.execute("UPDATE batch_items SET version_id=? WHERE id=?", (vid, bi["id"]))
            # 下发
            if tag and tag["status"] != "online":
                ok, detail = False, "价签离线"
            else:
                ok, detail = adapter.push(tag["id"], target["price"])
            if ok:
                conn.execute(
                    "UPDATE tags SET displayed_price=?, displayed_version_id=?, updated_at=? WHERE id=?",
                    (target["price"], vid, fmt_dt(ref), tag["id"]),
                )
                conn.execute(
                    "UPDATE batch_items SET dispatch_status='success', attempts=1, acked_at=? WHERE id=?",
                    (fmt_dt(ref), bi["id"]),
                )
            else:
                conn.execute(
                    "UPDATE batch_items SET dispatch_status='failed', attempts=1, last_error=? WHERE id=?",
                    (detail, bi["id"]),
                )
            conn.execute(
                "INSERT INTO dispatch_logs(batch_item_id,attempt,result,detail,created_at)"
                " VALUES (?,?,?,?,?)",
                (bi["id"], 1, "success" if ok else "failed", detail, fmt_dt(ref)),
            )
            _finalize_batch(conn, bid, ref)
            rcode = make_code("RB", ref)
            conn.execute(
                "INSERT INTO rollbacks(code,batch_id,store_id,product_id,from_version_id,"
                "target_version_id,status,executed_by,executed_at,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rcode, bid, store_id, product_id, cv["id"] if cv else None, target["id"],
                 "success" if ok else "failed", executed_by, fmt_dt(ref),
                 f"回滚批次 {bcode}：{detail}"),
            )
            r = {"ok": ok, "code": "success" if ok else "failed", "batch_code": bcode,
                 "rollback_code": rcode, "version_id": vid,
                 "message": "回滚下发成功" if ok else f"回滚已记录但价签下发失败：{detail}"}
    if own:
        conn.commit(); conn.close()
    return r
