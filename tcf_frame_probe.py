# -*- coding: utf-8 -*-
"""对照实验：对板上 TCF agent(1534) 试不同帧格式，找出哪种能被应答。

只发 Locator 的只读命令，不碰 RunControl，不会影响板上运行。
"""

import socket
import struct

HOST, PORT = "192.168.0.100", 1534


def recv_all(s, timeout=2.0):
    s.settimeout(timeout)
    buf = b""
    try:
        while True:
            d = s.recv(4096)
            if not d:
                break
            buf += d
    except socket.timeout:
        pass
    return buf


def try_frame(name, payload):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((HOST, PORT))
        hello = b""
        try:
            hello = s.recv(4096)
        except socket.timeout:
            pass
        s.sendall(payload)
        resp = recv_all(s, 2.0)
        ok = bool(resp)
        print("[%-28s] len=%3d  resp=%s %r" % (
            name, len(payload), "YES" if ok else "no ", resp[:160]))
        return resp
    except Exception as exc:
        print("[%-28s] ERROR %s" % (name, exc))
        return b""
    finally:
        s.close()


def main():
    print("基线：连接后问候帧")
    s = socket.socket()
    s.settimeout(2.5)
    s.connect((HOST, PORT))
    g = recv_all(s, 1.5)
    s.close()
    print("   greeting %d bytes: %r" % (len(g), g[:80]))
    print("   hex head:", g[:24].hex(" "))
    print()

    body_c = b"1\x00Locator\x00getPeers\x00"

    variants = []

    # A: 标准 TCF —— 4 字节大端总长(含长度字段) + 类型 + payload
    total = 4 + 1 + len(body_c)
    variants.append(("A 标准TCF 长度含自身", struct.pack(">I", total) + b"C" + body_c))
    # B: 长度不含 4 字节
    variants.append(("B 长度不含自身", struct.pack(">I", 1 + len(body_c)) + b"C" + body_c))
    # C: 无长度前缀 + ETX 尾
    variants.append(("C 无长度 + \\x03\\x01", b"C" + body_c + b"\x03\x01"))
    # D: 无长度无尾
    variants.append(("D 无长度无尾", b"C" + body_c))
    # E: 空 token 形式
    variants.append(("E 空token + \\x03\\x01", b"C\x00Locator\x00getPeers\x00\x03\x01"))
    # F: 版本握手 V
    body_v = b"1.6\x00BIG_ENDIAN\x00"
    variants.append(("F V版本 长度含自身", struct.pack(">I", 4 + 1 + len(body_v)) + b"V" + body_v))

    for name, payload in variants:
        try_frame(name, payload)


if __name__ == "__main__":
    main()
