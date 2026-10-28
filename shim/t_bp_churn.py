# -*- coding: utf-8 -*-
"""断点反复增删压测：在**真实会话**上跑，验证 MI 记账正确 + IDE 不被搞乱。

只动 UserInit（本次会话已执行过，不会再命中），所以不会让目标意外停机。
用法：python t_bp_churn.py [轮数]
"""
import json
import re
import socket
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import takeover as T   # 复用发现/注入/容错逻辑


def bp_table():
    """返回 (条数, 断点号列表, 是否含 UserInit)"""
    r = T.mi_robust("-break-list", 2.5)
    blob = " ".join(x["mi"] for x in r.get("records", []))
    m = re.search(r'nr_rows="(\d+)"', blob)
    rows = int(m.group(1)) if m else -1
    nums = re.findall(r'number="(\d+)"', blob)
    return rows, nums, ("UserInit" in blob)


def alive():
    """会话还活着？用一次轻量查询验。"""
    try:
        r = T.mi_robust("-thread-info", 2.0)
        return any("^done" in x["mi"] for x in r.get("records", []))
    except SystemExit:
        return False
    except Exception:
        return False


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    st = T.call({"op": "state"})
    print("目标会话: pid=%s port=%s build=%s records=%s"
          % (st.get("pid"), st.get("port"), st.get("build"), st.get("records")))
    rows0, nums0, has = bp_table()
    print("初始断点表: %d 条 %s 含UserInit=%s" % (rows0, nums0, has))
    baseline = rows0

    ok_all = True
    for i in range(1, rounds + 1):
        print("\n--- 第 %d 轮 ---" % i)
        # 1) 插入
        r = T.mi_robust("-break-insert UserInit", 2.5)
        blob = " ".join(x["mi"] for x in r.get("records", []))
        m = re.search(r'number="(\d+)"', blob)
        num = m.group(1) if m else None
        rows, nums, has = bp_table()
        print("  插入 -> bkpt=%s 表 %d 条 %s contains=%s" % (num, rows, nums, has))
        if not num or not has:
            print("  !! 插入失败"); ok_all = False; break

        # 2) 禁用 / 启用
        for cmd, want in (("-break-disable %s" % num, "disabled"),
                          ("-break-enable %s" % num, "enabled")):
            rr = T.mi_robust(cmd, 2.0)
            b = " ".join(x["mi"] for x in rr.get("records", []))
            good = ("^done" in b)
            print("  %-22s -> %s" % (cmd, "ok" if good else "FAIL " + b[:80]))
            ok_all &= good

        # 3) 删除
        rr = T.mi_robust("-break-delete %s" % num, 2.0)
        b = " ".join(x["mi"] for x in rr.get("records", []))
        rows, nums, has = bp_table()
        # 注意：基线上那条断点本身就可能是 UserInit，所以只校验"回到基线 + 新建的号消失"
        good = ("^done" in b) and (rows == baseline) and (num not in nums)
        print("  删除 -> 表 %d 条 %s 回到基线=%s 新建号已消失=%s"
              % (rows, nums, rows == baseline, num not in nums))
        if not good:
            print("  !! 删除后状态不符"); ok_all = False

        if not alive():
            print("  !! 会话挂了"); ok_all = False; break

    print("\n== 结论 ==")
    print("  压测 %d 轮，%s；会话%s" % (rounds, "全通过" if ok_all else "有失败",
                                    "仍在线" if alive() else "已失联"))
    rows, nums, has = bp_table()
    print("  终态断点表: %d 条 %s" % (rows, nums))


if __name__ == "__main__":
    main()
