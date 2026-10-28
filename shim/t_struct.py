# -*- coding: utf-8 -*-
"""真机验证 MCP 侧 5 个结构化工具（snapshot / eval / read_mem / disasm / break）。

在 IDE 已用 hello_ai.launch 起调试的前提下运行。
脚本结束时会把目标恢复运行，不留停止态。
"""

import json
import sys

import rede_mcp_server as R


def show(title, s, keys=None):
    print("\n== %s ==" % title)
    try:
        d = json.loads(s)
    except Exception:
        print("  (非 JSON)", s[:300])
        return {}
    if keys:
        d = {k: d.get(k) for k in keys if k in d}
    print(json.dumps(d, ensure_ascii=False, indent=2)[:1800])
    return d


def main():
    print("shim 目标:", json.dumps(json.loads(R.rede_shim_list()), ensure_ascii=False)[:200])

    # 1) snapshot（目标在跑时应自动暂停）
    snap = show("1) snapshot(depth=6)",
                R.rede_shim_snapshot(6),
                ["ok", "stopped", "note", "shim_pid", "shim_port", "build",
                 "thread", "frames", "locals", "locals_note", "regs", "breakpoints"])
    if not snap.get("ok"):
        print("!! snapshot 未成功，后续跳过")
        return 1

    # 2) eval
    show("2) eval  $pc", R.rede_shim_eval("$pc"))
    show("2b) eval $sp", R.rede_shim_eval("$sp"))

    # 3) disasm 从当前 pc 起 6 条
    d = show("3) disasm(pc, 6)", R.rede_shim_disasm("pc", 6))
    if d.get("insns"):
        print("   首条:", d["insns"][0])

    # 4) read_mem：读 UserInit 的机器码（基线断点就在那儿，肯定非零）
    bp_addr = None
    for b in (snap.get("breakpoints") or []):
        if b.get("func") == "UserInit":
            bp_addr = b.get("addr")
    show("4) read_mem(%s, 16) = UserInit 机器码" % (bp_addr or "0x0"),
         R.rede_shim_read_mem(bp_addr or "0x0", 16))

    # 5) break 临时断点 -> 立即删除
    b = show("5) break(UserInit, temporary=True)", R.rede_shim_break("UserInit", temporary=True))
    if b.get("ok") and b.get("number"):
        show("5b) 删除该断点", R.rede_shim_mi("-break-delete %s" % b["number"], 2.0),
             ["ok", "count"])

    # 6) 收尾：恢复运行，别把板子留在停止态
    print("\n== 6) 恢复运行 ==")
    r = json.loads(R.rede_shim_resume("continue", 5))
    print("   stopped=%s（false = 已恢复运行）" % r.get("stopped"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
