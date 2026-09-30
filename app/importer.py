"""调价清单导入与差异校验。

清单 CSV 列：tag_no, new_price[, promo_end_at]
门店/商品以价签编号为准（编号全局唯一，直接定位到门店+商品）。
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import results as R
from .db import IMPORT_DIR, iso, now, parse_dt


REQUIRED_HEADER = ["tag_no", "new_price"]


class ImportError_(Exception):
    pass


def save_upload(filename: str, content: bytes | str) -> Path:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = now().strftime("%Y%m%d_%H%M%S")
    safe = Path(filename).name.replace("/", "_")
    path = IMPORT_DIR / f"{ts}_{safe}"
    if isinstance(content, str):
        content = content.encode("utf-8-sig")
    path.write_bytes(content)
    return path


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [h for h in REQUIRED_HEADER if h not in header]
    if missing:
        raise ImportError_(f"缺少必需列: {', '.join(missing)}")
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
            for row in reader]
    if not rows:
        raise ImportError_("清单没有任何数据行")
    return header, rows


def create_import(conn: sqlite3.Connection, path: Path, username: str,
                  effective_at: datetime, promo_end_at: datetime | None) -> int:
    user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if user is None:
        raise ImportError_(f"提交人不存在: {username}")
    if promo_end_at and promo_end_at <= effective_at:
        raise ImportError_("促销结束时间必须晚于生效时间")
    if effective_at < now() - timedelta(minutes=1):
        raise ImportError_("生效时间不能早于当前时间")
    cur = conn.execute(
        """INSERT INTO imports(file_name, stored_path, submitted_by, submitted_at,
             effective_at, promo_end_at, status)
           VALUES(?,?,?,?,?,?,'validating')""",
        (path.name, str(path), user["id"], iso(now()),
         iso(effective_at), iso(promo_end_at) if promo_end_at else None),
    )
    conn.commit()
    return cur.lastrowid


def validate(conn: sqlite3.Connection, import_id: int) -> dict:
    """对清单逐行差异校验，写 import_rows。返回汇总字典。"""
    imp = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    _, raw = read_rows(Path(imp["stored_path"]))
    effective_at = parse_dt(imp["effective_at"])
    promo_end_at = parse_dt(imp["promo_end_at"])

    # 行内重复商品（同一价签编号 = 同一门店同一商品）
    seen: dict[str, int] = {}
    dup_lines: dict[str, list[int]] = {}
    for i, row in enumerate(raw, start=2):
        no = row.get("tag_no", "")
        if no in seen:
            dup_lines.setdefault(no, [seen[no]]).append(i)
        seen.setdefault(no, i)

    # 促销时段重叠：同一标签，任何已批准/执行中的促销版本覆盖了本次 [生效, 结束)
    overlap_nos = set()
    if promo_end_at:
        overlap_nos = _promo_overlap_nos(conn, effective_at, promo_end_at)

    # 互斥冲突：waiting（含已批准未生效）的批次条目占用的 (store, sku)
    busy = {(r["store_id"], r["sku"]) for r in conn.execute(
        """SELECT DISTINCT bi.store_id, bi.sku
             FROM batch_items bi JOIN tag_locks l
               ON l.batch_item_id = bi.id""")}

    ok = errors = warnings = 0
    for idx, row in enumerate(raw, start=1):
        line_no = idx + 1
        no = row.get("tag_no", "")
        price_s = row.get("new_price", "")
        code, msg, status = R.OK, "", "ok"
        tag_id = None
        old_price = None

        if not no:
            code, msg, status = R.TAG_NOT_FOUND, "价签编号为空", "error"
        if status == "ok":
            try:
                new_price = float(price_s)
                if new_price <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                code, msg, status = R.PRICE_INVALID, f"价格无法解析: {price_s!r}", "error"
                new_price = None

        tag = None
        if status == "ok":
            tag = conn.execute("SELECT * FROM tags WHERE tag_no=?", (no,)).fetchone()
            if tag is None:
                code, msg, status = R.TAG_NOT_FOUND, f"价签编号 {no} 不存在", "error"

        if status == "ok":
            tag_id = tag["id"]
            old_price = tag["expected_price"]
            prod = conn.execute("SELECT * FROM products WHERE sku=?",
                                (tag["sku"],)).fetchone()
            new_price = float(price_s)
            if new_price < prod["min_price"] - 1e-9 or new_price > prod["max_price"] + 1e-9:
                code = R.PRICE_LIMIT
                msg = (f"新价 ￥{new_price:.2f} 超出限价区间 "
                       f"￥{prod['min_price']:.2f} ~ ￥{prod['max_price']:.2f}")
                status = "error"
            elif no in dup_lines:
                first = dup_lines[no][0]
                code = R.DUPLICATE_PRODUCT
                msg = (f"价签 {no} 在本清单中重复出现"
                       + (f"（首次在第 {first} 行）" if line_no != first else
                          f"（第 {first} 行与第 {dup_lines[no][1]} 行重复）"))
                status = "error"
            elif no in overlap_nos:
                code = R.PROMO_OVERLAP
                msg = (f"促销时段 {iso(effective_at)} ~ {iso(promo_end_at)} "
                       "与该价签已批准的促销时段重叠")
                status = "error"
            elif (tag["store_id"], tag["sku"]) in busy:
                code = R.MUTEX_CONFLICT
                msg = "另一批次正在改该门店此商品，未完成前不能再改"
                status = "error"
            elif abs(new_price - old_price) < 1e-9:
                code = R.NO_CHANGE
                msg = f"新价与当前期望价相同 ￥{old_price:.2f}"
                status = "warning"

        conn.execute(
            """INSERT INTO import_rows(import_id,line_no,tag_no,store_code,sku,
                 new_price,promo_end_at,status,result_code,message,old_price,tag_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (import_id, line_no, no,
             (conn.execute("SELECT code FROM stores WHERE id=?",
                           (tag["store_id"],)).fetchone()["code"] if tag else None),
             tag["sku"] if tag else row.get("sku", ""),
             float(price_s) if _is_num(price_s) else None,
             imp["promo_end_at"], status, code, msg, old_price, tag_id),
        )
        if status == "error":
            errors += 1
        elif status == "warning":
            warnings += 1
        else:
            ok += 1

    summary = {"total": len(raw), "ok": ok, "errors": errors, "warnings": warnings}
    conn.execute(
        "UPDATE imports SET status='validated', summary=?, checked_at=? WHERE id=?",
        (_j(summary), iso(now()), import_id))
    conn.commit()
    return summary


def _promo_overlap_nos(conn: sqlite3.Connection, start: datetime,
                       end: datetime) -> set[str]:
    """找出与 [start,end) 时段重叠的、已批准促销覆盖的价签。

    已批准促销 = 已通过审批的带 promo_end_at 的清单（其批次可能尚未下发或已下发）。
    """
    rows = conn.execute(
        """SELECT DISTINCT ir.tag_no
             FROM imports imp JOIN import_rows ir ON ir.import_id = imp.id
            WHERE imp.status IN ('approved')
              AND imp.promo_end_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM batches b JOIN rollbacks rb ON rb.batch_id=b.id
                   WHERE b.import_id=imp.id)
              AND NOT (imp.promo_end_at <= ? OR imp.effective_at >= ?)""",
        (iso(start), iso(end)),
    ).fetchall()
    return {r["tag_no"] for r in rows}


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _j(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)
