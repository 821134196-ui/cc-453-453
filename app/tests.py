# -*- coding: utf-8 -*-
"""自测：批次互斥 / 促销时段重叠 / 审批分离 / 部分失败重试 / 回滚幂等，
以及七种独立结果码、现场抽检不一致、促销到期恢复。

运行：python3 -m app.tests
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from . import db, services

TMP = tempfile.mkdtemp(prefix="esl_test_")
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def fresh_db() -> str:
    path = os.path.join(TMP, f"t_{next(fresh_db.n)}.db")
    db.init_db(path)
    return path


fresh_db.n = iter(range(1000))


def write_csv(rows: list[str]) -> str:
    p = os.path.join(TMP, f"c_{next(write_csv.n)}.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("store_id,product_id,new_price,effective_at,promo_start,promo_end\n")
        for r in rows:
            f.write(r + "\n")
    return p


write_csv.n = iter(range(1000))


def dt(base: datetime, **kw) -> str:
    return (base + timedelta(**kw)).strftime(services.DT_FMT)


# ---------------------------------------------------------------- 1. 七种独立结果码

def test_result_codes():
    print("\n【1】导入差异校验：重复商品 / 越限 / 离线 / 不存在 / 时间无效 / 格式错误")
    path_db = fresh_db()
    conn = db.get_conn(path_db)
    base = datetime(2026, 9, 30, 9, 0, 0)
    now_s = dt(base)
    csv_path = write_csv([
        f"S001,100001,1.80,{now_s},,",                 # ok
        f"S001,100001,1.90,{now_s},,",                 # 重复商品
        f"S001,100004,999.00,{now_s},,",               # 价格越限（65 → 999）
        f"S001,100005,9.90,{now_s},,",                 # 价签离线
        f"S999,100001,1.80,{now_s},,",                 # 门店不存在
        f"S001,888888,1.80,{now_s},,",                 # 商品不存在
        f"S001,100002,-5,{now_s},,",                   # 价格格式错误
        f"S002,100002,2.99,2020-01-01 00:00:00,,",     # 生效时间无效
    ])
    res = services.import_price_list(csv_path, "u_op", conn=conn, ref_time=base)
    items = services.import_diff_items(conn, res["import_id"])
    got = {i["line_no"]: i["check_status"] for i in items}
    check("仅 1 行通过", res["ok"] == 1, str(res))
    check("第2行=重复商品 DUPLICATE_PRODUCT", got[2] == "DUPLICATE_PRODUCT", str(got))
    check("第3行=价格越限 PRICE_LIMIT", got[3] == "PRICE_LIMIT", str(got))
    check("第4行=价签离线 TAG_OFFLINE", got[4] == "TAG_OFFLINE", str(got))
    check("第5行=门店不存在 STORE_NOT_FOUND", got[5] == "STORE_NOT_FOUND", str(got))
    check("第6行=商品不存在 PRODUCT_NOT_FOUND", got[6] == "PRODUCT_NOT_FOUND", str(got))
    check("第7行=价格格式错误 BAD_NUMBER", got[7] == "BAD_NUMBER", str(got))
    check("第8行=生效时间无效 INVALID_TIME", got[8] == "INVALID_TIME", str(got))
    check("全部不通过时导入单 rejected",
          services.import_price_list(write_csv([f"S001,888888,1.0,{now_s},,"]),
                                     "u_op", conn=conn, ref_time=base)["status"] == "rejected")
    conn.close()


# ---------------------------------------------------------------- 2. 审批分离

def test_approval_separation():
    print("\n【2】审批人不能审批自己提交的清单")
    conn = db.get_conn(fresh_db())
    base = datetime(2026, 9, 30, 9, 0, 0)
    imp = services.import_price_list(
        write_csv([f"S001,100001,1.80,{dt(base)},,"]), "u_op", conn=conn, ref_time=base)
    r_self = services.approve_import(imp["import_id"], "u_op", conn=conn, ref_time=base)
    check("本人审批被拒绝 APPROVAL_SELF", (not r_self["ok"]) and r_self["code"] == "APPROVAL_SELF",
          str(r_self))
    il = conn.execute("SELECT status FROM import_lists WHERE id=?", (imp["import_id"],)).fetchone()
    check("拒绝后清单仍待审批", il["status"] == "validated", il["status"])
    r = services.approve_import(imp["import_id"], "u_mgr", conn=conn, ref_time=base)
    check("他人（经理）审批通过并生成批次", r["ok"] and r["batch"]["items"] == 1, str(r))
    rj = services.reject_import(imp["import_id"], "u_mgr", "x", conn=conn)
    check("已生成批次的单不可再操作", not rj["ok"])
    conn.close()


# ---------------------------------------------------------------- 3. 批次互斥

def test_batch_mutex():
    print("\n【3】两个批次不能同时改同一门店同一商品")
    conn = db.get_conn(fresh_db())
    base = datetime(2026, 9, 30, 9, 0, 0)
    # 两份清单在没有任何批次时先后导入 → 都能通过校验
    a = services.import_price_list(
        write_csv([f"S001,100001,1.80,{dt(base)},,"]), "u_op", conn=conn, ref_time=base)
    b = services.import_price_list(
        write_csv([f"S001,100001,1.70,{dt(base)},,"]), "u_op", conn=conn,
        ref_time=base + timedelta(seconds=1))
    check("两份清单导入期均通过", a["status"] == "validated" and b["status"] == "validated")
    ra = services.approve_import(a["import_id"], "u_mgr", conn=conn, ref_time=base)
    rb = services.approve_import(b["import_id"], "u_mgr", conn=conn,
                                 ref_time=base + timedelta(seconds=2))
    check("第一份生成批次", ra["ok"])
    check("第二份审批时明细被互斥排除（批次 0 条）",
          rb["ok"] and rb["batch"]["items"] == 0, str(rb))
    item = conn.execute(
        "SELECT check_status FROM import_items WHERE import_list_id=?", (b["import_id"],)).fetchone()
    check("第二份明细被标记 BATCH_CONFLICT", item["check_status"] == "BATCH_CONFLICT",
          item["check_status"])
    # 第三个清单在批次在途期导入：导入校验阶段直接报互斥
    c = services.import_price_list(
        write_csv([f"S001,100001,1.60,{dt(base)},,"]), "u_op", conn=conn,
        ref_time=base + timedelta(seconds=3))
    ci = services.import_diff_items(conn, c["import_id"])[0]
    check("在途期间导入直接 BATCH_CONFLICT", ci["check_status"] == "BATCH_CONFLICT",
          ci["check_status"])
    # 第一个批次完成后，同商品可以再改
    services.activate_due_batches(conn, base + timedelta(seconds=4))
    ab = services.active_batch_for(conn, "S001", "100001")
    check("批次完成后互斥解除", ab is None, str(dict(ab) if ab else None))
    conn.close()


# ---------------------------------------------------------------- 4. 促销时段重叠

def test_promo_overlap():
    print("\n【4】促销时段重叠独立拦截，不重叠可通过；到期自动恢复且只恢复一次")
    conn = db.get_conn(fresh_db())
    base = datetime(2026, 9, 30, 9, 0, 0)
    start, end = dt(base, hours=1), dt(base, hours=4)
    imp = services.import_price_list(
        write_csv([f"S001,100001,1.80,{dt(base)},{start},{end}"]),
        "u_op", conn=conn, ref_time=base)
    check("促销清单校验通过", imp["ok"] == 1)
    services.approve_import(imp["import_id"], "u_mgr", conn=conn, ref_time=base)
    services.activate_due_batches(conn, base + timedelta(minutes=2))
    cv = services.current_version(conn, "S001", "100001")
    check("生效后当前期望版本为促销价 1.80", abs(cv["price"] - 1.80) < 0.005, str(dict(cv)))

    # 与已存在促销交叉
    imp2 = services.import_price_list(
        write_csv([f"S001,100001,1.50,{dt(base, minutes=3)},{dt(base, hours=3)},{dt(base, hours=6)}"]),
        "u_op", conn=conn, ref_time=base + timedelta(minutes=3))
    check("重叠促销 PROMO_OVERLAP",
          services.import_diff_items(conn, imp2["import_id"])[0]["check_status"] == "PROMO_OVERLAP")
    # 完全落在其后的促销可以通过
    imp3 = services.import_price_list(
        write_csv([f"S001,100001,1.50,{dt(base, hours=4)},{dt(base, hours=5)},{dt(base, hours=8)}"]),
        "u_op", conn=conn, ref_time=base + timedelta(minutes=4))
    check("不重叠促销通过",
          services.import_diff_items(conn, imp3["import_id"])[0]["check_status"] == "ok")

    # 到期自动恢复
    restored1 = services.expire_promotions(conn, base + timedelta(hours=5))
    check("到期后自动发起恢复批次", len(restored1) == 1, str(restored1))
    cv2 = services.current_version(conn, "S001", "100001")
    check("期望价恢复为促销前 2.00", abs(cv2["price"] - 2.00) < 0.005, str(cv2["price"]))
    restored2 = services.expire_promotions(conn, base + timedelta(hours=6))
    check("重复扫描不会重复恢复（幂等）", restored2 == [])
    conn.close()


# ---------------------------------------------------------------- 5. 部分失败 + 单项重试

def test_partial_retry():
    print("\n【5】模拟适配器部分失败，失败项单独重试成功")
    conn = db.get_conn(fresh_db())
    base = datetime(2026, 9, 30, 9, 0, 0)
    imp = services.import_price_list(
        write_csv([
            f"S001,100001,1.80,{dt(base)},,",
            f"S001,100002,3.20,{dt(base)},,",
        ]), "u_op", conn=conn, ref_time=base)
    services.approve_import(imp["import_id"], "u_mgr", conn=conn, ref_time=base)
    bid = conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()["id"]
    fail_adapter = services.MockTagAdapter(fail_tags={"ESL-0002"})
    services.activate_due_batches(conn, base + timedelta(minutes=1), adapter=fail_adapter)
    p = services.batch_progress(conn, bid)
    check("部分成功：1 成功 1 失败", p == {"success": 1, "waiting": 0, "failed": 1, "total": 2}, str(p))
    b = conn.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()
    check("批次状态 partial", b["status"] == "partial", b["status"])
    tag2 = conn.execute("SELECT * FROM tags WHERE id='ESL-0002'").fetchone()
    check("失败价签仍显示旧价 3.50（实际价保留）", abs(tag2["displayed_price"] - 3.50) < 0.005)

    failed_item = conn.execute(
        "SELECT id FROM batch_items WHERE batch_id=? AND dispatch_status='failed'", (bid,)).fetchone()
    # 用仍然故障的适配器重试 → 仍失败，且不产生重复版本
    versions_before = conn.execute(
        "SELECT COUNT(*) c FROM price_versions WHERE store_id='S001' AND product_id='100002'").fetchone()["c"]
    r1 = services.retry_item(failed_item["id"], conn=conn, adapter=fail_adapter,
                             ref=base + timedelta(minutes=2))
    versions_after = conn.execute(
        "SELECT COUNT(*) c FROM price_versions WHERE store_id='S001' AND product_id='100002'").fetchone()["c"]
    check("故障未消除时重试仍失败", r1["code"] == "failed", str(r1))
    check("重试不重复追加价格版本", versions_before == versions_after)
    # 信号恢复后重试 → 成功
    r2 = services.retry_item(failed_item["id"], conn=conn,
                             adapter=services.MockTagAdapter(),
                             ref=base + timedelta(minutes=3))
    check("信号恢复后单项重试成功", r2["ok"] and r2["code"] == "success", str(r2))
    tag2 = conn.execute("SELECT * FROM tags WHERE id='ESL-0002'").fetchone()
    check("价签展示版本更新到 3.20", abs(tag2["displayed_price"] - 3.20) < 0.005)
    p = services.batch_progress(conn, bid)
    check("批次最终全部成功 done", p["failed"] == 0 and p["success"] == 2)
    # 幂等：对已成功项再次重试不产生副作用
    r3 = services.retry_item(failed_item["id"], conn=conn, ref=base + timedelta(minutes=4))
    check("成功项再重试幂等跳过", r3["code"] == "ALREADY_SUCCESS", str(r3))
    conn.close()


# ---------------------------------------------------------------- 6. 回滚幂等

def test_rollback():
    print("\n【6】紧急回滚必须指向明确版本，重复执行无副作用")
    conn = db.get_conn(fresh_db())
    base = datetime(2026, 9, 30, 9, 0, 0)
    imp = services.import_price_list(
        write_csv([f"S001,100001,1.80,{dt(base)},,"]), "u_op", conn=conn, ref_time=base)
    services.approve_import(imp["import_id"], "u_mgr", conn=conn, ref_time=base)
    services.activate_due_batches(conn, base + timedelta(minutes=1))
    v_initial = conn.execute(
        "SELECT id FROM price_versions WHERE store_id='S001' AND product_id='100001' ORDER BY id"
        ).fetchall()[0]
    check("当前价 1.80", abs(services.current_version(conn, "S001", "100001")["price"] - 1.80) < 0.005)

    # 必须指向真实版本
    r0 = services.rollback("S001", "100001", 999999, conn=conn, ref=base)
    check("无目标版本 → ROLLBACK_NO_TARGET", r0["code"] == "ROLLBACK_NO_TARGET", str(r0))
    # 指向别的商品的版本也不行
    other = conn.execute(
        "SELECT id FROM price_versions WHERE store_id='S001' AND product_id='100003'").fetchone()["id"]
    r0b = services.rollback("S001", "100001", other, conn=conn, ref=base)
    check("指向其他商品版本 → ROLLBACK_NO_TARGET", r0b["code"] == "ROLLBACK_NO_TARGET")

    # 正式回滚到初始版本
    r1 = services.rollback("S001", "100001", v_initial["id"], conn=conn,
                           ref=base + timedelta(minutes=2))
    check("首次回滚成功并生成回滚批次", r1["code"] == "success" and r1["batch_code"], str(r1))
    cv = services.current_version(conn, "S001", "100001")
    tag = conn.execute("SELECT * FROM tags WHERE id='ESL-0001'").fetchone()
    check("回滚版本明确指向历史版本", cv["change_type"] == "rollback"
          and cv["target_version_id"] == v_initial["id"])
    check("期望价与价签显示价均恢复 2.00",
          abs(cv["price"] - 2.00) < 0.005 and abs(tag["displayed_price"] - 2.00) < 0.005)
    versions_before = conn.execute(
        "SELECT COUNT(*) c FROM price_versions WHERE store_id='S001' AND product_id='100001'").fetchone()["c"]
    batches_before = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]

    # 重复回滚：幂等，无新版本、无新批次、无下发
    r2 = services.rollback("S001", "100001", v_initial["id"], conn=conn,
                           ref=base + timedelta(minutes=3))
    r3 = services.rollback("S001", "100001", v_initial["id"], conn=conn,
                           ref=base + timedelta(minutes=4))
    check("重复回滚返回 IDEMPOTENT_SKIP",
          r2["code"] == "IDEMPOTENT_SKIP" and r3["code"] == "IDEMPOTENT_SKIP", str(r2))
    versions_after = conn.execute(
        "SELECT COUNT(*) c FROM price_versions WHERE store_id='S001' AND product_id='100001'").fetchone()["c"]
    batches_after = conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]
    check("幂等：未新增价格版本、未新增批次",
          versions_before == versions_after and batches_before == batches_after)
    skips = conn.execute(
        "SELECT COUNT(*) c FROM rollbacks WHERE status='idempotent_skip'").fetchone()["c"]
    check("幂等跳过在回滚记录表中留痕", skips == 2, str(skips))
    conn.close()


# ---------------------------------------------------------------- 7. 现场抽检

def test_inspection():
    print("\n【7】现场抽检：实际价≠期望价给独立结果")
    conn = db.get_conn(fresh_db())
    base = datetime(2026, 9, 30, 9, 0, 0)
    imp = services.import_price_list(
        write_csv([f"S001,100001,1.80,{dt(base)},,"]), "u_op", conn=conn, ref_time=base)
    services.approve_import(imp["import_id"], "u_mgr", conn=conn, ref_time=base)
    services.activate_due_batches(conn, base + timedelta(minutes=1),
                                  adapter=services.MockTagAdapter(fail_tags={"ESL-0001"}))
    # 期望 1.80，价签仍显示 2.00
    r_bad = services.inspect_tag("ESL-0001", 2.00, "u_insp", None, "", conn=conn,
                                 ref=base + timedelta(minutes=5))
    check("现场 2.00 ≠ 期望 1.80 → inconsistent", r_bad["result"] == "inconsistent", str(r_bad))
    r_ok = services.inspect_tag("ESL-0001", 1.80, "u_insp", None, "", conn=conn,
                                ref=base + timedelta(minutes=6))
    check("现场录 1.80 → consistent", r_ok["result"] == "consistent")
    r_miss = services.inspect_tag("ESL-9999", 1.0, "u_insp", None, "", conn=conn)
    check("未知价签 → TAG_NOT_FOUND", not r_miss["ok"] and r_miss["code"] == "TAG_NOT_FOUND")
    conn.close()


def run_all() -> int:
    print("=" * 66)
    print("电子价签批量改价与回滚核验系统 —— 自动化自测")
    print("=" * 66)
    for fn in (test_result_codes, test_approval_separation, test_batch_mutex,
               test_promo_overlap, test_partial_retry, test_rollback, test_inspection):
        fn()
    print("\n" + "=" * 66)
    print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys
    sys.exit(run_all())
