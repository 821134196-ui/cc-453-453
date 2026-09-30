#!/usr/bin/env python3
"""一条命令启动：python3 run.py

动作：重置演示库 → 跑完整演示案例（成功/离线/恢复/回滚/抽检/校验拒绝）
     → 启动进程内定时任务 → 启动 Web 后台。

参数：
  --check       只跑自动化测试 + 演示，不启动服务
  --no-demo     保留现有数据库，不重置演示
  --port PORT   指定端口（默认 8000）
  --host HOST   指定地址（默认 127.0.0.1）
"""
from __future__ import annotations

import argparse
import sys
import time

from app.demo import run_demo
from app.scheduler import Scheduler
from app.server import serve


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="电子价签批量改价与回滚核验系统")
    p.add_argument("--check", action="store_true", help="运行自动化测试后退出")
    p.add_argument("--no-demo", action="store_true", help="不重置/重跑演示数据")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    if args.check:
        import unittest
        from tests import test_system
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(test_system)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    if not args.no_demo:
        run_demo(serve_after=True)

    sched = Scheduler(interval=3.0)
    sched.start()
    httpd = serve(args.host, args.port)
    print("定时任务已启动（每 3 秒检查生效/到期恢复）。Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭 …")
        sched.stop()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
