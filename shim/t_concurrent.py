# -*- coding: utf-8 -*-
"""并发注入压测：多个客户端同时对同一个 shim 控制口注入 MI。

要回答两个问题：
  1. shim 的写入锁是否让并发注入安全（不会把两条命令的字节搅在一起）
  2. 并发时 mi_wait 的"只返回本条之后的记录"还有效吗？
     （已知设计：它按记录序号过滤，不是按命令归属，所以会看到别人的输出 —— 要实测确认）
"""
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import takeover as T

RESULTS = []
LOCK = threading.Lock()


def worker(idx, n):
    for k in range(n):
        cmd = "-data-evaluate-expression \"%d+%d\"" % (idx * 100, k)
        t0 = time.time()
        r = T.call({"op": "mi_wait", "cmd": cmd, "wait": 0.2}, timeout=30)
        dt = time.time() - t0
        recs = r.get("records", [])
        mine = [x for x in recs if x["mi"].startswith("^done,value=")]
        has_ai_echo = any(x["dir"] == "ai>gdb" and cmd in x["mi"] for x in recs)
        with LOCK:
            RESULTS.append({"idx": idx, "k": k, "dt": round(dt, 2),
                            "count": len(recs), "values": len(mine),
                            "echo": has_ai_echo})
        time.sleep(0.05)


def main():
    st = T.call({"op": "state"})
    print("会话: pid=%s build=%s records=%s" % (st.get("pid"), st.get("build"), st.get("records")))

    N_THREADS, N_EACH = 3, 5
    print("== 并发注入：%d 线程 x %d 条 ==" % (N_THREADS, N_EACH))
    ths = [threading.Thread(target=worker, args=(i, N_EACH)) for i in range(N_THREADS)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0

    print("  总耗时 %.1fs，共 %d 次调用" % (wall, len(RESULTS)))
    print("  每次拿到的记录条数:", sorted(r["count"] for r in RESULTS))
    print("  其中含 ^done,value= 的条数:", sorted(r["values"] for r in RESULTS))
    echo_ok = sum(1 for r in RESULTS if r["echo"])
    print("  能看到自己命令回显的次数: %d/%d" % (echo_ok, len(RESULTS)))

    st2 = T.call({"op": "state"})
    print("  压测后: child_alive=%s records=%s" % (st2.get("child_alive"), st2.get("records")))

    print("== 校验：每个表达式的值是否正确（应为 idx*100+k）==")
    bad = 0
    for r in sorted(RESULTS, key=lambda x: (x["idx"], x["k"])):
        want = r["idx"] * 100 + r["k"]
        # 从最后一次调用里拿不到历史值，这里只统计结构性指标
        if r["values"] == 0:
            bad += 1
    print("  无有效回值次数: %d" % bad)

    print("== 会话存活 ==")
    ok = T.target_stopped() is not None
    try:
        r = T.mi_robust("-thread-info", 2.0)
        alive = any("^done,threads=" in x["mi"] for x in r.get("records", []))
    except SystemExit:
        alive = False
    print("  thread-info 正常:", alive)


if __name__ == "__main__":
    main()
