"""自动化测试：批次互斥、促销时段重叠、审批分离、部分失败、回滚幂等。

每个用例使用独立的临时 SQLite 文件。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import results as R
from app import services as S
from app.adapter import EslAdapter
from app.db import connect, init_db, now
from app import importer as imp_mod
from app.importer import create_import, save_upload, validate

OPERATOR = "u_yang"
OPERATOR2 = "u_wu"
MANAGER = "u_zhou"


class EslTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = connect(self.tmp.name)
        init_db(self.conn, reset=True)
        self.adapter = EslAdapter(self.conn)
        self._tmp_imports = tempfile.TemporaryDirectory()
        self._saved_import_dir = imp_mod.IMPORT_DIR
        imp_mod.IMPORT_DIR = Path(self._tmp_imports.name)

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        imp_mod.IMPORT_DIR = self._saved_import_dir
        self._tmp_imports.cleanup()

    # ---------------------------------------------------------- helpers
    def submit(self, csv_text, eff=None, promo_end=None, user=OPERATOR):
        path = save_upload("test.csv", csv_text)
        iid = create_import(
            self.conn, path, user,
            eff or (now() + timedelta(minutes=1)),
            promo_end)
        validate(self.conn, iid)
        return iid

    def row_code(self, iid, tag_no):
        r = self.conn.execute(
            "SELECT result_code,status FROM import_rows WHERE import_id=? AND tag_no=?",
            (iid, tag_no)).fetchone()
        return r["result_code"], r["status"]

    def approve(self, iid, user=MANAGER):
        return S.approve(self.conn, iid, user)

    def tag(self, no):
        return self.conn.execute(
            "SELECT * FROM tags WHERE tag_no=?", (no,)).fetchone()

    def dispatch(self):
        S.dispatch_due(self.conn, self.adapter)

    # ----------------------------------------------------- 1. 批次互斥
    def test_01_batch_mutex(self):
        """等待中的批次锁住 (门店, 商品)：清单打互斥结果，审批也被拒；回滚后放行。"""
        iid_a = self.submit(
            "tag_no,new_price\nESL-D-0202,3.40\n",
            eff=now() + timedelta(days=1))
        bid = self.approve(iid_a)

        iid_b = self.submit(
            "tag_no,new_price\nESL-D-0202,3.20\n",
            eff=now() + timedelta(days=2), user=OPERATOR2)
        code, status = self.row_code(iid_b, "ESL-D-0202")
        self.assertEqual(code, R.MUTEX_CONFLICT)
        self.assertEqual(status, "error")

        with self.assertRaises(S.ServiceError) as cm:
            self.approve(iid_b)
        self.assertEqual(cm.exception.code, R.MUTEX_CONFLICT)

        # 原批次紧急回滚（未生效条目撤销、锁释放）后，同商品可再次改价
        S.rollback_batch(self.conn, bid, OPERATOR, "测试释放锁", self.adapter)
        iid_c = self.submit(
            "tag_no,new_price\nESL-D-0202,3.20\n",
            eff=now() + timedelta(days=2), user=OPERATOR2)
        self.assertEqual(self.row_code(iid_c, "ESL-D-0202")[0], R.OK)

    # ----------------------------------------------------- 2. 促销重叠
    def test_02_promo_overlap(self):
        """与已批准促销时段重叠的清单独立标记；不重叠的通过。"""
        iid1 = self.submit(
            "tag_no,new_price\nESL-A-0002,2.50\n",
            eff=now() + timedelta(days=3),
            promo_end=now() + timedelta(days=5), user=OPERATOR2)
        self.approve(iid1)

        iid2 = self.submit(
            "tag_no,new_price\nESL-A-0002,2.30\n",
            eff=now() + timedelta(days=4),
            promo_end=now() + timedelta(days=6))
        self.assertEqual(self.row_code(iid2, "ESL-A-0002")[0], R.PROMO_OVERLAP)

        # 首尾相接（第 5 天开始）不算“时段重叠”，但第一单恢复完成前锁仍占用，
        # 系统以互斥结果（而非促销重叠）独立提示
        iid3 = self.submit(
            "tag_no,new_price\nESL-A-0002,2.30\n",
            eff=now() + timedelta(days=5),
            promo_end=now() + timedelta(days=7))
        self.assertEqual(self.row_code(iid3, "ESL-A-0002")[0], R.MUTEX_CONFLICT)

        # 不同价签不受影响
        iid4 = self.submit(
            "tag_no,new_price\nESL-C-0102,3.20\n",
            eff=now() + timedelta(days=4),
            promo_end=now() + timedelta(days=6))
        self.assertEqual(self.row_code(iid4, "ESL-C-0102")[0], R.OK)

    # ----------------------------------------------------- 3. 审批分离
    def test_03_approval_separation(self):
        """提交人不能自审；非 manager 不能审；店长可以审。"""
        iid = self.submit(
            "tag_no,new_price\nESL-D-0202,3.40\n",
            eff=now() + timedelta(days=1), user=OPERATOR)

        with self.assertRaises(S.ServiceError) as cm:
            self.approve(iid, user=OPERATOR)
        self.assertEqual(cm.exception.code, R.SELF_APPROVAL)

        with self.assertRaises(S.ServiceError) as cm:
            self.approve(iid, user=OPERATOR2)
        self.assertEqual(cm.exception.code, R.SELF_APPROVAL)  # 非店长同样拒绝

        # 清单仍是待审批，没有产生批次
        n = self.conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]
        self.assertEqual(n, 0)

        bid = self.approve(iid, user=MANAGER)
        self.assertTrue(bid > 0)
        imp = self.conn.execute(
            "SELECT submitted_by,approved_by FROM imports WHERE id=?",
            (iid,)).fetchone()
        self.assertNotEqual(imp["submitted_by"], imp["approved_by"])

    # ----------------------------------------------------- 4. 部分失败
    def test_04_partial_failure_and_retry(self):
        """离线标签与模拟首帧失败形成部分成功；失败项可单独重试。"""
        self.adapter.fail_once("ESL-A-0002", 1)  # 下一次下发失败，重试成功
        iid = self.submit(
            "tag_no,new_price\n"
            "ESL-D-0202,3.60\n"   # 在线，成功
            "ESL-A-0002,2.99\n"   # 模拟首帧失败
            "ESL-B-0201,4.20\n",  # 长期离线
            eff=now())
        bid = self.approve(iid)
        self.dispatch()

        st = S.batch_stats(self.conn, bid)
        self.assertEqual((st["success"], st["failed"], st["waiting"]), (1, 2, 0))
        b = self.conn.execute("SELECT status FROM batches WHERE id=?",
                              (bid,)).fetchone()
        self.assertEqual(b["status"], "partial")  # 1 成 2 败：部分成功

        # 模拟失败项单独重试 -> 成功，批次变部分成功
        item = self.conn.execute(
            "SELECT bi.id FROM batch_items bi JOIN tags t ON t.id=bi.tag_id "
            "WHERE bi.batch_id=? AND t.tag_no='ESL-A-0002'", (bid,)).fetchone()
        S.retry_item(self.conn, item["id"], self.adapter)
        st = S.batch_stats(self.conn, bid)
        self.assertEqual((st["success"], st["failed"]), (2, 1))
        b = self.conn.execute("SELECT status FROM batches WHERE id=?",
                              (bid,)).fetchone()
        self.assertEqual(b["status"], "partial")

        # 离线项重试仍然失败，结果码独立
        off = self.conn.execute(
            "SELECT bi.id, bi.last_result_code FROM batch_items bi JOIN tags t ON t.id=bi.tag_id "
            "WHERE bi.batch_id=? AND t.tag_no='ESL-B-0201'", (bid,)).fetchone()
        S.retry_item(self.conn, off["id"], self.adapter)
        off2 = self.conn.execute(
            "SELECT last_result_code,attempts FROM batch_items WHERE id=?",
            (off["id"],)).fetchone()
        self.assertEqual(off2["last_result_code"], R.ACK_TAG_OFFLINE)
        self.assertGreaterEqual(off2["attempts"], 2)

        # 期望价已前进、显示价停在旧值 -> 两价偏离被保留
        t = self.tag("ESL-B-0201")
        self.assertAlmostEqual(t["expected_price"], 4.20, places=2)
        self.assertAlmostEqual(t["display_price"], 4.50, places=2)
        # 成功标签两价一致
        t2 = self.tag("ESL-D-0202")
        self.assertAlmostEqual(t2["expected_price"], t2["display_price"], places=9)

    # ----------------------------------------------------- 5. 回滚幂等
    def test_05_rollback_points_to_version_and_idempotent(self):
        """回滚指向改价前明确版本；重复执行全部空操作、价格版本不变。"""
        iid = self.submit(
            "tag_no,new_price\nESL-A-0001,1.80\nESL-C-0102,3.50\n",
            eff=now())
        bid = self.approve(iid)
        self.dispatch()
        self.assertAlmostEqual(self.tag("ESL-A-0001")["display_price"], 1.80, places=2)

        rb1 = S.rollback_batch(self.conn, bid, OPERATOR, "设置错误", self.adapter)
        head = self.conn.execute("SELECT status FROM rollbacks WHERE id=?",
                                 (rb1,)).fetchone()
        self.assertEqual(head["status"], "succeeded")

        # 指向明确历史版本：回滚目标 = 批次条目记录的 old_version
        for it in self.conn.execute(
                "SELECT * FROM batch_items WHERE batch_id=?", (bid,)):
            ri = self.conn.execute(
                "SELECT * FROM rollback_items WHERE rollback_id=? AND tag_id=?",
                (rb1, it["tag_id"])).fetchone()
            self.assertEqual(ri["target_version"], it["old_version"])
            self.assertAlmostEqual(ri["target_price"], it["old_price"], places=9)
            self.assertEqual(ri["status"], "success")

        self.assertAlmostEqual(self.tag("ESL-A-0001")["display_price"], 2.00, places=2)
        self.assertAlmostEqual(self.tag("ESL-C-0102")["display_price"], 3.80, places=2)

        def snapshot():
            return {r["tag_no"]: (r["display_version"], round(r["display_price"], 4))
                    for r in self.conn.execute("SELECT * FROM tags")}

        before = snapshot()
        ver_count = self.conn.execute(
            "SELECT COUNT(*) c FROM price_versions").fetchone()["c"]

        # 第二次、第三次回滚：全部空操作，无版本追加，无价格变化
        for _ in range(2):
            rbid = S.rollback_batch(self.conn, bid, OPERATOR, "重复点击", self.adapter)
            head = self.conn.execute(
                "SELECT status FROM rollbacks WHERE id=?", (rbid,)).fetchone()
            self.assertEqual(head["status"], "no_op")
            n = self.conn.execute(
                "SELECT COUNT(*) c FROM rollback_items WHERE rollback_id=? AND status='no_op'",
                (rbid,)).fetchone()["c"]
            self.assertEqual(n, 2)
        self.assertEqual(snapshot(), before)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM price_versions").fetchone()["c"],
            ver_count)
        self.assertEqual(
            self.conn.execute("SELECT status FROM batches WHERE id=?",
                              (bid,)).fetchone()["status"], "rolled_back")

    # ----------------------------------------------------- 附加：校验结果码
    def test_06_validation_result_codes(self):
        """重复商品、价格越限、价签不存在各自独立成行；空变化仅提示。"""
        iid = self.submit(
            "tag_no,new_price\n"
            "ESL-A-0001,1.70\n"
            "ESL-A-0001,1.60\n"
            "ESL-A-0002,99.00\n"
            "ESL-Z-0000,2.00\n"
            "ESL-C-0102,3.80\n",
            eff=now() + timedelta(days=1))
        codes = {r["tag_no"]: r["result_code"] for r in self.conn.execute(
            "SELECT tag_no,result_code FROM import_rows WHERE import_id=?",
            (iid,))}
        self.assertEqual(codes["ESL-A-0001"], R.DUPLICATE_PRODUCT)
        self.assertEqual(codes["ESL-A-0002"], R.PRICE_LIMIT)
        self.assertEqual(codes["ESL-Z-0000"], R.TAG_NOT_FOUND)
        self.assertEqual(codes["ESL-C-0102"], R.NO_CHANGE)
        summ = self.conn.execute("SELECT summary FROM imports WHERE id=?",
                                 (iid,)).fetchone()["summary"]
        import json
        self.assertEqual(json.loads(summ)["errors"], 4)


if __name__ == "__main__":
    unittest.main()
