# -*- coding: utf-8 -*-
"""多会话并发验证（真 exe，不改动现有 IDE 会话）。

当前真实会话占着 30265，这里再起两个 exe 实例，应依次拿到 30266 / 30267，
并各自登记 ctl 文件；`takeover.py list` 应列出 3 个，"选最新"应选中最后起的那个。
结束后把它们杀掉，列表应回落到只剩真实会话。
"""
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "bin", "redb_shim.exe")
PY = sys.executable
CTRL = os.path.join(HERE, "bin", "ctrl")


def ctl_files():
    out = {}
    for p in glob.glob(os.path.join(CTRL, "ctl_*.json")):
        try:
            r = json.load(open(p, encoding="utf-8"))
            out[r["pid"]] = r
        except Exception:
            pass
    return out


def run(*args):
    r = subprocess.run([PY, os.path.join(HERE, "takeover.py")] + list(args),
                       capture_output=True, text=True, errors="replace")
    return (r.stdout + r.stderr).strip()


def main():
    print("== 0) 起之前 ==")
    before = ctl_files()
    print("   已有实例:", {k: v["port"] for k, v in before.items()})

    print("== 1) 再起 2 个 exe 实例 ==")
    procs = []
    for i in (1, 2):
        env = dict(os.environ)
        env["REDB_SHIM_LOG"] = os.path.join(HERE, "multi_%d.jsonl" % i)
        p = subprocess.Popen([EXE, "--interpreter", "mi2", "--nx"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env)
        procs.append(p)
        time.sleep(2.5)

    after = ctl_files()
    new = {k: v for k, v in after.items() if k not in before}
    print("   新实例:", {k: v["port"] for k, v in new.items()})
    ports = sorted(v["port"] for v in new.values())
    print("   端口应为 30266/30267 连续分配 ->", ports, "=>",
          "OK" if ports == [30266, 30267] else "异常")

    print("== 2) takeover.py list（应看到 3 个）==")
    print(run("list").replace("\n", "\n   "))

    print("== 3) status 应选中最新的那个（不是真实会话）==")
    out = run("status")
    first = [l for l in out.splitlines() if l.startswith("接管目标")]
    print("  ", first[0] if first else out[:120])

    print("== 4) 收尾：杀掉测试实例 ==")
    for p in procs:
        p.kill()
    time.sleep(2.0)
    final = ctl_files()
    print("   剩余实例:", {k: v["port"] for k, v in final.items()})
    print("   应只剩真实会话 ->", "OK" if set(final) == set(before) else "有残留")
    print("== 5) status 应回到真实会话 ==")
    out = run("status")
    first = [l for l in out.splitlines() if l.startswith("接管目标")]
    print("  ", first[0] if first else out[:120])


if __name__ == "__main__":
    main()
