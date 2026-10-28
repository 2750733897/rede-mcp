# -*- coding: utf-8 -*-
"""验证控制口"依次独占绑定 + 实例登记"逻辑。

基址用 30270（避开真实会话占用的 30265）：先占住 30270，再启 shim，
应自动升到 30271 并写出 ctl_<pid>.json。
"""
import glob
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(HERE, "redb_shim.py")
CTRL = os.path.join(HERE, "ctrl")
BASE = 30270


def hold(port):
    """独占占住端口，模拟"已有一个会话"。"""
    s = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", port))
    s.listen(4)
    return s


def alive(port):
    c = socket.socket()
    c.settimeout(0.8)
    try:
        c.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        c.close()


def ask(port):
    c = socket.socket()
    c.settimeout(3)
    c.connect(("127.0.0.1", port))
    c.sendall(b'{"op":"state"}\n')
    buf = b""
    while b"\n" not in buf:
        d = c.recv(65536)
        if not d:
            break
        buf += d
    c.close()
    return json.loads(buf.decode())


def main():
    print("== 1) 占住 %d，模拟已有会话 ==" % BASE)
    hog = hold(BASE)
    print("   %d 已被占用" % BASE)

    print("== 2) 启动源码版 shim（基址被占，应自动升到 %d） ==" % (BASE + 1))
    env = dict(os.environ)
    env["REDB_SHIM_PORT"] = str(BASE)
    env["REDB_SHIM_LOG"] = os.path.join(HERE, "esc_session.jsonl")
    p = subprocess.Popen([sys.executable, SHIM, "--interpreter", "mi2", "--nx"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, env=env)
    time.sleep(3.0)

    print("== 3) 登记文件 ==")
    for path in sorted(glob.glob(os.path.join(CTRL, "ctl_*.json"))):
        rec = json.load(open(path, encoding="utf-8"))
        print("   %-28s pid=%s port=%s build=%s"
              % (os.path.basename(path), rec.get("pid"), rec.get("port"), rec.get("build")))

    print("== 4) 端口探测 ==")
    for port in (BASE, BASE + 1):
        print("   %d: %s" % (port, "OPEN" if alive(port) else "closed"))

    print("== 5) 对 %d 发 state ==" % (BASE + 1))
    try:
        r = ask(BASE + 1)
        print("   pid=%s port=%s build=%s child_alive=%s args=%s"
              % (r.get("pid"), r.get("port"), r.get("build"),
                 r.get("child_alive"), r.get("args")))
    except Exception as exc:
        print("   FAIL %s: %s" % (type(exc).__name__, exc))

    print("== 6) 收尾 ==")
    p.kill()
    time.sleep(0.8)
    for path in glob.glob(os.path.join(CTRL, "ctl_*.json")):
        try:
            os.remove(path)
        except Exception:
            pass
    hog.close()
    time.sleep(0.3)
    print("   已清理；%d 现在 %s" % (BASE, "OPEN" if alive(BASE) else "closed"))


if __name__ == "__main__":
    main()
