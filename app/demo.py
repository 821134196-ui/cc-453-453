"""演示编排：重置演示库后依次跑出 成功 / 部分失败离线 / 促销到期恢复 /
紧急回滚幂等 / 现场不一致 / 各类校验拒绝 案例，结果打印在终端。

run.py 默认调用它，随后启动 Web 后台。
"""
from __future__ import annotations

import time
from datetime import timedelta

from . import results as R
from . import services as S
from .adapter import EslAdapter
from .db import connect, init_db, iso, now, reset_storage
from .importer import create_import, save_upload, validate

OPERATOR = "u_yang"
MANAGER = "u_zhou"


def _csv(text: str, name: str = "list.csv"):
    return save_upload(name, text)


def _submit_validate(conn, text, eff=None, promo_end=None,
                     user=OPERATOR, name="list.csv"):
    eff = eff or now()
    path = _csv(text, name)
    iid = create_import(conn, path, user, eff, promo_end)
    summary = validate(conn, iid)
    return iid, summary


def _line(title):
    print("\n" + "=" * 66)
    print(f" {title}")
    print("=" * 66)


def _tag(conn, no):
    return conn.execute("SELECT * FROM tags WHERE tag_no=?", (no,)).fetchone()


def run_demo(serve_after: bool = False) -> None:
    reset_storage()
    conn = connect()
    init_db(conn, reset=False)
    adapter = EslAdapter(conn)
    print("已初始化演示数据：3 家门店 / 8 个价签 / 6 种商品；ESL-B-0201 长期离线。")

    # ------------------------------------------------- 案例 1：全部成功
    _line("案例 1【成功】普通调价批次，两个在线价签即时生效")
    iid, summ = _submit_validate(
        conn, "tag_no,new_price\nESL-A-0001,1.80\nESL-C-0102,3.50\n",
        name="日常调价_两店.csv")
    print(f"  导入#{iid} 校验汇总：{summ}")
    bid_ok = S.approve(conn, iid, MANAGER)
    S.dispatch_due(conn, adapter)
    st = S.batch_stats(conn, bid_ok)
    print(f"  批次生成 #{bid_ok}，下发结果 成功{st['success']}/等待{st['waiting']}/失败{st['failed']}")
    for no in ("ESL-A-0001", "ESL-C-0102"):
        t = _tag(conn, no)
        print(f"  {no}: 期望价￥{t['expected_price']:.2f} / 显示价￥{t['display_price']:.2f} / 展示版本 v{t['display_version']}")

    # ----------------------------------------- 案例 2：离线 + 部分成功
    _line("案例 2【离线·部分成功】一个长期离线、一个模拟首帧失败、一个正常")
    adapter.fail_once("ESL-A-0002", 1)   # 该标签下次下发失败，重试可成功
    iid, summ = _submit_validate(
        conn,
        "tag_no,new_price\nESL-D-0202,3.60\nESL-B-0201,4.20\nESL-A-0002,2.99\n",
        name="跨店调价_含离线.csv")
    print(f"  导入#{iid} 校验汇总：{summ}（离线不在导入期拦截，以下发回执为准）")
    bid_pf = S.approve(conn, iid, MANAGER)
    S.dispatch_due(conn, adapter)
    st = S.batch_stats(conn, bid_pf)
    print(f"  批次 #{bid_pf} 分段：成功{st['success']} / 等待{st['waiting']} / 失败{st['failed']} → 部分成功")
    for it in conn.execute("SELECT t.tag_no, bi.status, bi.last_result_code, bi.attempts "
                           "FROM batch_items bi JOIN tags t ON t.id=bi.tag_id "
                           "WHERE bi.batch_id=?", (bid_pf,)):
        print(f"    {it['tag_no']}: {it['status']}（{R.label(it['last_result_code'])}，尝试 {it['attempts']} 次）")
    t = _tag(conn, "ESL-B-0201")
    print(f"  离线标签两价分离：期望￥{t['expected_price']:.2f} ≠ 显示￥{t['display_price']:.2f}")

    # ------------------------------------------- 案例 2b：失败项单独重试
    _line("案例 2b【失败重试】对模拟失败项单独重试，立即成功；离线项仍失败")
    item = conn.execute(
        "SELECT bi.id FROM batch_items bi JOIN tags t ON t.id=bi.tag_id "
        "WHERE bi.batch_id=? AND t.tag_no='ESL-A-0002'", (bid_pf,)).fetchone()
    r = S.retry_item(conn, item["id"], adapter)["result"]
    print(f"  ESL-A-0002 重试 → {r['status']}（{R.label(r['last_result_code'])}）")
    off_item = conn.execute(
        "SELECT bi.id FROM batch_items bi JOIN tags t ON t.id=bi.tag_id "
        "WHERE bi.batch_id=? AND t.tag_no='ESL-B-0201'", (bid_pf,)).fetchone()
    r2 = S.retry_item(conn, off_item["id"], adapter)["result"]
    print(f"  ESL-B-0201 重试 → {r2['status']}（{R.label(r2['last_result_code'])}，仍然离线）")
    st = S.batch_stats(conn, bid_pf)
    print(f"  批次分段：成功{st['success']} / 等待{st['waiting']} / 失败{st['failed']}")

    # ------------------------------------------- 案例 3：促销到期自动恢复
    _line("案例 3【促销到期恢复】促销 2 秒后到期，调度器自动发起恢复批次")
    iid, summ = _submit_validate(
        conn, "tag_no,new_price\nESL-D-0203,9.90\n",
        eff=now(), promo_end=now() + timedelta(seconds=2),
        name="NFC橙汁_限时促销.csv")
    print(f"  促销清单#{iid} 校验：{summ}")
    bid_p = S.approve(conn, iid, MANAGER)
    S.dispatch_due(conn, adapter)
    t = _tag(conn, "ESL-D-0203")
    print(f"  促销批次 #{bid_p} 生效：期望￥{t['expected_price']:.2f} / 显示￥{t['display_price']:.2f}（促销价）")
    print("  等待促销时段结束 …", end="", flush=True)
    time.sleep(2.2)
    restores = S.create_due_restores(conn)
    S.dispatch_due(conn, adapter)
    print(" 到期。")
    for rid in restores:
        rst = S.batch_stats(conn, rid)
        print(f"  系统自动生成恢复批次 #{rid}：成功{rst['success']}/等待{rst['waiting']}/失败{rst['failed']}")
    t = _tag(conn, "ESL-D-0203")
    print(f"  ESL-D-0203 已恢复：期望￥{t['expected_price']:.2f} / 显示￥{t['display_price']:.2f} / 版本 v{t['display_version']}")

    # ------------------------------------------- 案例 4：紧急回滚 + 幂等
    _line("案例 4【紧急回滚】回滚案例 1 的成功批次，指向改价前明确版本")
    rb1 = S.rollback_batch(conn, bid_ok, OPERATOR, "促销设置错误，紧急恢复原价", adapter)
    head = conn.execute("SELECT status FROM rollbacks WHERE id=?", (rb1,)).fetchone()
    print(f"  回滚单 #{rb1} 结果：{head['status']}")
    for ri in conn.execute(
            "SELECT t.tag_no, ri.target_version, ri.target_price, ri.status, ri.result_code, ri.message "
            "FROM rollback_items ri JOIN tags t ON t.id=ri.tag_id WHERE ri.rollback_id=?",
            (rb1,)):
        print(f"    {ri['tag_no']} → v{ri['target_version']} ￥{ri['target_price']:.2f} "
              f"{ri['status']}（{R.label(ri['result_code'])}）")
    for no in ("ESL-A-0001", "ESL-C-0102"):
        t = _tag(conn, no)
        print(f"  {no} 当前：期望￥{t['expected_price']:.2f} / 显示￥{t['display_price']:.2f} / v{t['display_version']}")

    _line("案例 4b【回滚幂等】对同一批次再次回滚：全部空操作，价格与版本不变")
    before = [dict(_tag(conn, no)) for no in ("ESL-A-0001", "ESL-C-0102")]
    rb2 = S.rollback_batch(conn, bid_ok, OPERATOR, "重复点击回滚按钮", adapter)
    head2 = conn.execute("SELECT status FROM rollbacks WHERE id=?", (rb2,)).fetchone()
    print(f"  第二次回滚单 #{rb2} 结果：{head2['status']}")
    for ri in conn.execute(
            "SELECT t.tag_no, ri.status, ri.result_code, ri.message "
            "FROM rollback_items ri JOIN tags t ON t.id=ri.tag_id WHERE ri.rollback_id=?",
            (rb2,)):
        print(f"    {ri['tag_no']}: {ri['status']}（{R.label(ri['result_code'])}）— {ri['message']}")
    for old, no in zip(before, ("ESL-A-0001", "ESL-C-0102")):
        t = _tag(conn, no)
        same = (old["display_version"] == t["display_version"]
                and abs(old["display_price"] - t["display_price"]) < 1e-9)
        print(f"  {no} 版本/价格未再变化：{'是' if same else '否'}")

    # ------------------------------------------- 案例 5：现场抽检不一致
    _line("案例 5【现场抽检】对离线标签抽检：录入现场看到的价格做逐位核对")
    ok_ins = S.inspect(conn, "ESL-A-0001", "2.00", "巡店店长", None, "回滚后复核")
    print(f"  ESL-A-0001 录入 2.00 → {R.label(ok_ins['code'])}")
    bad = S.inspect(conn, "ESL-B-0201", "4.50", "巡店店长", None,
                    "货架旧贴纸未更换")
    # ESL-B-0201 当前显示 4.50（离线），录 4.50 一致；再造一个真正不一致
    bad2 = S.inspect(conn, "ESL-B-0201", "3.99", "巡店店长", None,
                     "标签显示与抄录不符")
    print(f"  ESL-B-0201 录入 4.50 → {R.label(bad['code'])}（与价签显示一致，但与系统期望 ￥4.20 偏离）")
    print(f"  ESL-B-0201 录入 3.99 → {R.label(bad2['code'])}（现场价格不一致，需人工处置）")
    nf = S.inspect(conn, "ESL-X-9999", "1.00")
    print(f"  扫描不存在编号 ESL-X-9999 → {R.label(nf['code'])}")

    # ------------------------------------------- 案例 6：校验类独立结果
    _line("案例 6【差异校验】重复商品 / 价格越限 / 价签不存在，一行一结果")
    iid, summ = _submit_validate(
        conn,
        "tag_no,new_price\nESL-A-0001,1.70\nESL-A-0001,1.60\n"
        "ESL-A-0002,99.00\nESL-Z-0000,2.00\n",
        eff=now() + timedelta(days=1), name="问题清单.csv")
    print(f"  问题清单#{iid} 汇总：{summ}")
    for r in conn.execute("SELECT line_no,tag_no,result_code,message FROM import_rows WHERE import_id=?",
                          (iid,)):
        print(f"    第{r['line_no']}行 {r['tag_no'] or '(空)'}: {R.label(r['result_code'])} — {r['message']}")

    # ------------------------------------------- 案例 7：审批分离
    _line("案例 7【审批分离】提交人审批自己的清单被拒绝")
    iid, _ = _submit_validate(
        conn, "tag_no,new_price\nESL-D-0202,3.40\n",
        eff=now() + timedelta(days=1), user=OPERATOR, name="自审测试.csv")
    try:
        S.approve(conn, iid, OPERATOR)
        print("  未拦截（异常！）")
    except S.ServiceError as e:
        print(f"  {OPERATOR} 审批自己的清单 → 拒绝：{e.code}（{e.message}）")
    bid_self = S.approve(conn, iid, MANAGER)
    print(f"  改由店长 {MANAGER} 审批 → 通过，生成等待批次 #{bid_self}（未来生效，持互斥锁）")

    # ------------------------------------------- 案例 8：批次互斥
    _line("案例 8【批次互斥】两个批次不能同时改同一门店同一商品")
    iid_b, summ_b = _submit_validate(
        conn, "tag_no,new_price\nESL-D-0202,3.20\n",
        eff=now() + timedelta(days=2), user="u_wu", name="互斥测试.csv")
    r = conn.execute(
        "SELECT result_code,message FROM import_rows WHERE import_id=?",
        (iid_b,)).fetchone()
    print(f"  竞争清单#{iid_b} 差异校验 → {R.label(r['result_code'])}（汇总：{summ_b}）")
    print(f"    {r['message']}")
    try:
        S.approve(conn, iid_b, MANAGER)
        print("  审批未拦截（异常！）")
    except S.ServiceError as e:
        print(f"  即使强行提交审批也被拒绝：{e.code}（{e.message}）")
    print(f"  必须等批次 #{bid_self} 到期完成或回滚释放锁后，该商品才能再改。")

    # ------------------------------------------- 案例 9：促销时段重叠
    _line("案例 9【促销重叠】与已批准促销时段重叠的清单被独立标记")
    iid_p1, _ = _submit_validate(
        conn, "tag_no,new_price\nESL-A-0002,2.50\n",
        eff=now() + timedelta(days=3), promo_end=now() + timedelta(days=5),
        user="u_wu", name="国庆促销.csv")
    S.approve(conn, iid_p1, MANAGER)
    iid_p2, _ = _submit_validate(
        conn, "tag_no,new_price\nESL-A-0002,2.30\n",
        eff=now() + timedelta(days=4), promo_end=now() + timedelta(days=6),
        user=OPERATOR, name="重叠促销.csv")
    r = conn.execute(
        "SELECT result_code,message FROM import_rows WHERE import_id=?",
        (iid_p2,)).fetchone()
    print(f"  已批准促销：第3~5天；冲突清单：第4~6天 → {R.label(r['result_code'])}")
    print(f"    {r['message']}")

    _line("演示数据已就绪")
    print("  Web 后台可查看：总览两价偏离、批次分段条、差异表、审批队列、扫码核验、回滚记录。")
    if serve_after:
        print("  启动后台服务中 …\n")
