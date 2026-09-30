"""进程内定时任务：到点下发待生效批次；促销到期自动发起恢复批次。

演示环境 tick 间隔很短（默认 3s），方便一条启动命令看到完整链路。
"""
from __future__ import annotations

import logging
import threading
import time

from .adapter import EslAdapter
from .db import connect
from .services import create_due_restores, dispatch_due

log = logging.getLogger("scheduler")


class Scheduler(threading.Thread):
    def __init__(self, db_path: str | None = None, interval: float = 3.0,
                 run_immediately: bool = False):
        super().__init__(daemon=True, name="esl-scheduler")
        self.db_path = db_path
        self.interval = interval
        self._stop = threading.Event()
        self.run_immediately = run_immediately
        self.history: list[str] = []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        conn = connect(self.db_path)
        adapter = EslAdapter(conn)
        if self.run_immediately:
            self.tick(conn, adapter)
        while not self._stop.wait(self.interval):
            try:
                self.tick(conn, adapter)
            except Exception:  # 定时任务不能因一次异常死掉
                log.exception("调度 tick 失败")

    def tick(self, conn=None, adapter=None) -> dict:
        conn = conn or connect(self.db_path)
        adapter = adapter or EslAdapter(conn)
        restored = create_due_restores(conn)
        for rid in restored:
            self._note(f"促销到期，自动生成恢复批次 #{rid}")
        dispatched = dispatch_due(conn, adapter)
        for bid in dispatched:
            self._note(f"批次 #{bid} 到点执行下发")
        return {"restored": restored, "dispatched": dispatched}

    def _note(self, msg: str) -> None:
        log.info(msg)
        self.history.append(msg)
