"""模拟电子价签网关适配器。

真实环境中这里走 ESL 基站 MQTT/HTTP；演示环境按标签状态返回回执：
- 标签 online=0          -> 离线
- 标签 fail_next>0       -> 模拟一次失败（随后自动清零，重试即成功）
- 其余                   -> 成功回执
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import results as R


class EslAdapter:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def push(self, tag_id: int, version: int, price: float) -> dict:
        """下发一帧到价签，返回 {ok, code, message}。"""
        tag = self.conn.execute(
            "SELECT * FROM tags WHERE id=?", (tag_id,)
        ).fetchone()
        if tag is None:
            return {"ok": False, "code": R.ACK_SIMULATED_FAIL, "message": "价签不存在"}
        if not tag["online"]:
            return {"ok": False, "code": R.ACK_TAG_OFFLINE,
                    "message": f"价签 {tag['tag_no']} 离线，基站无响应"}
        if tag["fail_next"] > 0:
            self.conn.execute(
                "UPDATE tags SET fail_next = fail_next - 1 WHERE id=?", (tag_id,))
            self.conn.commit()
            return {"ok": False, "code": R.ACK_SIMULATED_FAIL,
                    "message": f"价签 {tag['tag_no']} 通信干扰，下发帧丢失"}
        # 成功：真实设备在这一步刷屏，这里只返回回执
        return {"ok": True, "code": R.ACK_SUCCESS,
                "message": f"价签 {tag['tag_no']} 已显示 ￥{price:.2f}（v{version}）"}

    def set_offline(self, tag_no: str, offline: bool = True) -> None:
        self.conn.execute("UPDATE tags SET online=? WHERE tag_no=?",
                          (0 if offline else 1, tag_no))
        self.conn.commit()

    def fail_once(self, tag_no: str, times: int = 1) -> None:
        self.conn.execute("UPDATE tags SET fail_next=? WHERE tag_no=?",
                          (times, tag_no))
        self.conn.commit()
