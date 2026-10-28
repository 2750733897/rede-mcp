# -*- coding: utf-8 -*-
"""验证 shim：一边扮演 IDE 发 MI，一边扮演 AI 通过控制口收/发。

不需要板子也不需要 IDE —— 用 redb 内置模拟器跑，只验证"中间人"机制本身。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.dirname(HERE)
SHIM = os.path.join(HERE, "redb_shim.py")
REAL = os.path.join(r"D:\guangxin7045\6.1.1-ARM\tools\debug\arm", "redb.exe")
ELF = os.path.join(MCP, "simtest", "demo.elf")
LOG = os.path.join(HERE, "test_session.jsonl")
PORT = 30266


def ctrl(req, timeout=5.0):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect(("127.0.0.1", PORT))
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        d = s.recv(65536)
        if not d:
            break
        buf += d
    s.close()
    return json.loads(buf.decode("utf-8", "replace"))


def main():
    if os.path.exists(LOG):
        os.remove(LOG)
    env = dict(os.environ)
    env["REDB_REAL"] = REAL
    env["REDB_SHIM_LOG"] = LOG
    env["REDB_SHIM_PORT"] = str(PORT)

    proc = subprocess.Popen(
        [sys.executable, SHIM, "--interpreter=mi2", "--nx"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, bufsize=0,
    )

    ide_out = []
    stop = threading.Event()

    def read_ide():
        while not stop.is_set():
            d = proc.stdout.read(4096)
            if not d:
                break
            ide_out.append(d)

    threading.Thread(target=read_ide, daemon=True).start()
    time.sleep(1.5)

    def send(cmd):
        proc.stdin.write((cmd + "\n").encode())
        proc.stdin.flush()
        time.sleep(0.6)

    print("== 1) IDE 侧发命令（应被转发给真 redb）==")
    send("-gdb-set confirm off")
    send("-file-exec-and-symbols %s" % ELF.replace("\\", "/"))
    send("-target-select sim --memory-region 0x0,0x1000000")
    send("-target-download")
    time.sleep(0.8)

    print("== 2) AI 侧通过控制口读状态 ==")
    st = ctrl({"op": "state"})
    print("   ", json.dumps(st, ensure_ascii=False))

    print("== 3) AI 侧注入断点（代替人操作）==")
    print("   ", ctrl({"op": "mi", "cmd": "-break-insert compute"}))
    time.sleep(0.8)

    print("== 4) AI 侧注入运行 ==")
    print("   ", ctrl({"op": "mi", "cmd": "-exec-run"}))
    time.sleep(2.0)

    print("== 5) AI 侧读最近记录（代替人接收信息）==")
    tail = ctrl({"op": "tail", "n": 14})
    for r in tail.get("records", []):
        line = r["mi"]
        print("    [%s] %s" % (r["dir"], line[:110]))

    print("\n== 6) 日志文件 ==")
    print("   ", LOG, os.path.getsize(LOG) if os.path.exists(LOG) else "缺失", "bytes")

    print("\n== 7) IDE 侧实际收到的字节数 ==")
    print("   ", sum(len(x) for x in ide_out), "bytes")

    stop.set()
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    print("\n完成")


if __name__ == "__main__":
    main()
