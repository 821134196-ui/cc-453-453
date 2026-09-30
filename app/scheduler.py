# -*- coding: utf-8 -*-
"""进程内定时任务：周期性检查到点批次生效、促销到期自动恢复。"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from . import db, services


class Scheduler:
    def __init__(self, interval_seconds: float = 3.0, adapter=None, verbose: bool = True):
        self.interval = interval_seconds
        self.adapter = adapter or services.ADAPTER
        self.verbose = verbose
        self._stop = threading.Event()
        self._thread = None
        self.tick_count = 0

    def tick(self, ref: datetime | None = None) -> dict:
        """执行一轮：到点生效 + 到期恢复。线程与 Web 请求共用同一把 SQLite 之外的进程锁。"""
        ref = ref or datetime.now()
        conn = db.get_conn()
        try:
            activated = services.activate_due_batches(conn, ref, self.adapter)
            restored = services.expire_promotions(conn, ref, self.adapter)
            conn.commit()
        finally:
            conn.close()
        self.tick_count += 1
        if self.verbose and (activated or restored):
            print(f"[调度 {ref.strftime('%H:%M:%S')}] 生效批次 {activated or '无'}，"
                  f"到期恢复 {[(x['code'], x['restore_to']) for x in restored] or '无'}")
        return {"activated": activated, "restored": restored, "at": ref.strftime("%H:%M:%S")}

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:  # 调度不能因单轮异常退出
                print("[调度] 轮询异常:", e)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="esl-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
