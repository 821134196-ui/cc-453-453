"""审批、批次下发、失败重试、版本回滚、现场抽检。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import results as R
from .adapter import EslAdapter
from .db import iso, now, parse_dt


class ServiceError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or R.label(code))
        self.code = code
        self.message = message or R.label(code)


# ----------------------------------------------------------------- 审批

def approve(conn: sqlite3.Connection, import_id: int, username: str) -> int:
    """审批通过 -> 生成批次。返回 batch_id。"""
    imp = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    if imp is None:
        raise ServiceError(R.IMPORT_NOT_APPROVABLE, "清单不存在")
    approver = conn.execute("SELECT * FROM users WHERE username=?",
                            (username,)).fetchone()
    if approver is None:
        raise ServiceError(R.SELF_APPROVAL, "审批人不存在")
    if approver["role"] != "manager":
        raise ServiceError(R.SELF_APPROVAL, "仅店长（manager）可以审批调价清单")
    if imp["submitted_by"] == approver["id"]:
        raise ServiceError(R.SELF_APPROVAL, "审批人不能审批自己提交的清单")
    if imp["status"] != "validated":
        raise ServiceError(R.IMPORT_NOT_APPROVABLE,
                           f"清单状态为 {imp['status']}，不可审批")

    rows = conn.execute(
        "SELECT * FROM import_rows WHERE import_id=? ORDER BY line_no",
        (import_id,)).fetchall()
    err = [r for r in rows if r["status"] == "error"]
    if err:
        raise ServiceError(err[0]["result_code"],
                           f"仍有 {len(err)} 行校验错误，首项：{err[0]['message']}")

    items = [r for r in rows if r["status"] == "ok"]
    if not items:
        raise ServiceError(R.IMPORT_NOT_APPROVABLE, "没有可执行的改价条目（全部为空变化）")

    # 生成前再次确认互斥锁（校验后可能有新批次进入）
    for r in items:
        if _lock_held(conn, r["tag_id"]):
            raise ServiceError(R.MUTEX_CONFLICT,
                               f"价签 {r['tag_no']} 正被其他批次占用")

    scheduled_at = parse_dt(imp["effective_at"])
    promo_end = parse_dt(imp["promo_end_at"])
    batch_no = _next_batch_no(conn)
    cur = conn.execute(
        """INSERT INTO batches(no,import_id,kind,status,created_at,scheduled_at,
             created_by,promo_end_at,note)
           VALUES(?,?,'normal','pending',?,?,?,?, '')""",
        (batch_no, import_id, iso(now()), iso(scheduled_at), approver["id"],
         iso(promo_end) if promo_end else None))
    batch_id = cur.lastrowid
    conn.execute(
        "UPDATE imports SET status='approved', approved_at=?, approved_by=? WHERE id=?",
        (iso(now()), approver["id"], import_id))

    item_type = "promo" if promo_end else "normal"
    for r in items:
        tag = conn.execute("SELECT * FROM tags WHERE id=?",
                           (r["tag_id"],)).fetchone()
        ver = _next_version(conn, r["tag_id"])
        bi = conn.execute(
            """INSERT INTO batch_items(batch_id,tag_id,store_id,sku,target_version,
                 target_price,old_version,old_price,item_type,status)
               VALUES(?,?,?,?,?,?,?,?,?, 'waiting')""",
            (batch_id, r["tag_id"], tag["store_id"], r["sku"], ver,
             r["new_price"], ver - 1, r["old_price"], item_type),
        )
        conn.execute(
            "INSERT INTO tag_locks(store_id,sku,batch_item_id) VALUES(?,?,?)",
            (tag["store_id"], r["sku"], bi.lastrowid))
    conn.commit()
    return batch_id


def reject(conn: sqlite3.Connection, import_id: int, username: str,
           reason: str) -> None:
    imp = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    if imp is None or imp["status"] != "validated":
        raise ServiceError(R.IMPORT_NOT_APPROVABLE, "清单不存在或不可驳回")
    approver = conn.execute("SELECT * FROM users WHERE username=?",
                            (username,)).fetchone()
    if approver and imp["submitted_by"] == approver["id"]:
        raise ServiceError(R.SELF_APPROVAL, "不能审批/驳回自己提交的清单")
    conn.execute(
        "UPDATE imports SET status='rejected', checked_at=?, checked_by=?, reject_reason=? WHERE id=?",
        (iso(now()), approver["id"] if approver else None, reason, import_id))
    conn.commit()


# ----------------------------------------------------------------- 下发

def dispatch_due(conn: sqlite3.Connection, adapter: EslAdapter | None = None) -> list[int]:
    """下发所有到期的待执行批次。返回本次实际处理的 batch id。"""
    adapter = adapter or EslAdapter(conn)
    touched = []
    rows = conn.execute(
        """SELECT id FROM batches
            WHERE status IN ('pending','partial')
              AND scheduled_at IS NOT NULL AND scheduled_at <= ?
            ORDER BY id""", (iso(now()),)).fetchall()
    for r in rows:
        if _dispatch_batch(conn, r["id"], adapter):
            touched.append(r["id"])
    return touched


def _dispatch_batch(conn: sqlite3.Connection, batch_id: int,
                    adapter: EslAdapter) -> bool:
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if b["started_at"] is None:
        conn.execute("UPDATE batches SET started_at=? WHERE id=?",
                     (iso(now()), batch_id))
    did = False
    waiting = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id=? AND status='waiting' ORDER BY id",
        (batch_id,)).fetchall()
    for item in waiting:
        _push_item(conn, item, adapter)
        did = True
    _refresh_batch_status(conn, batch_id)
    conn.commit()
    return did


def retry_item(conn: sqlite3.Connection, item_id: int,
               adapter: EslAdapter | None = None) -> dict:
    """单独重试一个失败项。"""
    adapter = adapter or EslAdapter(conn)
    item = conn.execute("SELECT * FROM batch_items WHERE id=?",
                        (item_id,)).fetchone()
    if item is None:
        raise ServiceError(R.ACK_SIMULATED_FAIL, "批次条目不存在")
    b = conn.execute("SELECT * FROM batches WHERE id=?",
                     (item["batch_id"],)).fetchone()
    if item["status"] != "failed":
        raise ServiceError(R.ALREADY_AT_VERSION,
                           f"条目当前状态 {item['status']}，无需重试")
    if b["status"] == "rolled_back":
        raise ServiceError(R.RB_NO_NEED, "所属批次已回滚，不能再重试")
    _push_item(conn, item, adapter)
    _refresh_batch_status(conn, item["batch_id"])
    conn.commit()
    return {"item_id": item_id, "result": _item_status(item_id, conn)}


def retry_batch_failed(conn: sqlite3.Connection, batch_id: int,
                       adapter: EslAdapter | None = None) -> int:
    adapter = adapter or EslAdapter(conn)
    n = 0
    for item in conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=? AND status='failed'",
            (batch_id,)).fetchall():
        b = conn.execute("SELECT status FROM batches WHERE id=?",
                         (batch_id,)).fetchone()
        if b["status"] == "rolled_back":
            break
        _push_item(conn, item, adapter)
        n += 1
    _refresh_batch_status(conn, batch_id)
    conn.commit()
    return n


def _push_item(conn: sqlite3.Connection, item: sqlite3.Row,
               adapter: EslAdapter) -> None:
    """到点激活并推送。

    激活：系统期望价立即变为目标价、追加价格版本（权威侧先行）；
    显示价只有在成功回执后才追上目标版本。离线/失败即形成两价偏离。
    """
    activated = False
    if item["status"] == "waiting":
        conn.execute(
            """INSERT INTO price_versions(tag_id,version,price,effective_at,
                 change_type,batch_id,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (item["tag_id"], item["target_version"], item["target_price"],
             iso(now()),
             {"promo": "promo", "restore": "restore"}.get(
                 item["item_type"], "normal"),
             item["batch_id"], iso(now())))
        conn.execute(
            "UPDATE tags SET expected_price=? WHERE id=?",
            (item["target_price"], item["tag_id"]))
        activated = True

    res = adapter.push(item["tag_id"], item["target_version"], item["target_price"])
    attempts = item["attempts"] + 1
    if res["ok"]:
        conn.execute(
            """UPDATE tags SET display_price=?, display_version=? WHERE id=?""",
            (item["target_price"], item["target_version"], item["tag_id"]))
        conn.execute(
            """UPDATE batch_items SET status='success', attempts=?, last_error='',
                 last_result_code=?, ack_at=? WHERE id=?""",
            (attempts, R.ACK_SUCCESS, iso(now()), item["id"]))
        # 促销价成功后锁保留到恢复批次；其他成功即释放
        if item["item_type"] != "promo":
            conn.execute("DELETE FROM tag_locks WHERE batch_item_id=?",
                         (item["id"],))
    else:
        conn.execute(
            """UPDATE batch_items SET status='failed', attempts=?, last_error=?,
                 last_result_code=? WHERE id=?""",
            (attempts, res["message"], res["code"], item["id"]))


def _refresh_batch_status(conn: sqlite3.Connection, batch_id: int) -> None:
    stats = batch_stats(conn, batch_id)
    if stats["failed"] and stats["success"]:
        status = "partial"
    elif stats["failed"]:
        status = "failed"
    elif stats["waiting"]:
        status = "pending"
    else:
        status = "succeeded"
    finished = iso(now()) if status in ("succeeded", "failed", "partial") else None
    conn.execute(
        "UPDATE batches SET status=?, finished_at=COALESCE(finished_at, ?) WHERE id=?",
        (status, finished, batch_id))


def batch_stats(conn: sqlite3.Connection, batch_id: int) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM batch_items WHERE batch_id=? GROUP BY status",
        (batch_id,)).fetchall()
    stats = {"waiting": 0, "success": 0, "failed": 0, "canceled": 0,
             "total": 0}
    for r in rows:
        stats[r["status"]] = r["c"]
        stats["total"] += r["c"]
    return stats


def _item_status(item_id: int, conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT * FROM batch_items WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else {}


# ----------------------------------------------------------------- 促销恢复

def create_due_restores(conn: sqlite3.Connection) -> list[int]:
    """促销到期：为已生效成功的促销条目自动发起恢复批次。"""
    created = []
    rows = conn.execute(
        """SELECT b.* FROM batches b
            WHERE b.kind='normal' AND b.promo_end_at IS NOT NULL
              AND b.promo_end_at <= ?
              AND b.status IN ('succeeded','partial')
              AND NOT EXISTS (
                  SELECT 1 FROM batches r
                   WHERE r.kind='promo_restore' AND r.note LIKE 'RB-SRC-' || b.id || '-%')
              AND NOT EXISTS (
                  SELECT 1 FROM rollbacks rb WHERE rb.batch_id=b.id)
            ORDER BY b.id""", (iso(now()),)).fetchall()
    for b in rows:
        promo_items = conn.execute(
            """SELECT * FROM batch_items WHERE batch_id=?
               AND item_type='promo' AND status IN ('success','failed')""",
            (b["id"],)).fetchall()
        if not promo_items:
            continue
        rid = _make_restore_batch(conn, b, promo_items)
        if rid:
            created.append(rid)
    return created


def _make_restore_batch(conn: sqlite3.Connection, src_batch: sqlite3.Row,
                        promo_items: list[sqlite3.Row]) -> int | None:
    no = _next_batch_no(conn)
    cur = conn.execute(
        """INSERT INTO batches(no,import_id,kind,status,created_at,scheduled_at,
             created_by,note)
           VALUES(?,NULL,'promo_restore','pending',?,?,NULL,?)""",
        (no, iso(now()), iso(now()), f"RB-SRC-{src_batch['id']}-AUTO"))
    rid = cur.lastrowid
    for it in promo_items:
        base = conn.execute(
            "SELECT price FROM price_versions WHERE tag_id=? AND version=?",
            (it["tag_id"], it["old_version"])).fetchone()
        target_price = base["price"] if base else it["old_price"]
        new_ver = _next_version(conn, it["tag_id"])
        bi = conn.execute(
            """INSERT INTO batch_items(batch_id,tag_id,store_id,sku,target_version,
                 target_price,old_version,old_price,item_type,status)
               VALUES(?,?,?,?,?,?,?,?,'restore','waiting')""",
            (rid, it["tag_id"], it["store_id"], it["sku"], new_ver, target_price,
             it["target_version"], it["target_price"]))
        # 把促销锁转交给恢复条目
        conn.execute("DELETE FROM tag_locks WHERE batch_item_id=?", (it["id"],))
        conn.execute(
            "INSERT INTO tag_locks(store_id,sku,batch_item_id) VALUES(?,?,?)",
            (it["store_id"], it["sku"], bi.lastrowid))
    conn.commit()
    return rid


# ----------------------------------------------------------------- 回滚

def rollback_batch(conn: sqlite3.Connection, batch_id: int, username: str,
                   reason: str, adapter: EslAdapter | None = None) -> int:
    """紧急回滚：指向每条价签改价前的明确版本。重复执行幂等。"""
    adapter = adapter or EslAdapter(conn)
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if b is None:
        raise ServiceError(R.RB_TAG_NOT_FOUND, "批次不存在")
    user = conn.execute("SELECT * FROM users WHERE username=?",
                        (username,)).fetchone()
    if user is None:
        raise ServiceError(R.RB_TAG_NOT_FOUND, "操作人不存在")

    items = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id=? ORDER BY id",
        (batch_id,)).fetchall()
    rb = conn.execute(
        """INSERT INTO rollbacks(batch_id,reason,operator_id,created_at,status)
           VALUES(?,?,?,?,'pending')""",
        (batch_id, reason, user["id"], iso(now())))
    rb_id = rb.lastrowid

    n_success = n_noop = n_failed = 0
    for it in items:
        tag = conn.execute("SELECT * FROM tags WHERE id=?",
                           (it["tag_id"],)).fetchone()
        target_ver = it["old_version"]
        base = conn.execute(
            "SELECT price FROM price_versions WHERE tag_id=? AND version=?",
            (it["tag_id"], target_ver)).fetchone()
        if base is None:
            _rb_item(conn, rb_id, it, target_ver, it["old_price"],
                     "failed", R.RB_VERSION_MISSING,
                     f"历史版本 v{target_ver} 不存在")
            n_failed += 1
            continue
        target_price = base["price"]
        # 幂等判断：系统期望价与价签显示价都已等于目标价 -> 空操作
        if abs(tag["expected_price"] - target_price) < 1e-9 and \
           abs(tag["display_price"] - target_price) < 1e-9:
            _rb_item(conn, rb_id, it, target_ver, target_price,
                     "no_op", R.RB_NO_NEED,
                     f"当前已显示 v{target_ver} ￥{target_price:.2f}，无需回滚")
            _release_tag_locks(conn, it)
            n_noop += 1
            continue
        res = adapter.push(it["tag_id"], 0, target_price)
        if res["ok"]:
            new_ver = _next_version(conn, it["tag_id"])
            conn.execute(
                """INSERT INTO price_versions(tag_id,version,price,effective_at,
                     change_type,batch_id,created_at)
                   VALUES(?,?,?,?,'rollback',?,?)""",
                (it["tag_id"], new_ver, target_price, iso(now()),
                 batch_id, iso(now())))
            conn.execute(
                """UPDATE tags SET expected_price=?, display_price=?, display_version=?
                    WHERE id=?""",
                (target_price, target_price, new_ver, it["tag_id"]))
            _rb_item(conn, rb_id, it, target_ver, target_price,
                     "success", R.ACK_SUCCESS,
                     f"已回滚至 v{target_ver} ￥{target_price:.2f}（新版本 v{new_ver}）")
            _release_tag_locks(conn, it)
            n_success += 1
        else:
            _rb_item(conn, rb_id, it, target_ver, target_price,
                     "failed", res["code"], res["message"])
            n_failed += 1

    # 尚未下发的等待条目随紧急回滚一并撤销（锁全部释放）
    conn.execute(
        """UPDATE batch_items SET status='canceled',
             last_result_code='RB_NO_NEED',
             last_error='紧急回滚：未生效条目被撤销'
           WHERE batch_id=? AND status='waiting'""", (batch_id,))
    for it in conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=?",
            (batch_id,)).fetchall():
        _release_tag_locks(conn, it)

    if n_failed:
        status = "partial"
    elif n_success:
        status = "succeeded"
    else:
        status = "no_op"
    conn.execute("UPDATE rollbacks SET status=? WHERE id=?", (status, rb_id))
    if n_failed == 0:
        conn.execute("UPDATE batches SET status='rolled_back', finished_at=? WHERE id=?",
                     (iso(now()), batch_id))
    conn.commit()
    return rb_id


def _rb_item(conn, rb_id, it, target_ver, target_price, status, code, message):
    conn.execute(
        """INSERT INTO rollback_items(rollback_id,tag_id,from_version,
             target_version,target_price,status,result_code,message)
           VALUES(?,?,?,?,?,?,?,?)""",
        (rb_id, it["tag_id"], it["target_version"], target_ver, target_price,
         status, code, message))


def _release_tag_locks(conn, it) -> None:
    conn.execute("DELETE FROM tag_locks WHERE batch_item_id=?", (it["id"],))
    # 同 (store, sku) 的其它残留锁（恢复/撤销场景）一并释放
    conn.execute(
        """DELETE FROM tag_locks
            WHERE store_id=? AND sku=?
              AND batch_item_id IN (SELECT id FROM batch_items WHERE batch_id=?)""",
        (it["store_id"], it["sku"], it["batch_id"]))


# ----------------------------------------------------------------- 抽检

PHOTO_PLACEHOLDER = "（未上传照片）"


def inspect(conn: sqlite3.Connection, tag_no: str, observed_s: str,
            inspector: str = "现场店员", photo_path: str | None = None,
            note: str = "") -> dict:
    tag_no = (tag_no or "").strip()
    tag = conn.execute("SELECT * FROM tags WHERE tag_no=?", (tag_no,)).fetchone()
    ts = iso(now())
    if tag is None:
        conn.execute(
            """INSERT INTO inspections(tag_no,observed_price,result,inspector,
                 photo_path,note,inspected_at)
               VALUES(?,?, 'tag_not_found', ?,?,?,?)""",
            (tag_no, _to_float(observed_s), inspector, photo_path, note, ts))
        conn.commit()
        return {"result": "tag_not_found", "code": R.INSP_TAG_NOT_FOUND}
    try:
        observed = float(observed_s)
        if observed <= 0:
            raise ValueError
    except (TypeError, ValueError):
        conn.execute(
            """INSERT INTO inspections(tag_no,tag_id,observed_price,result,
                 expected_price,display_price,inspector,photo_path,note,inspected_at)
               VALUES(?,?,NULL,'invalid',?,?,?,?,?,?)""",
            (tag_no, tag["id"], tag["expected_price"], tag["display_price"],
             inspector, photo_path, "录入价格非法：" + str(observed_s), ts))
        conn.commit()
        return {"result": "invalid", "code": R.INSP_PRICE_INVALID}

    mismatch = abs(observed - tag["display_price"]) > 1e-9
    result = "mismatch" if mismatch else "match"
    msg = ""
    if mismatch:
        msg = f"现场 ￥{observed:.2f} ≠ 价签显示 ￥{tag['display_price']:.2f}"
        if abs(tag["display_price"] - tag["expected_price"]) > 1e-9:
            msg += f"；系统期望 ￥{tag['expected_price']:.2f}（价签也未跟上期望价）"
    conn.execute(
        """INSERT INTO inspections(tag_no,tag_id,observed_price,result,
             expected_price,display_price,inspector,photo_path,note,inspected_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (tag_no, tag["id"], observed, result, tag["expected_price"],
         tag["display_price"], inspector,
         photo_path if photo_path else PHOTO_PLACEHOLDER,
         (note + (" / " if note and msg else "") + msg), ts))
    conn.commit()
    return {
        "result": result,
        "code": R.INSP_MISMATCH if mismatch else R.INSP_MATCH,
        "tag": dict(tag),
        "observed": observed,
    }


# ----------------------------------------------------------------- 工具

def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _next_batch_no(conn: sqlite3.Connection) -> str:
    prefix = "B" + now().strftime("%Y%m%d")
    n = conn.execute(
        "SELECT COUNT(*) c FROM batches WHERE no LIKE ?", (prefix + "-%",)
    ).fetchone()["c"] + 1
    # 同秒多次调用时靠自增计数兜底
    while conn.execute("SELECT 1 FROM batches WHERE no=?",
                       (f"{prefix}-{n:03d}",)).fetchone():
        n += 1
    return f"{prefix}-{n:03d}"


def _next_version(conn: sqlite3.Connection, tag_id: int) -> int:
    r = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 v FROM price_versions WHERE tag_id=?",
        (tag_id,)).fetchone()
    return r["v"]


def _lock_held(conn: sqlite3.Connection, tag_id: int) -> bool:
    r = conn.execute(
        """SELECT 1 FROM tag_locks l JOIN tags t ON t.id=?
            WHERE l.store_id=t.store_id AND l.sku=t.sku LIMIT 1""",
        (tag_id,)).fetchone()
    return r is not None
