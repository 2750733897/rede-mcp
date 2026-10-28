# -*- coding: utf-8 -*-
"""在 IDE 已经用 hello_ai.launch 起了调试之后，由 AI/人通过 shim 控制口接管。

不依赖 MCP 会话。控制口从 shim 自己登记的实例表里发现：
    <shim>/bin/ctrl/ctl_<pid>.json  {"pid","port","started","build",...}
选 started 最新、且真的能连上的那个。也支持：
    --pid 38108        指定实例
    REDB_SHIM_PORT=30266   强制端口（跳过发现）

用法：
    python takeover.py                 # 只体检：状态 + 最近流水
    python takeover.py list            # 列出所有登记过的 shim 实例
    python takeover.py tail 60         # 看最近 60 条 MI
    python takeover.py mi "-info-breakpoints"
    python takeover.py bp UserInit     # 下断点
    python takeover.py go              # 继续，并**轮询等到真正停下**（最多 30s）
    python takeover.py step            # 单步，同样等到停下
    python takeover.py wait 20         # 只听不发：最多听 20s，等目标自己停下
    python takeover.py interrupt       # 等价 IDE 暂停按钮（目标在跑时读不了数据）
    python takeover.py demo            # 断点表 -> 栈 -> 局部量 -> 任务 -> PC/SP/LR
"""
import glob
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CTRL_DIR = os.path.join(HERE, "bin", "ctrl")
DEFAULT_PORT = 30265

_PICKED = {"pid": None, "port": None}
_NOTICED = {"fallback": False}


def _try_port(port, timeout=0.6):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def instances():
    """返回登记过的实例列表（按 started 倒序），并顺手清理已死的登记。"""
    out = []
    for path in glob.glob(os.path.join(CTRL_DIR, "ctl_*.json")):
        try:
            rec = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        rec["_path"] = path
        out.append(rec)
    out.sort(key=lambda r: r.get("started", 0), reverse=True)
    live = []
    for rec in out:
        if _try_port(rec.get("port", 0)):
            live.append(rec)
        else:
            try:
                os.remove(rec["_path"])
            except Exception:
                pass
    return live


def discover(force_pid=None):
    env_port = os.environ.get("REDB_SHIM_PORT")
    if env_port:
        return {"pid": None, "port": int(env_port), "build": "(env)"}
    live = instances()
    if force_pid is not None:
        for rec in live:
            if rec.get("pid") == force_pid:
                return rec
        print("!! 没找到 pid=%s 的在线 shim，现有：%s" % (force_pid, [r.get("pid") for r in live]))
        sys.exit(3)
    if not live:
        # 兼容旧版 exe（不写 ctl 登记文件）：直接试基址端口
        if _try_port(DEFAULT_PORT):
            if not _NOTICED["fallback"]:
                print("(没有实例登记文件，退回默认端口 %d —— 多半是旧版 shim)" % DEFAULT_PORT)
                _NOTICED["fallback"] = True
            return {"pid": None, "port": DEFAULT_PORT, "build": "(no-ctl-file)"}
        print("!! 没有任何在用控制口的 shim 实例：IDE 还没用 hello_ai.launch 起调试，"
              "或 shim 没被拉起。")
        sys.exit(3)
    if len(live) > 1:
        print("(发现 %d 个会话，取最新 pid=%s port=%s；要指定用 --pid)"
              % (len(live), live[0]["pid"], live[0]["port"]))
    return live[0]


def call(req, timeout=45.0):
    rec = discover(_PICKED["pid"])
    _PICKED["port"] = rec["port"]
    _PICKED["pid"] = rec.get("pid")
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", rec["port"]))
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (socket.timeout, TimeoutError):
        print("!! 控制口 %.0fs 无应答：shim 正在等 redb（目标可能在跑），稍后重试" % timeout)
        sys.exit(4)
    except Exception as exc:
        print("!! 连接 %s 失败：%s" % (rec["port"], exc))
        sys.exit(3)
    finally:
        s.close()
    if not buf.strip():
        print("!! 控制口无应答内容")
        sys.exit(4)
    return json.loads(buf.decode("utf-8", "replace"))


def show(label, resp, width=200):
    print("\n--- %s ---" % label)
    if not resp.get("ok"):
        print("  FAIL:", resp.get("error"))
        return
    for r in resp.get("records", []):
        line = r["mi"]
        print("  [%s] %s" % (r["dir"], line if len(line) <= width else line[:width] + "…"))


# 判定"目标停下来了"的 MI 记录；顺带把明显的失败也当终止条件
_STOP_MARKS = ('*stopped', '=thread-group-exited', '^error')
_BAD_THREAD = "Invalid selected thread"


def target_stopped():
    """有没有任何一个线程处于 stopped。全在 running 就说明目标在跑。"""
    r = mi_robust("-thread-info", 2.0)
    blob = " ".join(x["mi"] for x in r.get("records", []))
    return 'state="stopped"' in blob


def interrupt(timeout=15.0):
    """等价于 IDE 的暂停按钮：让在跑的目标停下来。"""
    print("--- 暂停目标（等价 IDE 暂停按钮） ---")
    ok, rec = run_until_stopped("-exec-interrupt", timeout)
    if not ok:
        show("-thread-info", mi_robust("-thread-info", 2.0), width=300)
        return False
    return target_stopped()


def ensure_stopped():
    """读数据前先确保目标停下；本来就在跑就暂停一次，并明确告知。"""
    if target_stopped():
        return True
    print("(目标正在运行，先暂停再读；读完可用 takeover.py go 让它继续)")
    return interrupt()


def _first_thread():
    """取一个可用线程 id：优先 current-thread-id，否则第一个线程。"""
    r = call({"op": "mi_wait", "cmd": "-thread-info", "wait": 1.5})
    for rec in r.get("records", []):
        mi = rec["mi"]
        if "^done,threads=" not in mi:
            continue
        import re
        m = re.search(r'current-thread-id="(\d+)"', mi)
        if m:
            return m.group(1)
        m = re.search(r'threads=\[\{id="(\d+)"', mi)
        if m:
            return m.group(1)
    return None


def mi_robust(cmd, wait=2.0):
    """注入 MI，遇到 "Invalid selected thread" 自动选线程后重试一次。

    AI 注入的命令没有 token、也不带 --thread，靠 GDB 的"当前线程"；
    目标刚 run 过或线程表变了，当前线程会失效。真板同样会遇到。
    """
    r = call({"op": "mi_wait", "cmd": cmd, "wait": wait})
    blob = json.dumps(r, ensure_ascii=False)
    if _BAD_THREAD not in blob:
        return r
    tid = _first_thread()
    if not tid:
        return r
    print("  (选中线程失效，改选 thread %s 后重试)" % tid)
    call({"op": "mi_wait", "cmd": "-thread-select %s" % tid, "wait": 1.0})
    return call({"op": "mi_wait", "cmd": cmd, "wait": wait})


def run_until_stopped(cmd, timeout=30.0, quiet=False):
    """发一条运行类命令，然后**轮询到真正停下**为止，而不是盲等固定秒数。

    只用 state / mi_wait / tail 三个 op，所以对旧版 shim 也兼容。
    """
    st = call({"op": "state"})
    n0 = int(st.get("records") or 0)
    call({"op": "mi_wait", "cmd": cmd, "wait": 0.15})

    seen = set()
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        tail = call({"op": "tail", "n": 800})
        fresh = [r for r in tail.get("records", []) if r["i"] > n0 and r["i"] not in seen]
        for r in fresh:
            seen.add(r["i"])
            if not quiet:
                print("  [%s] %s" % (r["dir"], r["mi"][:220]))
        for r in fresh:
            if any(m in r["mi"] for m in _STOP_MARKS):
                return True, r
        time.sleep(0.25)
    return False, None


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--pid")]
    if "--pid" in sys.argv:
        i = sys.argv.index("--pid")
        try:
            _PICKED["pid"] = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            print("用法: takeover.py --pid <shim进程号> ...")
            sys.exit(2)
        argv = [a for a in argv if a != str(_PICKED["pid"])]

    cmd = argv[0] if argv else "status"

    if cmd == "list":
        live = instances()
        print("在线 shim 实例（按启动时间倒序）:")
        for r in live:
            print("  pid=%-6s port=%-6s build=%-14s started=%s cwd=%s"
                  % (r.get("pid"), r.get("port"), r.get("build"),
                     r.get("started"), r.get("cwd")))
        if not live:
            print("  （无）")

    elif cmd == "status":
        st = call({"op": "state"})
        print("接管目标: pid=%s port=%s" % (_PICKED["pid"], _PICKED["port"]))
        for k in ("build", "child_alive", "records", "args", "cwd", "real", "log"):
            if k in st:
                print("  %-12s %s" % (k, st[k]))
        show("最近 20 条 MI 流水", call({"op": "tail", "n": 20}))

    elif cmd == "tail":
        n = int(argv[1]) if len(argv) > 1 else 40
        show("最近 %d 条" % n, call({"op": "tail", "n": n}))

    elif cmd == "mi":
        if len(argv) < 2:
            print('用法: takeover.py mi "<MI 命令>"')
            sys.exit(2)
        show(argv[1], mi_robust(argv[1], 3.0), width=400)

    elif cmd == "bp":
        if len(argv) < 2:
            print("用法: takeover.py bp <symbol|file:line>")
            sys.exit(2)
        show("下断点 %s" % argv[1],
             call({"op": "mi_wait", "cmd": "-break-insert %s" % argv[1], "wait": 3.0}), width=400)

    elif cmd in ("go", "step", "wait"):
        if cmd == "wait":
            print("--- 只听不发：等目标停下（最多 %s 秒） ---"
                  % (argv[1] if len(argv) > 1 else "30"))
            st = call({"op": "state"})
            n0 = int(st.get("records") or 0)
            deadline = time.time() + (float(argv[1]) if len(argv) > 1 else 30.0)
            seen = set()
            while time.time() < deadline:
                tail = call({"op": "tail", "n": 800})
                for r in tail.get("records", []):
                    if r["i"] > n0 and r["i"] not in seen:
                        seen.add(r["i"])
                        print("  [%s] %s" % (r["dir"], r["mi"][:220]))
                        if any(m in r["mi"] for m in _STOP_MARKS):
                            return
                time.sleep(0.25)
            print("  (超时，目标仍在运行)")
            return

        c = "-exec-continue" if cmd == "go" else "-exec-next"
        to = float(argv[1]) if len(argv) > 1 else 30.0
        print("--- %s，等停下（最多 %.0fs） ---" % ("继续" if cmd == "go" else "单步", to))
        ok, rec = run_until_stopped(c, to)
        if ok and rec and "^error" in rec["mi"] and "not being run" in rec["mi"]:
            # 模拟器后端 / 会话刚建时还没有 inferior
            print("  (目标尚未启动，改用 -exec-run)")
            ok, rec = run_until_stopped("-exec-run", to)
        if not ok:
            print("  (%.0fs 内没等到 *stopped；目标应仍在跑，可用 takeover.py interrupt 暂停)" % to)

    elif cmd == "interrupt":
        ok = interrupt()
        print("  结果：%s" % ("已停住" if ok else "仍在运行 / 未能停下"))

    elif cmd == "demo":
        st = call({"op": "state"})
        print("== 1) 体检 == target pid=%s port=%s build=%s child_alive=%s records=%s"
              % (_PICKED["pid"], _PICKED["port"], st.get("build"),
                 st.get("child_alive"), st.get("records")))
        if not ensure_stopped():
            print("!! 目标停不下来，后续读取跳过")
            return
        print("== 2) 断点表（IDE 已经设了什么） ==")
        show("-break-list", mi_robust("-break-list", 2.0), width=160)
        print("== 3) 当前停机位置 ==")
        show("-stack-list-frames 0 3", mi_robust("-stack-list-frames 0 3", 2.0), width=400)
        print("== 4) 当前帧局部变量 ==")
        show("-stack-list-variables --simple-values",
             mi_robust("-stack-list-variables --simple-values", 2.0), width=400)
        print("== 5) 任务/线程 ==")
        show("-thread-info", mi_robust("-thread-info", 2.0), width=400)
        print("== 6) PC / SP / LR ==")
        for reg in ("$pc", "$sp", "$lr"):
            show(reg, mi_robust('-data-evaluate-expression "%s"' % reg, 2.0), width=200)
    else:
        print("未知子命令：%s（可用 status/list/tail/mi/bp/go/step/demo）" % cmd)
        sys.exit(2)


if __name__ == "__main__":
    main()
