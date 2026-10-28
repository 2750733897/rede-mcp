# -*- coding: utf-8 -*-
"""redb_shim.py —— ReDe 调试的透明 MI 中间人（方案 D）

背景
----
ReDe 的真实调试链路是：

    ReDe IDE --MI(管道)--> redb.exe --RSP(UDP 30253)--> IDE 内置代理 --TCF--> 板上 agent(1534)

目标板只认 TCF，redb 只会 RSP，两者之间的桥由 IDE 提供，且**代理与 agent 都是单客户端**。
所以 AI 不能另起炉灶，只能在 IDE 起好的会话里"替人操作、替人收信息"。

做法
----
由 IDE 启动本中间人（把 .launch 里的 DEBUG_NAME 指向本程序打包出的 exe），本程序：
  1. 原样启动真正的 redb.exe，并双向转发 MI 流量 —— IDE 的界面照常工作；
  2. 把每条 MI 记录以 JSONL 落盘 —— AI 读文件即可"接收信息"；
  3. 开一个本地 JSON 控制口 —— AI 写入 MI 命令即可"代替人操作"。

用法
----
    redb_shim.exe --interpreter=mi2 --nx          # IDE 就是这样调用 redb 的

环境变量
--------
    REDB_REAL    真正的 redb 路径（默认 <本exe同目录>/redb.exe 的同名真身 redb_real.exe）
    REDB_SHIM_LOG  日志路径（默认与真身同目录的 rede_mi_session.jsonl）
    REDB_SHIM_PORT 控制口端口（默认 30265）
    REDB_SHIM_CWD  子进程工作目录（默认继承 IDE 给的目录）
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

# 兜底：本机 ReDe 安装里的 redb（打包后没有环境变量也能找到）
BAKED_REAL = os.path.join(r"D:\guangxin7045\6.1.1-ARM", "tools", "debug", "arm", "redb.exe")

# 真身 redb：允许显式指定，否则找同目录 redb_real.exe，再退到同目录 redb.exe，最后用兜底
DEF_CANDIDATES = [
    os.environ.get("REDB_REAL", ""),
    os.path.join(HERE, "redb_real.exe"),
    os.path.join(HERE, "redb.exe"),
    BAKED_REAL,
]
REAL = next((p for p in DEF_CANDIDATES if p and os.path.exists(p)), "")

LOG_PATH = os.environ.get("REDB_SHIM_LOG") or os.path.join(HERE, "rede_mi_session.jsonl")
CTRL_PORT = int(os.environ.get("REDB_SHIM_PORT", "30265"))
CTRL_SPAN = 20                      # 30265..30284 依次尝试，避免多会话抢同一个端口
CTRL_DIR = os.path.join(HERE, "ctrl")   # 每个实例登记 {pid, port, started}，客户端据此选会话
BUILD = "2026-10-27.5"   # 打一次 exe 改一次，便于确认 IDE 拉起的是哪一版

_lock = threading.Lock()
_children: list = []
_records = 0
_recent: "deque" = __import__("collections").deque(maxlen=4000)
_port = [0]          # 实际绑定的控制口


def _log(direction: str, line: bytes) -> None:
    global _records
    try:
        text = line.decode("latin-1", "replace").rstrip("\r\n")
        if not text.strip():
            return
        with _lock:
            _records += 1
            rec = {"i": _records, "ts": round(time.time(), 3), "dir": direction, "mi": text}
            _recent.append(rec)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _tee_lines(buf: bytearray, direction: str) -> None:
    while True:
        idx = buf.find(b"\n")
        if idx < 0:
            return
        line = bytes(buf[:idx + 1])
        del buf[:idx + 1]
        _log(direction, line)


def _pump_stdin_to_child(child) -> None:
    buf = bytearray()
    while True:
        try:
            chunk = os.read(0, 65536)
        except Exception:
            chunk = b""
        if not chunk:
            break
        buf += chunk
        _tee_lines(buf, "ide>gdb")
        try:
            child.stdin.write(chunk)
            child.stdin.flush()
        except Exception:
            break
    # IDE 关了管道 = 会话结束。给它一点时间自己退出，否则强杀——
    # 否则会留下"控制口还占着、但没人用"的孤儿进程（真机踩到过）。
    _log("shim", b"stdin EOF, closing child")
    try:
        child.stdin.close()
    except Exception:
        pass

    def _reap():
        for _ in range(50):          # 最多等 5s
            if child.poll() is not None:
                return
            time.sleep(0.1)
        try:
            child.terminate()
            time.sleep(0.5)
            if child.poll() is None:
                child.kill()
        except Exception:
            pass

    threading.Thread(target=_reap, daemon=True).start()


def _pump_child_to_stdout(child) -> None:
    buf = bytearray()
    out = sys.stdout.buffer
    while True:
        try:
            chunk = child.stdout.read(65536)
        except Exception:
            chunk = b""
        if not chunk:
            break
        buf += chunk
        _tee_lines(buf, "gdb>ide")
        try:
            out.write(chunk)
            out.flush()
        except Exception:
            break


def _inject(cmd: str) -> dict:
    if not _children:
        return {"ok": False, "error": "子进程未就绪"}
    child = _children[0]
    try:
        with _lock:
            child.stdin.write((cmd.rstrip("\n") + "\n").encode("latin-1"))
            child.stdin.flush()
        _log("ai>gdb", cmd.encode("latin-1"))
        return {"ok": True, "sent": cmd}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tail(n: int) -> dict:
    with _lock:
        recs = list(_recent)[-max(1, n):]
    return {"ok": True, "total": _records, "records": recs}


def _inject_and_collect(cmd: str, wait: float) -> dict:
    """注入一条 MI 并只返回这条命令之后新产生的记录（AI 视角最有用）。"""
    with _lock:
        n0 = _records
    res = _inject(cmd)
    if not res.get("ok"):
        return res
    deadline = time.time() + max(0.2, wait)
    while time.time() < deadline:
        time.sleep(0.05)
    with _lock:
        new = [r for r in _recent if r["i"] > n0]
    return {"ok": True, "sent": cmd, "count": len(new), "records": new}


def _serve() -> None:
    """在 CTRL_PORT..CTRL_PORT+CTRL_SPAN 里挑第一个能独占绑定的端口。

    不能对 30265 用 SO_REUSEADDR：Windows 上允许多个 socket 绑同一端口，
    connect 会固定落到先绑的那个，多会话时 AI 就会操纵错的目标。
    """
    srv = socket.socket()
    bound = 0
    for port in range(CTRL_PORT, CTRL_PORT + CTRL_SPAN):
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen(8)
            bound = port
            break
        except OSError:
            srv.close()
            srv = socket.socket()
            continue
    if not bound:
        sys.stderr.write("shim: 控制口 %d..%d 全被占用\n"
                         % (CTRL_PORT, CTRL_PORT + CTRL_SPAN - 1))
        return
    _port[0] = bound
    _register(bound)
    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            return
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _register(port: int) -> None:
    """登记本实例，让客户端能在多会话里选中"最新那个"。"""
    try:
        os.makedirs(CTRL_DIR, exist_ok=True)
        rec = {"pid": os.getpid(), "port": port, "started": round(time.time(), 3),
               "build": BUILD, "real": REAL, "log": LOG_PATH, "cwd": os.getcwd()}
        path = os.path.join(CTRL_DIR, "ctl_%d.json" % os.getpid())
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, path)
        with open(os.path.join(CTRL_DIR, "latest.txt"), "w", encoding="utf-8") as fh:
            fh.write("%d %d\n" % (os.getpid(), port))
    except Exception:
        pass


def _unregister() -> None:
    try:
        os.remove(os.path.join(CTRL_DIR, "ctl_%d.json" % os.getpid()))
    except Exception:
        pass


def _handle(conn) -> None:
    try:
        conn.settimeout(5)
        data = b""
        while b"\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if not data.strip():
            conn.sendall(b'{"ok":false,"error":"empty request"}\n')
            return
        req = json.loads(data.decode("utf-8", "replace"))
        op = req.get("op", "state")
        if op == "mi":
            resp = _inject(str(req.get("cmd", "")))
        elif op == "mi_wait":
            resp = _inject_and_collect(str(req.get("cmd", "")), float(req.get("wait", 1.2)))
        elif op == "tail":
            resp = _tail(int(req.get("n", 50)))
        else:
            resp = {"ok": True, "build": BUILD, "pid": os.getpid(), "port": _port[0],
                    "real": REAL, "log": LOG_PATH, "records": _records,
                    "cwd": os.getcwd(), "args": " ".join(sys.argv[1:]),
                    "child_alive": bool(_children and _children[0].poll() is None)}
        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception as exc:
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parent_gone(pid: int) -> bool:
    """父进程（PyInstaller onefile 的 bootloader）是否已经消失。

    Windows 上进程退出后已记录的 ppid 不会变，所以不能靠 `os.getppid()` 判断，
    必须开 handle 看它是否已 signaled。
    """
    try:
        import ctypes
        from ctypes import wintypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return True                       # 进程不存在（含已退出）
        try:
            return k32.WaitForSingleObject(h, 0) != WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False                          # 判断不了就当还活着，别自己乱退


def _watch_parent() -> None:
    """孤儿自救。

    PyInstaller onefile 下真实结构是：
        redb_shim.exe(bootloader)  <-- IDE 只知道这个 pid
          └─ redb_shim.exe(python，真正监听控制口的那个)
               └─ redb.exe
    IDE 若只终止自己 CreateProcess 的那个 pid（bootloader），python 这一层会被
    遗留成孤儿，一直占着控制口不放——真机已踩到过（pid 43816 占着 30266）。
    发现父进程没了就自己走：先收 redb 子进程，再注销登记，最后退出。
    """
    ppid = os.getppid()
    if ppid <= 0:
        return
    if os.environ.get("REDB_SHIM_NO_WATCHDOG"):
        _log("shim", b"parent watchdog disabled by env")
        return
    while True:
        time.sleep(2.0)
        if not _parent_gone(ppid):
            continue
        _log("shim", ("parent(bootloader) %d gone, self-exit" % ppid).encode())
        try:
            if _children:
                _children[0].kill()
        except Exception:
            pass
        _unregister()
        os._exit(0)


def _is_probe(args) -> bool:
    """IDE 会先跑 `<gdb> --version` 探测版本；这种一次性调用不开控制口、不登记。"""
    for a in args:
        if a in ("--version", "-v", "-V") or a.startswith("--version"):
            return True
    return False


def main() -> int:
    if not REAL:
        sys.stderr.write("shim: 找不到真身 redb（设 REDB_REAL 或放 redb_real.exe 到同目录）\n")
        return 2
    args = sys.argv[1:] or ["--interpreter=mi2", "--nx"]
    probe = _is_probe(args)
    # cwd 默认继承（= IDE 给的工作目录）：IDE 可能用相对路径传 --gdbinit / -x，
    # 强行改成 redb 所在目录会让它解析错位。redb 依赖的 cygwin1.dll 走
    # “exe 所在目录优先 + PATH”，不依赖 cwd，所以继承是安全的。
    cwd = os.environ.get("REDB_SHIM_CWD") or None
    child = subprocess.Popen(
        [REAL] + args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=cwd, bufsize=0,
    )
    _children.append(child)
    _log("shim", ("start real=%s cwd=%s probe=%s args=%s"
                  % (REAL, cwd or os.getcwd(), probe, " ".join(args))).encode())

    if not probe:
        threading.Thread(target=_serve, daemon=True).start()
        threading.Thread(target=_watch_parent, daemon=True).start()
        time.sleep(0.05)
    threading.Thread(target=_pump_stdin_to_child, args=(child,), daemon=True).start()
    t = threading.Thread(target=_pump_child_to_stdout, args=(child,), daemon=True)
    t.start()
    t.join()
    try:
        code = child.wait(timeout=10)
    except Exception:
        try:
            child.kill()
        except Exception:
            pass
        code = 0
    _unregister()
    return code


if __name__ == "__main__":
    sys.exit(main())
