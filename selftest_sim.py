# -*- coding: utf-8 -*-
"""对 rede_mcp_server 做端到端自检：模拟器加载 -> 断点 -> 单步 -> 看变量/内存/寄存器/反汇编。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rede_mcp_server as R

HERE = os.path.dirname(os.path.abspath(__file__))
ELF = os.path.join(HERE, "simtest", "demo.elf")


def show(title, raw, limit=1200):
    print("\n" + "=" * 70)
    print("## " + title)
    try:
        obj = json.loads(raw)
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = str(raw)
    print(text[:limit])


def main():
    show("1) rede_sim 加载", R.rede_sim(ELF))
    show("2) 断点 compute", R.rede_break_insert("compute"))
    show("3) continue", R.rede_resume("continue", timeout_s=8))
    show("4) backtrace", R.rede_backtrace())
    show("5) locals", R.rede_locals())
    show("6) eval g_counter", R.rede_eval("g_counter"))
    show("7) eval p.x", R.rede_eval("p.x"))

    for i in range(3):
        print("\n-- next x%d" % (i + 1))
        r = json.loads(R.rede_resume("next", timeout_s=6))
        fr = (r.get("stop") or {}).get("frame", {})
        print("   at %s:%s in %s" % (fr.get("file"), fr.get("line"), fr.get("func")))

    show("8) locals @compute", R.rede_locals())
    show("9) args @compute", R.rede_args())
    show("10) eval s", R.rede_eval("s"))
    show("11) struct point", R.rede_struct("p"))

    show("12) registers (截断)", R.rede_registers(), 600)

    ga = json.loads(R.rede_eval("(int)&g_array"))
    addr = ga.get("value", "0x18100")
    n = int(addr, 16) if addr.lower().startswith("0x") else int(addr)
    show("13) &g_array = %s，读 16 字节" % addr, R.rede_read_memory("0x%x" % n, 16))
    show("14) 反汇编 compute", R.rede_disassemble("compute", count=6))
    show("15) break-list", R.rede_break_list())
    show("16) status", R.rede_status(), 800)
    show("17) console: info frame", R.rede_console("info frame"))

    print("\n" + "=" * 70)
    print("全部工具调用完成，无异常")


if __name__ == "__main__":
    main()
