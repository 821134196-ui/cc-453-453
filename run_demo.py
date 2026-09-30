# -*- coding: utf-8 -*-
"""一键启动：重置演示数据 → 跑自动化测试 → 脚本化生成
【成功 / 离线 / 部分失败待重试 / 促销到期恢复 / 紧急回滚+幂等 / 现场不一致】案例
→ 启动本地 Web 后台。

用法：
  python3 run_demo.py              # 自测 + 造数据 + 启动后台 http://127.0.0.1:8000
  python3 run_demo.py --test-only  # 只跑测试
  python3 run_demo.py --port 8080
"""
from __future__ import annotations

import argparse
import csv
import os
import struct
import sys
import zlib
from datetime import datetime, timedelta

from app import db, services
from app.tests import run_all

FMT = services.DT_FMT


def write_import(name: str, rows: list[list[str]]) -> str:
    path = os.path.join(db.IMPORT_DIR, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["store_id", "product_id", "new_price", "effective_at", "promo_start", "promo_end"])
        w.writerows(rows)
    return path


def make_photo(path: str):
    """用标准库合成一张 220x150 的占位抽检照片（零售红底 + 白色价签块）。"""
    w, h = 220, 150
    red = (215, 0, 26)

    def px(x, y):
        # 中间白色价签矩形
        if 35 <= x <= w - 35 and 45 <= y <= h - 35:
            return (255, 255, 255) if not (60 <= y <= 78) else red  # 一条红色“价格行”
        return red

    raw = b"".join(
        b"\x00" + b"".join(bytes(px(x, y)) for x in range(w)) for y in range(h)
    )

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def seed_demo():
    print("\n" + "=" * 66)
    print("生成演示案例 …")
    print("=" * 66)
    db.init_db()
    conn = db.get_conn()
    now = datetime.now().replace(microsecond=0)
    t = lambda **k: (now + timedelta(**k)).strftime(FMT)
    results = []

    # ① 全部成功的改价批次（含另一门店）
    p1 = write_import("demo_01_all_success.csv", [
        ["S001", "100003", "64.90", t(), "", ""],
        ["S002", "100001", "1.90", t(), "", ""],
        ["S002", "100002", "3.30", t(), "", ""],
    ])
    i1 = services.import_price_list(p1, "u_op", "演示①全成功批次", conn=conn, ref_time=now)
    a1 = services.approve_import(i1["import_id"], "u_mgr", conn=conn, ref_time=now)
    services.activate_due_batches(conn, now + timedelta(seconds=5))
    results.append(("① 全部成功", a1["batch"]["code"], "3/3 回执成功，价签展示版本已更新"))

    # ② 部分失败批次：ESL-0001 模拟通信失败，保留失败项供页面单项重试
    p2 = write_import("demo_02_partial_fail.csv", [
        ["S001", "100001", "1.80", t(minutes=1), "", ""],
        ["S001", "100002", "3.20", t(minutes=1), "", ""],
    ])
    i2 = services.import_price_list(p2, "u_op", "演示②部分失败（可单项重试）",
                                    conn=conn, ref_time=now + timedelta(seconds=10))
    a2 = services.approve_import(i2["import_id"], "u_mgr", conn=conn,
                                 ref_time=now + timedelta(seconds=10))
    services.activate_due_batches(
        conn, now + timedelta(minutes=1, seconds=10),
        adapter=services.MockTagAdapter(fail_tags={"ESL-0001"}))
    prog2 = services.batch_progress(conn, a2["batch"]["id"])
    results.append(("② 部分失败", a2["batch"]["code"],
                    f"成功 {prog2['success']} / 失败 {prog2['failed']}，失败项可在批次页单独重试"))

    # ③ 离线价签导入被拦截
    p3 = write_import("demo_03_offline.csv", [
        ["S001", "100005", "9.90", t(), "", ""],
    ])
    i3 = services.import_price_list(p3, "u_op", "演示③离线价签拦截", conn=conn,
                                    ref_time=now + timedelta(seconds=20))
    results.append(("③ 价签离线", i3["code"], "导入校验直接返回 TAG_OFFLINE，不进入审批"))

    # ④ 促销完整生命周期：3 小时前生效、1 小时前到期 → 自动恢复批次
    t0 = now - timedelta(hours=3)
    p4 = write_import("demo_04_promo.csv", [
        ["S001", "100004", "59.90", t0.strftime(FMT),
         (t0 + timedelta(minutes=5)).strftime(FMT), (now - timedelta(hours=1)).strftime(FMT)],
    ])
    i4 = services.import_price_list(p4, "u_op", "演示④金典牛奶促销（已到期）",
                                    conn=conn, ref_time=t0)
    services.approve_import(i4["import_id"], "u_mgr", conn=conn, ref_time=t0)
    services.activate_due_batches(conn, t0 + timedelta(minutes=6))
    restored = services.expire_promotions(conn, now)
    rb_code = restored[0]["code"] if restored else "—"
    results.append(("④ 促销到期自动恢复", rb_code,
                    "促销 65.00→59.90 已成功，到期后进程任务自动恢复到 65.00"))

    # ⑤ 紧急回滚（调和油 64.90 紧急回滚 69.90），并演示重复执行幂等
    v_old = conn.execute(
        "SELECT id FROM price_versions WHERE store_id='S001' AND product_id='100003'"
        " ORDER BY id LIMIT 1").fetchone()["id"]
    r5 = services.rollback("S001", "100003", v_old, executed_by="u_mgr", conn=conn,
                           ref=now + timedelta(seconds=30), note="演示紧急回滚")
    r5b = services.rollback("S001", "100003", v_old, executed_by="u_mgr", conn=conn,
                            ref=now + timedelta(seconds=31), note="演示重复回滚")
    results.append(("⑤ 紧急回滚", r5["batch_code"],
                    f"回滚到明确版本 v{v_old}；重复执行={r5b['code']}（无新版本/批次/下发）"))

    # ⑥ 定时生效：+1 小时后生效，批次保持 pending（进度条灰色“等待”段）
    p6 = write_import("demo_06_scheduled.csv", [
        ["S001", "100006", "12.50", t(hours=1), "", ""],
    ])
    i6 = services.import_price_list(p6, "u_op", "演示⑥定时生效（+1小时）",
                                    conn=conn, ref_time=now + timedelta(seconds=40))
    a6 = services.approve_import(i6["import_id"], "u_mgr", conn=conn,
                                 ref_time=now + timedelta(seconds=40))
    results.append(("⑥ 定时生效待执行", a6["batch"]["code"], "到点由进程内调度任务自动下发"))

    # ⑦ 各类校验结果集合单（留在审批队列），并演示审批分离被拒
    p7 = write_import("demo_07_all_checks.csv", [
        ["S001", "100001", "1.50", t(), "", ""],                # BATCH_CONFLICT（②在途）
        ["S001", "100001", "1.40", t(), "", ""],                # DUPLICATE_PRODUCT
        ["S001", "100004", "999.00", t(), "", ""],              # PRICE_LIMIT
        ["S001", "100005", "9.90", t(), "", ""],                # TAG_OFFLINE
        ["S999", "100001", "1.80", t(), "", ""],                # STORE_NOT_FOUND
        ["S001", "888888", "1.80", t(), "", ""],                # PRODUCT_NOT_FOUND
        ["S001", "100002", "abc", t(), "", ""],                 # BAD_NUMBER
        ["S002", "100002", "2.99", "2020-01-01 00:00:00", "", ""],  # INVALID_TIME
        ["S002", "100001", "1.70", t(hours=2), "", ""],         # ok
    ])
    i7 = services.import_price_list(p7, "u_op", "演示⑦全部校验结果集合单",
                                    conn=conn, ref_time=now + timedelta(seconds=50))
    self_r = services.approve_import(i7["import_id"], "u_op", conn=conn,
                                     ref_time=now + timedelta(seconds=51))
    results.append(("⑦ 审批分离", i7["code"],
                    f"8 类独立校验结果各就各位；提交人自审被拒：{self_r['code']}，仍在审批队列"))

    # ⑧ 现场抽检：不一致（失败价签）/ 一致（回滚后商品）/ 离线
    photo = os.path.join(db.PHOTO_DIR, "demo_inspection_ESL-0001.png")
    make_photo(photo)
    rel_photo = os.path.relpath(photo, db.BASE_DIR)
    r_bad = services.inspect_tag("ESL-0001", 2.00, "u_insp 巡检员·小吴", rel_photo,
                                 "期望 1.80，现场仍显示旧价（等宽逐位核对）", conn=conn, ref=now)
    r_ok = services.inspect_tag("ESL-0003", 69.90, "u_insp 巡检员·小吴", None,
                                "回滚后现场与期望一致", conn=conn, ref=now)
    r_off = services.inspect_tag("ESL-0005", None, "u_insp 巡检员·小吴", None,
                                 "价签离线", conn=conn, ref=now)
    results.append(("⑧ 现场抽检", r_bad["code"],
                    f"ESL-0001={r_bad['result']}，ESL-0003={r_ok['result']}，ESL-0005={r_off['result']}"))

    conn.commit()
    conn.close()

    for name, code, desc in results:
        print(f"  {name:<16} {code:<24} {desc}")
    print("\n本地目录：")
    print(f"  导入文件 : {db.IMPORT_DIR}")
    print(f"  抽检照片 : {db.PHOTO_DIR}")
    print(f"  SQLite   : {db.DB_PATH}")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--test-only", action="store_true", help="只跑自动化测试")
    ap.add_argument("--no-test", action="store_true", help="跳过测试直接造数启动")
    args = ap.parse_args()

    if not args.no_test:
        if run_all() != 0:
            print("\n自测未通过，已中止启动。")
            sys.exit(1)
    if args.test_only:
        return
    seed_demo()
    from app.server import serve
    print("\n浏览器打开下列页面（Ctrl+C 退出）：")
    print("  /            总览：实际显示价 vs 系统期望价")
    print("  /imports     调价导入与差异校验（固定旧价/新价/生效时间列）")
    print("  /approvals   审批队列（演示⑦集合单在此，试试提交人自审）")
    print("  /batches     批次分段进度条（成功/等待/失败），失败项可单项重试")
    print("  /inspect     现场核验大号扫码（输入 ESL-0001 看不一致案例）")
    print("  /rollback    价格版本链与回滚记录（幂等跳过有留痕）")
    serve(port=args.port)


if __name__ == "__main__":
    main()
