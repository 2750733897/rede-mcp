# -*- coding: utf-8 -*-
"""通过串口桥(localhost:9999)在板子 reworks> shell 上执行命令并回收输出。

注意：这是共享的串口控制台（可能同时有别人连着），写命令会出现在同一条流里。
只用于只读查询，改动类命令需人工确认。
"""

import re
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 9999
PROMPT = b"reworks>"

ANSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def clean(raw: bytes) -> str:
    raw = ANSI.sub(b"", raw)
    out = []
    for b in raw:
        if b == 8:          # backspace
            if out:
                out.pop()
        elif b in (13,):
            continue
        elif b < 32 and b not in (9, 10):
            continue
        else:
            out.append(chr(b))
    return "".join(out)


def drain(s, timeout=0.6):
    s.settimeout(timeout)
    buf = b""
    try:
        while True:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except Exception:
        pass
    return buf


def run(cmd, wait=2.5):
    s = socket.socket()
    s.settimeout(3)
    s.connect((HOST, PORT))
    drain(s, 0.8)                     # 吃掉 banner 与残留
    s.sendall(cmd.encode() + b"\r\n")
    buf = b""
    deadline = time.time() + wait
    while time.time() < deadline:
        buf += drain(s, 0.4)
        if buf.rstrip().endswith(PROMPT):
            break
    s.close()
    return clean(buf)


if __name__ == "__main__":
    cmds = sys.argv[1:] or ["ifconfig"]
    for c in cmds:
        print("=" * 60)
        print("$ " + c)
        print(run(c))
