# -*- coding: utf-8 -*-
"""第二轮：在 \\x03\\x01 自定界格式下，试版本握手与命令参数终止符。"""

import socket

HOST, PORT = "192.168.0.100", 1534
END = b"\x03\x01"


def sess_send(payload, wait=1.8, label=""):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((HOST, PORT))
        try:
            s.recv(4096)          # 丢掉问候
        except socket.timeout:
            pass
        s.sendall(payload)
        s.settimeout(wait)
        buf = b""
        try:
            while len(buf) < 8192:
                d = s.recv(4096)
                if not d:
                    break
                buf += d
        except socket.timeout:
            pass
        # 只保留非 peerHeartBeat 的消息
        msgs = buf.split(END)
        interesting = []
        for m in msgs:
            if not m:
                continue
            if b"peerHeartBeat" in m:
                continue
            interesting.append(m)
        print("[%-34s] total=%3d  non-heartbeat=%d" % (label, len(buf), len(interesting)))
        for m in interesting[:4]:
            print("      ->", m[:180])
        return interesting
    except Exception as exc:
        print("[%-34s] ERROR %s" % (label, exc))
        return []
    finally:
        s.close()


def main():
    print("=== 1. 版本握手 V（多种写法）===")
    sess_send(b"V" + b'"1.6"\x00' + b'"BIG_ENDIAN"\x00' + END, label="V 1.6/BIG_ENDIAN")
    sess_send(b"V" + b'"1.6"\x00' + b'"LITTLE_ENDIAN"\x00' + END, label="V 1.6/LITTLE_ENDIAN")
    sess_send(b"V" + b'\x00' + b'\x00' + END, label="V 空版本")

    print("\n=== 2. 命令：token 有无 + args 终止符 ===")
    for label, p in [
        ("有token 1, 无空args", b"C" + b"1\x00" + b"Locator\x00" + b"getPeers\x00" + END),
        ("有token 1, 补空args\\0", b"C" + b"1\x00" + b"Locator\x00" + b"getPeers\x00" + b"\x00" + END),
        ("空token, 无空args", b"C" + b"\x00" + b"Locator\x00" + b"getPeers\x00" + END),
        ("空token, 补空args\\0", b"C" + b"\x00" + b"Locator\x00" + b"getPeers\x00" + b"\x00" + END),
        ("token 1, hello 命令", b"C" + b"1\x00" + b"Locator\x00" + b"Hello\x00" + b"\x00" + END),
    ]:
        sess_send(p, label=label)

    print("\n=== 3. 先 V 后 C（同一连接）===")
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((HOST, PORT))
        s.recv(4096)
        s.sendall(b"V" + b'"1.6"\x00' + b'"BIG_ENDIAN"\x00' + END)
        s.settimeout(1.0)
        try:
            print("  V 之后:", s.recv(512)[:120])
        except socket.timeout:
            print("  V 之后: 无应答")
        s.sendall(b"C" + b"1\x00" + b"Locator\x00" + b"getPeers\x00" + b"\x00" + END)
        s.settimeout(1.8)
        buf = b""
        try:
            while True:
                d = s.recv(4096)
                if not d:
                    break
                buf += d
        except socket.timeout:
            pass
        print("  C 之后 %d 字节:" % len(buf))
        for m in buf.split(END)[:5]:
            if m and b"peerHeartBeat" not in m:
                print("      ->", m[:180])
    finally:
        s.close()


if __name__ == "__main__":
    main()
