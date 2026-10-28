# -*- coding: utf-8 -*-
"""
rede_mcp_server.py — 让 AI 直接使用锐华 ReDe 内置调试能力的 MCP 服务

原理
----
ReDe 的调试后端是 tools/debug/arm/redb.exe —— 一个 GNU gdb 7.4.1 的锐华定制分支
(--target=arm-elf)，支持完整的 MI2 解释器。IDE 自己也只是它的一个 MI 客户端
(cn.com.ecict.rede.debug.core 里的 FinalLaunchSequence 继承 CDT DSF 的 GdbLaunch)。

因此本服务不碰 Eclipse，直接以子进程方式驱动 redb.exe 的 MI2 通道，
对 AI 暴露一组语义化的调试工具：连接 / 断点 / 运行控制 / 调用栈 / 变量 /
内存 / 寄存器 / 任务(线程) / 反汇编 / 原始控制台。

两种后端
--------
  rede_attach   —— 连板上 gdbstub (target remote host:port)，真机调试
  rede_sim      —— 用 gdb 内置 ARM 模拟器 (target sim)，无板验证全链路

注意
----
* redb.exe 是 cygwin 程序，依赖同目录 cygwin1.dll，必须以该目录为工作目录启动。
* 它内嵌的 Python2.6 会在 stderr 抱怨找不到 PYTHONHOME，与 stdout 的 MI 无关，
  只影响 pretty-printer，这里直接丢弃 stderr。
* MI 是行协议且带异步记录(*stopped / =thread-*)，必须用状态机缓冲，不能一问一答。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# 路径默认值（可用环境变量覆盖）
# --------------------------------------------------------------------------

ROOT = os.environ.get("REDE_ROOT", r"D:\guangxin7045\6.1.1-ARM")
REDB_EXE = os.environ.get("REDB_EXE", os.path.join(ROOT, "tools", "debug", "arm", "redb.exe"))
REDB_CWD = os.path.dirname(REDB_EXE)

DEFAULT_HOST = os.environ.get("REDE_HOST", "192.168.0.100")
DEFAULT_PORT = int(os.environ.get("REDE_PORT", "30253"))
DEFAULT_TCF_PORT = int(os.environ.get("REDE_TCF_PORT", "1534"))


# --------------------------------------------------------------------------
# MI 值语法解析
#   value   := cstring | tuple | list
#   tuple   := '{' [result (',' result)*] '}'
#   list    := '[' [elem (',' elem)*] ']'     elem := result | value
# --------------------------------------------------------------------------


class MIParseError(Exception):
    pass


def _parse_cstring(s: str, i: int):
    if i >= len(s) or s[i] != '"':
        raise MIParseError("expect cstring at %d: %r" % (i, s[i:i + 20]))
    i += 1
    out: List[str] = []
    while i < len(s):
        c = s[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            i += 1
            if i >= len(s):
                break
            n = s[i]
            simple = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v",
                      "a": "\a", "b": "\b", "e": "\x1b", '"': '"', "'": "'", "\\": "\\"}
            if n in simple:
                out.append(simple[n])
                i += 1
            elif n == "x":
                j = i + 1
                while j < len(s) and s[j] in "0123456789abcdefABCDEF":
                    j += 1
                out.append(chr(int(s[i + 1:j], 16)))
                i = j
            elif n.isdigit():
                j = i
                while j < len(s) and j < i + 3 and s[j] in "01234567":
                    j += 1
                out.append(chr(int(s[i:j], 8)))
                i = j
            else:
                out.append(n)
                i += 1
        else:
            out.append(c)
            i += 1
    raise MIParseError("unterminated cstring")


def _looks_like_result(s: str, i: int) -> bool:
    j = i
    while j < len(s) and (s[j].isalnum() or s[j] in "_-."):
        j += 1
    return j > i and j < len(s) and s[j] == "="


def _parse_value(s: str, i: int):
    if i >= len(s):
        raise MIParseError("unexpected end")
    c = s[i]
    if c == '"':
        return _parse_cstring(s, i)
    if c == "{":
        i += 1
        d: Dict[str, Any] = {}
        if i < len(s) and s[i] == "}":
            return d, i + 1
        while True:
            j = i
            while j < len(s) and s[j] != "=":
                j += 1
            key = s[i:j]
            val, i = _parse_value(s, j + 1)
            d[key] = val
            if i < len(s) and s[i] == ",":
                i += 1
                continue
            if i < len(s) and s[i] == "}":
                return d, i + 1
            raise MIParseError("bad tuple near %r" % s[i:i + 30])
    if c == "[":
        i += 1
        lst: List[Any] = []
        if i < len(s) and s[i] == "]":
            return lst, i + 1
        while True:
            if _looks_like_result(s, i):
                j = i
                while j < len(s) and s[j] != "=":
                    j += 1
                key = s[i:j]
                val, i = _parse_value(s, j + 1)
                lst.append({key: val})
            else:
                val, i = _parse_value(s, i)
                lst.append(val)
            if i < len(s) and s[i] == ",":
                i += 1
                continue
            if i < len(s) and s[i] == "]":
                return lst, i + 1
            raise MIParseError("bad list near %r" % s[i:i + 30])
    raise MIParseError("unexpected char %r at %d" % (c, i))


def _parse_results(s: str) -> Dict[str, Any]:
    """解析 `k=v,k2=v2,...`；首段没有前导逗号，后续段才有。"""
    res: Dict[str, Any] = {}
    s = s.strip()
    if not s:
        return res
    i, n = 0, len(s)
    while i < n:
        if s[i] == ",":
            i += 1
            continue
        j = i
        while j < n and s[j] != "=":
            if s[j] in "{[":
                raise MIParseError("expect '=' near %r" % s[i:i + 20])
            j += 1
        if j >= n:
            raise MIParseError("no '=' in result near %r" % s[i:i + 20])
        key = s[i:j]
        val, i = _parse_value(s, j + 1)
        res[key] = val
    return res


def parse_record(line: str) -> Optional[Dict[str, Any]]:
    """把一行 MI 输出解析成 {'kind','token','cls','data'} 或 None。"""
    line = line.rstrip("\r\n")
    if not line or line.strip() in ("(gdb)", ""):
        return None

    token = None
    m = re.match(r"^(\d+)", line)
    if m:
        token = int(m.group(1))
        line = line[m.end():]
        if not line:
            return None

    head = line[0]
    body = line[1:]

    if head in "^*+=":
        cls, _, rest = body.partition(",")
        try:
            data = _parse_results(rest) if rest else {}
        except MIParseError:
            data = {"_raw": rest}
        kind = {"^": "result", "*": "async", "=": "notify", "+": "plus"}[head]
        return {"kind": kind, "token": token, "cls": cls.strip(), "data": data}

    if head in '~&@':
        try:
            payload, _ = _parse_cstring(body, 0)
        except MIParseError:
            payload = body
        return {"kind": {"~": "console", "&": "log", "@": "target"}[head],
                "token": token, "cls": None, "data": payload}

    return {"kind": "other", "token": token, "cls": None, "data": body}


# --------------------------------------------------------------------------
# MI 会话
# --------------------------------------------------------------------------


class RedbSession:
    def __init__(self, exe: str = REDB_EXE, cwd: str = REDB_CWD):
        self.exe = exe
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._buf = b""
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._pending: Dict[int, Optional[Dict[str, Any]]] = {}
        self._stops: List[Dict[str, Any]] = []
        self._notifies: List[Dict[str, Any]] = []
        self._console: List[str] = []
        self._token = 0
        self._alive = False
        self._reader: Optional[threading.Thread] = None
        self.last_stop: Optional[Dict[str, Any]] = None
        self.target_desc: Optional[str] = None
        self.symbol_file: Optional[str] = None
        self.needs_exec_run = False
        self.running = False

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._alive and self.proc and self.proc.poll() is None:
            return
        if not os.path.exists(self.exe):
            raise RuntimeError("redb.exe 不存在: %s" % self.exe)
        for name in ("cygwin1.dll", "cygintl-8.dll"):
            if not os.path.exists(os.path.join(self.cwd, name)):
                raise RuntimeError("%s 缺失，redb 无法启动（必须在 %s 目录下运行）" % (name, self.cwd))

        self.proc = subprocess.Popen(
            [self.exe, "--interpreter=mi2", "--nx"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,   # 屏蔽内嵌 Python2.6 的噪音
            bufsize=0,
        )
        self._alive = True
        self._buf = b""
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        with self._cv:
            self._alive = False
            self._cv.notify_all()
        if self.proc and self.proc.poll() is None:
            try:
                self.cmd("-gdb-exit", timeout=3)
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.target_desc = None
        self.symbol_file = None
        self.last_stop = None

    @property
    def alive(self) -> bool:
        return bool(self._alive and self.proc and self.proc.poll() is None)

    # ---------------- 读取线程 ----------------

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                chunk = self.proc.stdout.read(4096)
            except Exception:
                break
            if not chunk:
                break
            self._buf += chunk
            try:
                self._drain()
            except Exception:
                # 单行解析异常不能杀死读取线程，否则整个会话会静默失联
                self._buf = b""
        with self._cv:
            self._alive = False
            self._cv.notify_all()

    def _drain(self) -> None:
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode("latin-1").rstrip("\r")
            for rec in self._split_prompt(line):
                self._dispatch(rec)

    @staticmethod
    def _split_prompt(line: str) -> List[str]:
        # gdb 有时把 prompt 和记录放在同一行
        parts = re.split(r"\(gdb\)\s*", line)
        return [p for p in parts if p.strip()]

    def _dispatch(self, line: str) -> None:
        rec = parse_record(line)
        if rec is None:
            return
        with self._cv:
            kind = rec["kind"]
            if kind == "console":
                self._console.append(rec["data"])
                if len(self._console) > 400:
                    del self._console[:200]
            elif kind == "result":
                tok = rec["token"]
                if tok is not None and tok in self._pending:
                    self._pending[tok] = rec
                    self._cv.notify_all()
            elif kind == "async":
                if rec["cls"] == "stopped":
                    self.running = False
                    self.last_stop = rec
                    self._stops.append(rec)
                else:
                    if rec["cls"] == "running":
                        self.running = True
                    self._notifies.append(rec)
                self._cv.notify_all()
            elif kind == "notify":
                self._notifies.append(rec)
                self._cv.notify_all()

    # ---------------- 命令 ----------------

    def cmd(self, command: str, timeout: float = 20.0) -> Dict[str, Any]:
        if not self.alive:
            raise RuntimeError("redb 会话未启动或已退出")
        with self._cv:
            self._token += 1
            tok = self._token
            self._pending[tok] = None
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(("%d%s\n" % (tok, command)).encode("latin-1"))
            self.proc.stdin.flush()
        except Exception as exc:
            raise RuntimeError("写入 MI 命令失败: %s" % exc)

        deadline = time.time() + timeout
        with self._cv:
            while self._pending.get(tok) is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._pending.pop(tok, None)
                    hint = ""
                    if self.running:
                        hint = "（目标仍在运行：先用 rede_interrupt 停下，再取状态）"
                    raise TimeoutError("MI 命令超时(%.0fs): %s%s" % (timeout, command, hint))
                if not self._alive:
                    self._pending.pop(tok, None)
                    raise RuntimeError("redb 已退出，命令未完成: %s" % command)
                self._cv.wait(min(remaining, 0.2))
            rec = self._pending.pop(tok)
        assert rec is not None
        if rec["cls"] == "error":
            msg = rec["data"].get("msg", "unknown")
            raise RuntimeError("MI 错误: %s  (命令: %s)" % (msg, command))
        return rec

    def wait_stop(self, timeout: float) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        with self._cv:
            while not self._stops:
                remaining = deadline - time.time()
                if remaining <= 0 or not self._alive:
                    return None
                self._cv.wait(min(remaining, 0.2))
            return self._stops.pop(0)

    def drain_console(self) -> str:
        with self._cv:
            text = "".join(self._console)
            self._console.clear()
        return text.strip()


# --------------------------------------------------------------------------
# 结果整理
# --------------------------------------------------------------------------


def _frame_summary(frame: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not frame:
        return {}
    out = {
        "level": frame.get("level"),
        "addr": frame.get("addr"),
        "func": frame.get("func"),
        "file": frame.get("file"),
        "fullname": frame.get("fullname"),
        "line": frame.get("line"),
    }
    if "from" in frame:
        out["from"] = frame["from"]
    return {k: v for k, v in out.items() if v is not None}


def _stop_summary(stop: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not stop:
        return {"reason": "unknown"}
    d = stop.get("data", {})
    out = {"reason": d.get("reason")}
    for key in ("thread-id", "signal-name", "signal-meaning", "bkptno", "disp"):
        if key in d:
            out[key.replace("-", "_")] = d[key]
    fr = d.get("frame")
    if isinstance(fr, dict):
        out["frame"] = _frame_summary(fr)
    if "stopped-threads" in d:
        out["stopped_threads"] = d["stopped-threads"]
    return out


def _norm_path(p: str) -> str:
    return p.replace("\\", "/")


def _parse_addr(text: str) -> int:
    t = str(text).strip()
    if t.lower().startswith("0x"):
        return int(t, 16)
    if re.fullmatch(r"[0-9a-fA-F]{8,16}", t):
        return int(t, 16)
    return int(t, 0)


# --------------------------------------------------------------------------
# 全局会话
# --------------------------------------------------------------------------

SESSION: Optional[RedbSession] = None
mcp = FastMCP("rede-debug")


def _sess() -> RedbSession:
    global SESSION
    if SESSION is None or not SESSION.alive:
        raise RuntimeError("尚未连接目标，请先调用 rede_attach(...) 或 rede_sim(...)")
    return SESSION


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具：探测 / 连接
# --------------------------------------------------------------------------


@mcp.tool()
def rede_probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout_s: float = 2.0) -> str:
    """探测目标端是否运行着 GDB stub（只发 qSupported 握手，通常不会打断目标）。

    返回端口状态、GDB 远程串协议特征、以及包长限制。
    """
    pkt_body = "qSupported:multiprocess+;swbreak+;hwbreak+;qRelocInsn+;vContSupported+"
    checksum = sum(ord(c) for c in pkt_body) % 256
    pkt = ("$%s#%02x" % (pkt_body, checksum)).encode()

    result: Dict[str, Any] = {"host": host, "port": port}
    s = socket.socket()
    s.settimeout(timeout_s)
    try:
        t0 = time.time()
        s.connect((host, port))
        result["tcp"] = "connected"
        result["connect_ms"] = round((time.time() - t0) * 1000, 1)
        s.sendall(pkt)
        try:
            data = s.recv(4096)
        except socket.timeout:
            result["gdbstub"] = False
            result["verdict"] = "TCP 可连但无 RSP 应答：不是 gdbserver（可能是锐华私有服务端口）"
            return _j(result)
        text = data.decode("latin-1", "replace")
        result["raw"] = text[:400]
        result["gdbstub"] = text.startswith("$")
        if text.startswith("$"):
            body = text[1:].split("#")[0]
            feats: Dict[str, Any] = {}
            for item in body.split(";"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    feats[k] = v
                elif item:
                    feats[item] = True
            result["features"] = feats
            pk = feats.get("PacketSize")
            if pk:
                try:
                    result["max_packet_bytes"] = int(pk, 16)
                except ValueError:
                    pass
            result["verdict"] = "确认是 GDB stub，可以 rede_attach"
        else:
            result["verdict"] = "应答非 RSP 格式，不是 gdbserver"
        return _j(result)
    except ConnectionRefusedError:
        result["tcp"] = "refused"
        result["gdbstub"] = False
        result["verdict"] = "端口未监听：目标侧调试代理没起来"
        return _j(result)
    except Exception as exc:
        result["tcp"] = "error"
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["verdict"] = "连接失败"
        return _j(result)
    finally:
        s.close()


@mcp.tool()
def rede_tcf_hello(host: str = DEFAULT_HOST, port: int = DEFAULT_TCF_PORT,
                   timeout_s: float = 3.0) -> str:
    """探测目标侧 TCF agent（ReDe 自己走的就是这条通道，默认 1534）。

    连接后 agent 会主动推送 Locator/Hello 问候帧，里面列出它支持的服务。
    这是判断"板子能不能被调试"的最可靠信号——比 30253 更可信，因为 30253
    只是 IDE 本机开的 gdbserver 代理端口，IDE 不跑就不存在。
    """
    out: Dict[str, Any] = {"host": host, "port": port}
    s = socket.socket()
    s.settimeout(timeout_s)
    try:
        s.connect((host, port))
        out["tcp"] = "connected"
        buf = b""
        try:
            while len(buf) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if buf.endswith(b"\x03\x01") or b"\x03\x01" in buf[-4:]:
                    break
        except socket.timeout:
            pass
        if not buf:
            out["tcf"] = False
            out["verdict"] = "TCP 通但无问候帧：不是 TCF agent"
            return _j(out)

        out["raw_head"] = buf[:32].decode("latin-1")
        body = buf.rstrip(b"\x03\x01")
        fields = [f.decode("latin-1", "replace") for f in body.split(b"\x00") if f]
        if fields:
            head = fields.pop(0)
            out["frame_type"] = head[0] if head else None
            if head[1:]:
                fields.insert(0, head[1:])
            out["service"] = fields[0] if len(fields) > 0 else None
            out["command"] = fields[1] if len(fields) > 1 else None
        services: List[str] = []
        for f in fields:
            if f.startswith("[") and f.endswith("]"):
                try:
                    services = json.loads(f)
                    break
                except Exception:
                    pass
        out["services"] = services
        out["tcf"] = bool(services)
        if services:
            out["verdict"] = "确认 TCF agent，服务 %d 个" % len(services)
            debug_need = {"RunControl", "Breakpoints", "Registers", "Memory"}
            out["debug_capable"] = sorted(debug_need & set(services))
            out["debug_missing"] = sorted(debug_need - set(services))
        else:
            out["verdict"] = "有数据但没解析出服务列表"
        return _j(out)
    except ConnectionRefusedError:
        out["tcp"] = "refused"
        out["tcf"] = False
        out["verdict"] = "端口未监听：目标没起 TCF agent"
        return _j(out)
    except (socket.timeout, TimeoutError):
        out["tcp"] = "timeout"
        out["tcf"] = False
        out["verdict"] = ("连接/读取超时：TCF agent 是单客户端，"
                          "很可能已被 ReDe IDE 的调试会话占用，需先在 IDE 里停止调试")
        return _j(out)
    except Exception as exc:
        out["tcp"] = "error"
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["verdict"] = "连接失败"
        return _j(out)
    finally:
        s.close()


@mcp.tool()
def rede_launch_info(launch_file: str) -> str:
    """解析 ReDe 的 .launch 启动配置，提取连接参数与符号文件，供后续 attach 使用。

    只读。典型位置：
      <workspace>/.metadata/.plugins/org.eclipse.debug.core/.launches/<name>.launch

    注意：里面的 PORT 是 **IDE 本机 gdbserver 代理**的端口（UDP），
    不是板子的端口；AI 直连目标要走 1534 的 TCF agent。
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(launch_file)
    except Exception as exc:
        return _j({"ok": False, "error": "解析失败: %s" % exc})

    root = tree.getroot()
    attrs: Dict[str, str] = {}
    for node in root:
        key = node.get("key")
        if not key:
            continue
        val = node.get("value")
        if val is None:
            entries = [e.get("value") for e in node if e.get("value") is not None]
            val = ",".join(entries)
        attrs[key] = val

    out: Dict[str, Any] = {
        "ok": True,
        "launch_type": root.get("type"),
        "debug_mode": attrs.get("debug_mode"),
        "level": attrs.get("level"),
        "host": attrs.get("org.eclipse.cdt.dsf.gdb.HOST")
                or attrs.get("cn.com.ecict.rede.target.debug.launch.HOST_ATTR"),
        "port": attrs.get("org.eclipse.cdt.dsf.gdb.PORT"),
        "non_stop": attrs.get("org.eclipse.cdt.dsf.gdb.NON_STOP"),
        "gdb": attrs.get("org.eclipse.cdt.dsf.gdb.DEBUG_NAME"),
        "program": attrs.get("org.eclipse.cdt.launch.PROGRAM_NAME"),
        "image": attrs.get("fileprogram"),
        "project": attrs.get("org.eclipse.cdt.launch.PROJECT_ATTR"),
        "stop_at": attrs.get("org.eclipse.cdt.launch.DEBUGGER_STOP_AT_MAIN_SYMBOL"),
        "rede_path": attrs.get("REDE_PATH"),
    }
    out = {k: v for k, v in out.items() if v is not None}
    out["caveat"] = ("port 是 IDE 本机代理口(UDP)，仅 IDE 调试会话期间存在且单客户端；"
                     "AI 独立调试请连目标 1534 的 TCF agent")
    return _j(out)


@mcp.tool()
def rede_attach_from_launch(launch_file: str, transport: str = "tcp") -> str:
    """读取 .launch 里的符号文件并连接目标（host/port 取自配置）。

    注意：.launch 里的 PORT 属于 IDE 本机代理，通常抢不到；真机请优先用 rede_sim 验证
    或等 TCF 客户端就绪后走 1534。
    """
    raw = rede_launch_info(launch_file)
    info = json.loads(raw)
    if not info.get("ok"):
        return raw
    host, port = info.get("host"), info.get("port")
    symbol = info.get("program")
    if not host or not port:
        return _j({"ok": False, "error": ".launch 里缺 host 或 port", "parsed": info})
    non_stop = str(info.get("non_stop", "")).lower() == "true"
    return rede_attach(host=host, port=int(port), symbol_file=symbol,
                       non_stop=non_stop, transport=transport)


def _do_connect(kind: str, target: str, symbol_file: Optional[str], non_stop: bool) -> Dict[str, Any]:
    global SESSION
    if SESSION is not None and SESSION.alive:
        SESSION.close()
    sess = RedbSession()
    sess.start()
    SESSION = sess

    info: Dict[str, Any] = {"backend": kind, "target": target}
    sess.cmd("-gdb-set confirm off")
    sess.cmd("-gdb-set pagination off")
    sess.cmd("-gdb-set print pretty off")
    if non_stop:
        sess.cmd("-gdb-set non-stop on")
    info["non_stop"] = non_stop

    if symbol_file:
        path = _norm_path(os.path.abspath(symbol_file))
        sess.cmd("-file-exec-and-symbols %s" % path, timeout=60)
        sess.symbol_file = path
        info["symbol_file"] = path

    rec = sess.cmd("-target-select %s" % target, timeout=30)
    sess.target_desc = kind
    info["connected"] = True
    if "thread-id" in rec["data"]:
        info["thread_id"] = rec["data"]["thread-id"]

    try:
        ti = sess.cmd("-thread-info", timeout=15)
        threads = ti["data"].get("threads", [])
        info["thread_count"] = len(threads)
        info["threads"] = [_thread_brief(t) for t in threads][:16]
    except Exception as exc:
        info["thread_info_error"] = str(exc)

    try:
        bl = sess.cmd("-break-list", timeout=10)
        info["breakpoints"] = (bl["data"].get("BreakpointTable", {}) or {}).get("body", [])
    except Exception:
        pass

    return info


def _thread_brief(t: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "id": t.get("id"),
        "target_id": t.get("target-id"),
        "name": t.get("name"),
        "state": t.get("state"),
    }
    fr = t.get("frame")
    if isinstance(fr, dict):
        out["frame"] = _frame_summary(fr)
    if "core" in t:
        out["core"] = t["core"]
    return {k: v for k, v in out.items() if v is not None}


@mcp.tool()
def rede_attach(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                symbol_file: Optional[str] = None, non_stop: bool = False,
                transport: str = "tcp") -> str:
    """连接板上 gdbserver 并开始调试（等价于 IDE 里点"调试"之后 redb 做的事）。

    host/port: 目标调试地址
    symbol_file: 带调试符号的 .obj 或 .elf（ReDe 工程里通常是 gnuarm/<BOARD>/<proj>.obj）
    non_stop: 是否开启 non-stop 模式（IDE 的 launch 配置里是 true；多任务场景建议开）
    transport: "tcp" 或 "udp"。注意 ReDe IDE 自己的代理走的是 udp:127.0.0.1:30253，
               且只服务它自己拉起的 redb，AI 抢不到这个槽位。

    注意：连接会挂起目标，调试结束后请调用 rede_detach 恢复运行。
    """
    try:
        prefix = "udp:" if transport.lower() == "udp" else ""
        target = "remote %s%s:%d" % (prefix, host, port)
        info = _do_connect("remote", target, symbol_file, non_stop)
        st = _sess().cmd("-thread-info", timeout=10)
        info["state_after_attach"] = "stopped" if st else "unknown"
        return _j(info)
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_sim(symbol_file: str, memory_region: str = "0x0,0x1000000") -> str:
    """用 redb 内置 ARM 模拟器跑本地程序，无需目标板即可验证调试链路。

    symbol_file: 本地 ELF/OBJ
    memory_region: 模拟内存映射，arm-elf 的 sim 必须显式给，否则 run 会崩
    """
    try:
        info = _do_connect("sim", "sim --memory-region %s" % memory_region, symbol_file, False)
        sess = _sess()
        sess.cmd("-target-download", timeout=30)
        sess.needs_exec_run = True
        info["loaded"] = True
        return _j(info)
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_load_symbols(symbol_file: str) -> str:
    """在当前会话里加载/替换调试符号文件（不重连目标）。"""
    try:
        sess = _sess()
        path = _norm_path(os.path.abspath(symbol_file))
        sess.cmd("-file-exec-and-symbols %s" % path, timeout=60)
        sess.symbol_file = path
        return _j({"ok": True, "symbol_file": path})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_status() -> str:
    """查看当前调试会话状态：后端类型、符号文件、最近一次停机原因、当前帧。"""
    global SESSION
    if SESSION is None or not SESSION.alive:
        return _j({"connected": False, "hint": "调用 rede_attach 或 rede_sim 建立会话"})
    sess = SESSION
    out: Dict[str, Any] = {
        "connected": True,
        "backend": sess.target_desc,
        "symbol_file": sess.symbol_file,
        "last_stop": _stop_summary(sess.last_stop),
    }
    try:
        bl = sess.cmd("-break-list", timeout=10)
        tbl = bl["data"].get("BreakpointTable", {}) or {}
        out["breakpoints"] = tbl.get("body", [])
    except Exception:
        pass
    try:
        ti = sess.cmd("-thread-info", timeout=10)
        out["threads"] = [_thread_brief(t) for t in ti["data"].get("threads", [])]
    except Exception:
        pass
    return _j(out)


# --------------------------------------------------------------------------
# 工具：断点
# --------------------------------------------------------------------------


@mcp.tool()
def rede_break_insert(spec: str, condition: Optional[str] = None,
                      ignore_count: int = 0, temporary: bool = False) -> str:
    """下断点。

    spec 三种写法：
      函数名        "compute"
      文件:行号     "usrInit.c:120"  或  "../../usrInit.c:120"
      绝对地址      "*0x00206774"

    condition: 条件表达式，如 "i==2"
    ignore_count: 跳过前 N 次命中
    temporary: true 表示一次性断点
    """
    try:
        sess = _sess()
        cmd = "-break-insert"
        if temporary:
            cmd += " -t"
        if condition:
            cmd += " -c %s" % json.dumps(condition)
        if ignore_count:
            cmd += " -i %d" % ignore_count
        cmd += " %s" % spec.strip()
        rec = sess.cmd(cmd, timeout=15)
        bp = rec["data"].get("bkpt", {})
        return _j({"ok": True, "breakpoint": bp})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc), "spec": spec})


@mcp.tool()
def rede_break_list() -> str:
    """列出全部断点（含命中次数）。"""
    try:
        sess = _sess()
        rec = sess.cmd("-break-list", timeout=10)
        tbl = rec["data"].get("BreakpointTable", {}) or {}
        rows = []
        for item in tbl.get("body", []):
            bp = item.get("bkpt", {})
            rows.append({
                "number": bp.get("number"),
                "type": bp.get("type"),
                "enabled": bp.get("enabled"),
                "addr": bp.get("addr"),
                "func": bp.get("func"),
                "file": bp.get("file"),
                "line": bp.get("line"),
                "times": bp.get("times"),
                "cond": bp.get("cond"),
            })
        return _j({"count": len(rows), "breakpoints": rows})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_break_delete(number: Optional[int] = None) -> str:
    """删除断点；number 为空则删除全部断点。"""
    try:
        sess = _sess()
        cmd = "-break-delete" + ("" if number is None else " %d" % number)
        sess.cmd(cmd, timeout=10)
        return _j({"ok": True, "deleted": number if number is not None else "all"})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_break_enable(number: int, enabled: bool = True) -> str:
    """启用/禁用指定断点。"""
    try:
        sess = _sess()
        sess.cmd("-break-%s %d" % ("enable" if enabled else "disable", number), timeout=10)
        return _j({"ok": True, "number": number, "enabled": enabled})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


# --------------------------------------------------------------------------
# 工具：运行控制
# --------------------------------------------------------------------------


def _run_and_wait(cmd: str, timeout_s: float, thread_id: Optional[str]) -> Dict[str, Any]:
    sess = _sess()
    # 模拟器后端：load 完成后还没有 inferior，第一次"继续"必须用 -exec-run 启动
    if cmd == "-exec-continue" and sess.needs_exec_run:
        cmd = "-exec-run"
        sess.needs_exec_run = False
    full = cmd if not thread_id else "%s --thread %s" % (cmd, thread_id)
    rec = sess.cmd(full, timeout=20)
    if rec["cls"] == "running":
        stop = sess.wait_stop(timeout_s)
        if stop is None:
            return {"state": "running", "note": "已继续运行，%.0fs 内未停机" % timeout_s}
        return {"state": "stopped", "stop": _stop_summary(stop)}
    return {"state": "stopped", "stop": _stop_summary(sess.last_stop)}


@mcp.tool()
def rede_resume(mode: str = "continue", timeout_s: float = 10.0,
                thread_id: Optional[str] = None, address: Optional[str] = None) -> str:
    """运行控制，返回停机原因与当前帧。

    mode:
      continue  继续跑（撞断点/异常才返回）
      step      源码级单步进入
      next      源码级单步跳过
      stepi     汇编级单步
      nexti     汇编级单步跳过
      finish    执行到当前函数返回
      until     运行到指定地址

    timeout_s: 等待停机的秒数；超时则返回 state=running，可用 rede_interrupt 打断
    thread_id: 指定任务/线程（MI 的 thread-id，如 "1"），多任务场景用
    address: 仅 until 模式使用，如 "0x8030"
    """
    mapping = {
        "continue": "-exec-continue",
        "step": "-exec-step",
        "next": "-exec-next",
        "stepi": "-exec-step-instruction",
        "nexti": "-exec-next-instruction",
        "finish": "-exec-finish",
    }
    try:
        if mode == "until":
            if not address:
                return _j({"ok": False, "error": "until 模式需要 address"})
            sess = _sess()
            sess.cmd("-exec-until %s" % address, timeout=20)
            stop = sess.wait_stop(timeout_s)
            if stop is None:
                return _j({"state": "running"})
            return _j({"state": "stopped", "stop": _stop_summary(stop)})
        if mode not in mapping:
            return _j({"ok": False, "error": "未知 mode: %s" % mode, "valid": sorted(mapping) + ["until"]})
        out = _run_and_wait(mapping[mode], timeout_s, thread_id)
        out["mode"] = mode
        return _j(out)
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_interrupt(thread_id: Optional[str] = None) -> str:
    """打断正在运行的目标（IDE 里的暂停按钮）。"""
    try:
        sess = _sess()
        cmd = "-exec-interrupt"
        if thread_id:
            cmd += " --thread %s" % thread_id
        try:
            sess.cmd(cmd, timeout=10)
        except Exception:
            pass
        stop = sess.wait_stop(5)
        return _j({"stopped": stop is not None, "stop": _stop_summary(stop)})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


# --------------------------------------------------------------------------
# 工具：调用栈 / 变量 / 内存 / 寄存器 / 任务
# --------------------------------------------------------------------------


@mcp.tool()
def rede_backtrace(limit: int = 20, thread_id: Optional[str] = None,
                   full: bool = False) -> str:
    """取调用栈。

    注意：redb 是 GDB 7.4.1，-stack-list-frames 没有 --no-args/--full，
    只能按区间取帧：-stack-list-frames LOW HIGH。
    """
    try:
        sess = _sess()
        cmd = "-stack-list-frames 0 %d" % max(0, limit - 1)
        if thread_id:
            cmd += " --thread %s" % thread_id
        rec = sess.cmd(cmd, timeout=15)
        raw = rec["data"].get("stack", [])
        frames = []
        for item in raw[:limit]:
            fr = item.get("frame", item) if isinstance(item, dict) else {}
            frames.append(_frame_summary(fr))
        out = {"count": len(frames), "frames": frames}
        if full:
            try:
                sess.cmd("-stack-select-frame 0", timeout=10)
                ar = sess.cmd("-stack-list-arguments --simple-values 0 0", timeout=15)
                out["frame0_args"] = ar["data"].get("stack-args", [])
            except Exception as exc:
                out["frame0_args_error"] = str(exc)
        return _j(out)
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_locals(frame: int = 0, thread_id: Optional[str] = None,
                all_values: bool = False) -> str:
    """列出指定帧的局部变量（GDB 7.4 无 --frame 选项，先 select-frame 再取）。"""
    try:
        sess = _sess()
        if frame:
            sess.cmd("-stack-select-frame %d" % frame, timeout=10)
        flag = "--all-values" if all_values else "--simple-values"
        cmd = "-stack-list-variables %s" % flag
        if thread_id:
            cmd += " --thread %s" % thread_id
        rec = sess.cmd(cmd, timeout=15)
        return _j({"frame": frame, "variables": rec["data"].get("variables", [])})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_args(frame: int = 0, thread_id: Optional[str] = None) -> str:
    """列出指定帧的函数参数。"""
    try:
        sess = _sess()
        cmd = "-stack-list-arguments --simple-values %d %d" % (frame, frame)
        if thread_id:
            cmd += " --thread %s" % thread_id
        rec = sess.cmd(cmd, timeout=15)
        return _j({"frame": frame, "arguments": rec["data"].get("stack-args", [])})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_eval(expression: str, frame: int = 0, thread_id: Optional[str] = None) -> str:
    """求值一个 C 表达式 / 变量 / 指针解引用 / 强制类型转换。

    例：g_counter、p.x、*(int*)0x180fc、((struct point*)p)->y
    """
    try:
        sess = _sess()
        sess.cmd("-stack-select-frame %d" % frame, timeout=10) if frame else None
        rec = sess.cmd("-data-evaluate-expression %s" % expression, timeout=15)
        return _j({"expression": expression, "value": rec["data"].get("value")})
    except Exception as exc:
        return _j({"ok": False, "expression": expression, "error": str(exc)})


@mcp.tool()
def rede_struct(expression: str, recursive: bool = False) -> str:
    """展开结构体/数组/指针的成员（用 -var-object 做，能看到嵌套字段）。"""
    try:
        sess = _sess()
        name = "v%d" % int(time.time() * 1000 % 1000000)
        sess.cmd("-var-create %s * %s" % (name, expression), timeout=20)
        out: Dict[str, Any] = {"expression": expression}
        try:
            rec = sess.cmd("-var-list-children --all-values %s" % name, timeout=20)
            kids = rec["data"].get("children", [])
            out["children"] = [
                {"name": k.get("child", {}).get("exp"),
                 "type": k.get("child", {}).get("type"),
                 "value": k.get("child", {}).get("value")}
                if isinstance(k, dict) else k
                for k in kids
            ]
            out["count"] = len(out["children"])
            if recursive:
                out["note"] = "递归展开暂只到一层，深层请对子表达式再调用 rede_struct"
        finally:
            try:
                sess.cmd("-var-delete %s" % name, timeout=10)
            except Exception:
                pass
        return _j(out)
    except Exception as exc:
        return _j({"ok": False, "expression": expression, "error": str(exc)})


@mcp.tool()
def rede_read_memory(address: str, length: int = 64) -> str:
    """读内存，返回十六进制与 ASCII 双栏。

    address: 起始地址，可写 0x180fc 或 0x180fc+16
    """
    try:
        sess = _sess()
        rec = sess.cmd("-data-read-memory-bytes %s %d" % (address, length), timeout=15)
        mem = rec["data"].get("memory", [])
        if not mem:
            return _j({"ok": False, "error": "无数据"})
        node = mem[0]
        hexstr = node.get("contents", "")
        raw = bytes.fromhex(hexstr)
        base = _parse_addr(node.get("begin", address))
        lines = []
        for off in range(0, len(raw), 16):
            chunk = raw[off:off + 16]
            hexpart = " ".join("%02x" % b for b in chunk)
            asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append("%08x  %-47s  |%s|" % (base + off, hexpart, asciipart))
        return _j({
            "begin": node.get("begin"),
            "end": node.get("end"),
            "bytes": len(raw),
            "hex": hexstr,
            "dump": "\n".join(lines),
        })
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_write_memory(address: str, hex_bytes: str) -> str:
    """写内存。hex_bytes 形如 "01000000"（连续十六进制，无空格）。"""
    try:
        sess = _sess()
        clean = re.sub(r"[^0-9a-fA-F]", "", hex_bytes)
        if len(clean) % 2:
            return _j({"ok": False, "error": "hex 字节串长度必须是偶数"})
        sess.cmd('-data-write-memory-bytes %s "%s"' % (address, clean), timeout=15)
        return _j({"ok": True, "address": address, "bytes": len(clean) // 2})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_registers() -> str:
    """读全部寄存器（十六进制）。"""
    try:
        sess = _sess()
        names = sess.cmd("-data-list-register-names", timeout=10)
        nl = names["data"].get("register-names", [])
        vals = sess.cmd("-data-list-register-values x", timeout=15)
        rows = []
        for item in vals["data"].get("register-values", []):
            num = item.get("number")
            try:
                idx = int(num)
            except (TypeError, ValueError):
                idx = -1
            nm = nl[idx] if 0 <= idx < len(nl) and nl[idx] else "r%s" % num
            rows.append({"reg": nm, "value": item.get("value")})
        return _j({"count": len(rows), "registers": rows})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_threads() -> str:
    """列出目标上的任务/线程（ReWorks 任务级调试时这里就是任务列表）。"""
    try:
        sess = _sess()
        rec = sess.cmd("-thread-info", timeout=15)
        return _j({"threads": [_thread_brief(t) for t in rec["data"].get("threads", [])]})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_thread_select(thread_id: str) -> str:
    """切换当前任务/线程。"""
    try:
        sess = _sess()
        rec = sess.cmd("-thread-select %s" % thread_id, timeout=10)
        return _j({"ok": True, "thread_id": rec["data"].get("new-thread-id")})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


# --------------------------------------------------------------------------
# 工具：反汇编 / 控制台 / 收尾
# --------------------------------------------------------------------------


def _addr_of(sess: "RedbSession", spec: str) -> int:
    """把 "0x8030" / "compute" / "&g_array" 解析成整数地址。"""
    s = spec.strip()
    if re.match(r"^0x[0-9a-fA-F]+$", s):
        return int(s, 16)
    if re.fullmatch(r"[0-9a-fA-F]{6,16}", s):
        return int(s, 16)
    expr = s if s.startswith("&") else "&" + s
    rec = sess.cmd('-data-evaluate-expression "%s"' % expr, timeout=15)
    val = str(rec["data"].get("value", ""))
    m = re.search(r"0x[0-9a-fA-F]+", val)
    if not m:
        raise RuntimeError("无法解析地址: %s -> %r" % (s, val))
    return int(m.group(0), 16)


@mcp.tool()
def rede_disassemble(address: str, count: int = 12, end: Optional[str] = None) -> str:
    """反汇编。

    address: 起始地址或符号名（"compute" / "0x8030" / "&g_array"）
    count:   反汇编条数（按 4 字节 ARM 指令估算区间）
    end:     结束地址；给了 end 就忽略 count

    GDB 7.4.1 的 -data-disassemble 不支持 -c，只能 -s/-e 区间。
    """
    try:
        sess = _sess()
        start = _addr_of(sess, address)
        if end:
            stop = _addr_of(sess, end)
        else:
            stop = start + max(1, count) * 4
        rec = sess.cmd("-data-disassemble -s 0x%x -e 0x%x -- 0" % (start, stop), timeout=20)
        out = []
        for ins in rec["data"].get("asm_insns", []):
            out.append({
                "addr": ins.get("address"),
                "func": ins.get("func-name"),
                "inst": (ins.get("inst") or "").strip(),
            })
        return _j({"start": "0x%x" % start, "end": "0x%x" % stop,
                   "count": len(out), "instructions": out[:max(1, count) if not end else len(out)]})
    except Exception as exc:
        return _j({"ok": False, "error": str(exc)})


@mcp.tool()
def rede_console(command: str) -> str:
    """执行任意 GDB 控制台命令，返回文本输出。

    用于 MI 没覆盖的能力，例如：
      "info registers"、"x/8wx 0x180fc"、"p *p"、
      "reworks_load_symbol_file <name> <id>"（锐华定制：按模块加载符号）
    """
    try:
        sess = _sess()
        sess.drain_console()
        rec = sess.cmd('-interpreter-exec console %s' % json.dumps(command), timeout=30)
        text = sess.drain_console()
        return _j({"command": command, "class": rec["cls"], "output": text})
    except Exception as exc:
        return _j({"ok": False, "command": command, "error": str(exc)})


@mcp.tool()
def rede_detach(resume: bool = True) -> str:
    """断开目标。resume=true 时先让目标恢复运行再断开（板子继续正常跑）。"""
    global SESSION
    if SESSION is None or not SESSION.alive:
        return _j({"ok": True, "note": "本就没有活动会话"})
    sess = SESSION
    out: Dict[str, Any] = {}
    try:
        if resume:
            try:
                sess.cmd("-exec-continue", timeout=10)
                out["resumed"] = True
            except Exception as exc:
                out["resume_error"] = str(exc)
        sess.cmd("-target-detach", timeout=10)
        out["detached"] = True
    except Exception as exc:
        out["detach_error"] = str(exc)
    finally:
        sess.close()
        SESSION = None
    return _j(out)


# --------------------------------------------------------------------------
# 工具：接管 IDE 现有调试会话（方案 D —— 不自己造调试服务器）
#
#   ReDe IDE --MI--> redb_shim.exe --MI--> 真 redb --RSP(UDP)--> IDE代理 --TCF--> 板子
#
# shim 由 IDE 启动（.launch 的 DEBUG_NAME 指向它），AI 通过本地控制口注入 MI
# 并读取 MI 流水，等于"替人操作 + 替人收信息"。
# --------------------------------------------------------------------------

SHIM_PORT = int(os.environ.get("REDE_SHIM_PORT", "30265"))

# ctrl 目录按优先级取第一个存在的：显式环境变量 > 与本文件同级的 shim/bin/ctrl
# （git clone 后直接跑）> 本机历史布局（pip 安装 / 放到别处时兜底）
_HERE = os.path.dirname(os.path.abspath(__file__))
_CTRL_CANDIDATES = [
    os.environ.get("REDE_SHIM_CTRL_DIR"),
    os.path.join(_HERE, "shim", "bin", "ctrl"),
    os.path.join(ROOT, "workspace", "_ai_artifacts", "rede_mcp", "shim", "bin", "ctrl"),
]


def _pick_ctrl_dir() -> str:
    for d in _CTRL_CANDIDATES:
        if d and os.path.isdir(d):
            return d
    return _CTRL_CANDIDATES[1]


SHIM_CTRL_DIR = _pick_ctrl_dir()


def _shim_discover() -> Dict[str, Any]:
    """从 shim 自己登记的实例表里挑一个在线的控制口（多会话时取最新）。

    shim 会在 <shim>/bin/ctrl/ctl_<pid>.json 登记 {pid, port, started, build}；
    端口从 30265 起依次尝试独占绑定，所以多会话时端口可能不是 30265。
    """
    if os.environ.get("REDE_SHIM_PORT"):
        return {"pid": None, "port": SHIM_PORT, "build": "(env)"}
    try:
        import glob
        recs = []
        for path in glob.glob(os.path.join(SHIM_CTRL_DIR, "ctl_*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                rec["_path"] = path
                recs.append(rec)
            except Exception:
                continue
        recs.sort(key=lambda r: r.get("started", 0), reverse=True)
        for rec in recs:
            probe = socket.socket()
            probe.settimeout(0.6)
            try:
                probe.connect(("127.0.0.1", int(rec.get("port", 0))))
                return rec
            except Exception:
                try:
                    os.remove(rec["_path"])
                except Exception:
                    pass
            finally:
                probe.close()
    except Exception:
        pass
    return {"pid": None, "port": SHIM_PORT, "build": "(default)"}


def _shim_call(req: Dict[str, Any], timeout: float = 8.0) -> Dict[str, Any]:
    rec = _shim_discover()
    port = int(rec.get("port") or SHIM_PORT)
    s = socket.socket()
    s.settimeout(timeout)
    try:
        try:
            s.connect(("127.0.0.1", port))
        except (ConnectionRefusedError, socket.timeout, TimeoutError, OSError) as exc:
            # 本机未监听时 Windows 可能直接丢 SYN（表现为超时）而不是 refused，两种都归一化
            return {"ok": False,
                    "error": ("控制口 %d 未监听（%s）：IDE 还没用 shim 启动调试。"
                              "需把 .launch 的 GDB 路径指向 redb_shim.exe 后再点调试"
                              % (port, type(exc).__name__)),
                    "hint": "也可以用 shim/takeover.py status / takeover.py list 直接体检",
                    "ctrl_dir": SHIM_CTRL_DIR}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            return {"ok": False, "error": "控制口无应答（shim 可能正忙或子进程已退出）"}
        out = json.loads(buf.decode("utf-8", "replace"))
        out.setdefault("_ctrl_port", port)
        return out
    except (socket.timeout, TimeoutError):
        return {"ok": False,
                "error": "控制口 %d 应答超时：shim 在等 redb（目标在跑或命令未返回），稍后重试" % port}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        s.close()


@mcp.tool()
def rede_shim_list() -> str:
    """列出所有在线/登记过的 shim 实例（pid、控制口、启动时间、build），
    用于多调试会话时确认"接下来要操作的是哪一个"。"""
    import glob
    recs = []
    for path in glob.glob(os.path.join(SHIM_CTRL_DIR, "ctl_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        probe = socket.socket()
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", int(rec.get("port", 0))))
            rec["alive"] = True
        except Exception:
            rec["alive"] = False
        finally:
            probe.close()
        recs.append(rec)
    recs.sort(key=lambda r: r.get("started", 0), reverse=True)
    return _j({"ctrl_dir": SHIM_CTRL_DIR, "count": len(recs),
               "note": "started 最新且 alive=true 的那个就是 rede_shim_* 会操作的对象",
               "instances": recs})


@mcp.tool()
def rede_shim_status() -> str:
    """查看 IDE 现有调试会话（经 MI 中间人）是否在线，以及日志路径与记录数。"""
    return _j(_shim_call({"op": "state"}))


@mcp.tool()
def rede_shim_tail(n: int = 40) -> str:
    """读取最近 n 条 MI 记录（IDE 与 redb 的双向流量）。"""
    return _j(_shim_call({"op": "tail", "n": n}))


@mcp.tool()
def rede_shim_mi(command: str, wait_s: float = 1.2) -> str:
    """向 IDE 正在用的那个 redb 注入一条 MI 命令，只返回这条命令之后的新记录。

    等价于"人来点按钮"，但走的是 IDE 自己的调试通道（IDE 界面同时可见）。
    例：-break-insert UserInit / -stack-list-frames 0 9 / -data-evaluate-expression g_x
    """
    return _j(_shim_call({"op": "mi_wait", "cmd": command, "wait": wait_s}))


# 停机/终止类记录：见到就说明"目标已经不在跑了"
STOP_MARKS = ('*stopped', '=thread-group-exited', '^error')


def _shim_records_since(n0: int, limit: int = 800) -> List[Dict[str, Any]]:
    tail = _shim_call({"op": "tail", "n": limit})
    return [r for r in tail.get("records", []) if int(r.get("i", 0)) > n0]


def _shim_run_and_wait(cmd: str, timeout_s: float = 30.0) -> Dict[str, Any]:
    """注入一条运行类命令，然后轮询到真正停下为止（不是盲等固定秒数）。

    只用 state / mi_wait / tail，所以对旧版 shim 也兼容。
    """
    st = _shim_call({"op": "state"})
    n0 = int((st or {}).get("records") or 0)
    _shim_call({"op": "mi_wait", "cmd": cmd, "wait": 0.15})

    seen: set = set()
    fresh: List[Dict[str, Any]] = []
    deadline = time.time() + max(1.0, timeout_s)
    stop_rec = None
    while time.time() < deadline:
        for r in _shim_records_since(n0):
            if r["i"] in seen:
                continue
            seen.add(r["i"])
            fresh.append(r)
            if stop_rec is None and any(m in r.get("mi", "") for m in STOP_MARKS):
                stop_rec = r
        if stop_rec is not None:
            break
        time.sleep(0.25)

    return {"ok": True, "sent": cmd, "count": len(fresh),
            "stopped": stop_rec is not None,
            "stop_line": stop_rec["mi"] if stop_rec else None,
            "records": fresh[-40:]}


@mcp.tool()
def rede_shim_resume(mode: str = "continue", timeout_s: float = 30.0) -> str:
    """在 IDE 的会话里做运行控制，**等到目标真的停下**再返回（不是盲等）。

    mode: continue / step / next / stepi / nexti / finish / interrupt
    返回含 stopped/stop_line，以及期间产生的 MI 记录（最后 40 条）。
    若 timeout_s 内没停，stopped=false —— 目标应仍在跑，可用 mode="interrupt" 暂停。
    """
    table = {"continue": "-exec-continue", "step": "-exec-step", "next": "-exec-next",
             "stepi": "-exec-step-instruction", "nexti": "-exec-next-instruction",
             "finish": "-exec-finish", "interrupt": "-exec-interrupt"}
    if mode not in table:
        return _j({"ok": False, "error": "未知 mode", "valid": sorted(table)})
    res = _shim_run_and_wait(table[mode], timeout_s)
    if not res.get("stopped") and mode != "interrupt":
        res["hint"] = ("%.0fs 内没等到 *stopped：目标可能仍在跑，"
                       "需要停下请调 rede_shim_resume(mode=\"interrupt\")" % timeout_s)
    return _j(res)


@mcp.tool()
def rede_shim_interrupt(timeout_s: float = 15.0) -> str:
    """暂停正在运行的目标（等价 IDE 的暂停按钮）。

    目标在跑时所有读取类命令都会报 Target is executing.，先调这个再读。
    """
    res = _shim_run_and_wait("-exec-interrupt", timeout_s)
    st = _shim_call({"op": "state"})
    res["records_total"] = (st or {}).get("records")
    return _j(res)


@mcp.tool()
def rede_shim_is_stopped() -> str:
    """判断目标当前是否已停下（读数据前的先决条件）。"""
    r = _shim_call({"op": "mi_wait", "cmd": "-thread-info", "wait": 2.0})
    blob = " ".join(x.get("mi", "") for x in r.get("records", []))
    stopped = 'state="stopped"' in blob
    return _j({"ok": True, "stopped": stopped,
               "advice": None if stopped else "目标在跑，先调 rede_shim_interrupt() 再读",
               "threads": [x["mi"] for x in r.get("records", []) if "^done,threads=" in x.get("mi", "")]})


@mcp.tool()
def rede_shim_step_line(mode: str = "next", timeout_s: float = 30.0) -> str:
    """高一层包装：确保目标停下后单步一行，返回停机位置。

    mode: next（源码级跨过）/ step（进入函数）
    """
    if mode not in ("next", "step"):
        return _j({"ok": False, "error": "mode 只能是 next / step"})
    is_stopped = json.loads(rede_shim_is_stopped()).get("stopped")
    if not is_stopped:
        rede_shim_interrupt()
    res = _shim_run_and_wait("-exec-%s" % mode, timeout_s)
    return _j(res)


# --------------------------------------------------------------------------
# shim 路径的结构化读取：AI 直接拿字段，不必自己啃 MI 文本
# --------------------------------------------------------------------------

def _shim_parsed(cmd: str, wait_s: float = 1.5, timeout: float = 10.0) -> List[Dict[str, Any]]:
    """注入一条 MI，把 gdb>ide 方向的记录解析成 {'kind','cls','data','raw'}。"""
    r = _shim_call({"op": "mi_wait", "cmd": cmd, "wait": wait_s}, timeout=timeout)
    out: List[Dict[str, Any]] = []
    for x in r.get("records", []):
        if x.get("dir") != "gdb>ide":
            continue
        raw = x.get("mi", "")
        try:
            rec = parse_record(raw)
        except Exception:
            rec = None
        if rec:
            rec = dict(rec)
            rec["raw"] = raw
            out.append(rec)
    return out


def _shim_done(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """取最后一条 ^done 的 data（没有则空 dict）。"""
    for r in reversed(recs):
        if r.get("kind") == "result" and r.get("cls") == "done":
            return r.get("data") or {}
    return {}


def _shim_error(recs: List[Dict[str, Any]]) -> Optional[str]:
    for r in recs:
        if r.get("kind") == "result" and r.get("cls") == "error":
            return str((r.get("data") or {}).get("msg") or "error")
    return None


def _int_or_none(v):
    s = str(v if v is not None else "")
    return int(s) if s.isdigit() else None


def _hex_if_plain(v):
    """$lr 这类没有符号的寄存器，GDB 给的是十进制；统一成 0x 便于和地址对照。"""
    s = str(v if v is not None else "")
    return "0x%x" % int(s) if s.isdigit() else s


def _thread_meta(t: Dict[str, Any]) -> Dict[str, Any]:
    """把 GDB 7.4 的 `target-id="Thread 70809600"` + `details="'shell' PRI: 170"` 拆开。

    多任务 RTOS（ReWorks）的线程名和优先级没有独立字段，只能从这两个串里抠。
    """
    target_id = str(t.get("target-id") or "")
    details = str(t.get("details") or "")
    name = None
    m = re.match(r"'([^']*)'", details)
    if m:
        name = m.group(1)
    prio = None
    mp = re.search(r"PRI:\s*(-?\d+)", details)
    if mp:
        prio = int(mp.group(1))
    tid = None
    mt = re.match(r"Thread\s+(\d+)", target_id)
    if mt:
        tid = mt.group(1)
    return {"id": t.get("id"), "tid": tid, "name": name, "priority": prio,
            "state": t.get("state"), "core": t.get("core"),
            "details": details or None}


def _as_list_of(recs: List[Dict[str, Any]], key: str, inner: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 ^done 的 data 里取列表；MI 的 `[frame={...}]` 会解析成 [{'frame': {...}}]。"""
    data = _shim_done(recs)
    lst = data.get(key)
    if not isinstance(lst, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in lst:
        if not isinstance(item, dict):
            continue
        if inner:
            item = item.get(inner) if isinstance(item.get(inner), dict) else None
        if isinstance(item, dict):
            out.append(item)
    return out


@mcp.tool()
def rede_shim_snapshot(depth: int = 6, with_locals: bool = True,
                       with_regs: bool = True, with_breakpoints: bool = True) -> str:
    """一次读全当前停机现场 —— 接手 IDE 会话的第一步。

    返回结构化字段（不用自己解析 MI 文本）：
      thread      {id,name,state,core,priority}
      frames      [{level,addr,func,file,line}]     ← 0 号是最内层
      locals      [{name,type,value}]
      regs        {pc,sp,lr,pc_symbol}
      breakpoints [{number,type,enabled,addr,func,file,line}]
    目标在跑时会自动暂停再读；读不到就给出 advice。
    """
    depth = max(1, min(40, int(depth)))
    note: Optional[str] = None
    st = json.loads(rede_shim_is_stopped())
    if not st.get("stopped"):
        rede_shim_interrupt()
        st = json.loads(rede_shim_is_stopped())
        note = ("目标原在运行，已自动暂停" if st.get("stopped")
                else "暂停失败：目标可能在内核关键区/中断上下文，稍后重试或看 IDE 界面")

    tgt = _shim_discover()
    out: Dict[str, Any] = {"ok": bool(st.get("stopped")), "stopped": bool(st.get("stopped")),
                           "note": note, "shim_pid": tgt.get("pid"), "shim_port": tgt.get("port"),
                           "build": tgt.get("build")}
    if not out["ok"]:
        out["advice"] = "先用 rede_shim_interrupt() 暂停，再调本工具"
        return _j(out)

    # 1) 线程：优先取 state=stopped 的那个（多任务系统里跑着的其它任务不算）
    th_recs = _shim_parsed("-thread-info", 2.0)
    threads = [_thread_meta(t) for t in _as_list_of(th_recs, "threads")]
    out["threads"] = threads
    cur = next((t for t in threads if t.get("state") == "stopped"), None) or (threads[0] if threads else None)
    out["thread"] = cur

    # 2) 调用栈
    fr = _as_list_of(_shim_parsed("-stack-list-frames 0 %d" % (depth - 1), 1.5), "stack", "frame")
    frames = []
    for f in fr:
        frames.append({"level": _int_or_none(f.get("level")), "addr": f.get("addr"),
                       "func": f.get("func"), "file": f.get("file"),
                       "line": _int_or_none(f.get("line"))})
    frames.sort(key=lambda x: x["level"] if x["level"] is not None else 0)
    out["frames"] = frames
    if frames and not any(f["file"] for f in frames):
        out["frames_note"] = ("全部帧都没有 file/line：多半停在内核/无调试信息的模块。"
                              "看用户态用 break 到用户函数再停，或直接看 func+addr")

    # 3) 当前帧局部变量
    if with_locals:
        vs = _as_list_of(_shim_parsed("-stack-list-variables --simple-values", 1.5), "variables")
        out["locals"] = [{"name": v.get("name"), "type": v.get("type"), "value": v.get("value")}
                         for v in vs]
        if not vs:
            out["locals_note"] = "当前帧没有局部量（可能停在汇编/无调试信息处）"

    # 4) 关键寄存器（-data-list-register-values 在 GDB 7.4 只认编号，所以走表达式）
    if with_regs:
        regs = {}
        for name in ("pc", "sp", "lr"):
            d = _shim_done(_shim_parsed('-data-evaluate-expression "$%s"' % name, 1.2))
            v = d.get("value")
            if v is None:
                continue
            s = str(v)
            m = re.match(r"^(0x[0-9a-fA-F]+|\d+)\s*(?:<([^>]*)>)?", s)
            if m:
                regs[name] = _hex_if_plain(m.group(1))
                if m.group(2):
                    regs[name + "_symbol"] = m.group(2)
            else:
                regs[name] = s
        out["regs"] = regs

    # 5) 断点表（-break-list 只有一次调用，body 在 BreakpointTable 里）
    if with_breakpoints:
        bps = []
        data = _shim_done(_shim_parsed("-break-list", 2.0))
        table = data.get("BreakpointTable") if isinstance(data.get("BreakpointTable"), dict) else {}
        for item in (table.get("body") or []):
            b = item.get("bkpt") if isinstance(item, dict) else None
            if not isinstance(b, dict):
                continue
            bps.append({"number": b.get("number"), "type": b.get("type"),
                        "enabled": b.get("enabled"), "addr": b.get("addr"),
                        "func": b.get("func"), "file": b.get("file"),
                        "line": _int_or_none(b.get("line")),
                        "location": b.get("original-location")})
        out["breakpoints"] = bps

    return _j(out)


@mcp.tool()
def rede_shim_eval(expression: str) -> str:
    """在 IDE 会话里求值一个 C 表达式，返回 {value, addr, symbol}。

    例：g_counter / p.x / *ptr / array[2] / $sp
    结构体请逐字段求值（p.x），整结构体在 GDB 7.4 上常返回不可读的聚合串。
    """
    if not expression or not expression.strip():
        return _j({"ok": False, "error": "expression 不能为空"})
    expr = expression.strip().replace('"', '\\"')
    recs = _shim_parsed('-data-evaluate-expression "%s"' % expr, 1.5)
    err = _shim_error(recs)
    if err:
        return _j({"ok": False, "expression": expression, "error": err,
                   "advice": ("Target is executing" in err) and
                             "目标在跑：先 rede_shim_interrupt()" or None})
    val = _shim_done(recs).get("value")
    if val is None:
        return _j({"ok": False, "expression": expression, "error": "无返回（可能没有符号）"})
    s = str(val)
    m = re.match(r"^(0x[0-9a-fA-F]+|\d+)\s*(?:<([^>]*)>)?$", s)
    res = {"ok": True, "expression": expression, "value": s}
    if m:
        res["addr"] = m.group(1)
        if m.group(2):
            res["symbol"] = m.group(2)
    return _j(res)


def _shim_addr(spec: str) -> Optional[int]:
    """把 '0x8000' / '&g_var' / 'g_var' / 'compute' 解析成整数地址。"""
    s = (spec or "").strip()
    if not s:
        return None
    if re.match(r"^0x[0-9a-fA-F]+$", s):
        return int(s, 16)
    if re.fullmatch(r"[0-9a-fA-F]{6,16}", s):
        return int(s, 16)
    expr = s if s.startswith("&") else "&" + s
    d = _shim_done(_shim_parsed('-data-evaluate-expression "%s"' % expr, 1.5))
    m = re.match(r"^(0x[0-9a-fA-F]+|\d+)", str(d.get("value") or ""))
    return int(m.group(1), 0) if m else None


@mcp.tool()
def rede_shim_read_mem(address: str, count: int = 32) -> str:
    """读目标内存（默认 32 字节），返回 hex 与 ascii。

    address 支持 "0x7aa00000" / "&g_array" / "g_array"；count 上限 4096。
    """
    count = max(1, min(4096, int(count)))
    addr = _shim_addr(address)
    if addr is None:
        return _j({"ok": False, "error": "地址无法解析: %s" % address,
                   "hint": "用 0x 前缀的绝对地址，或确认该符号有调试信息"})
    recs = _shim_parsed("-data-read-memory-bytes 0x%x %d" % (addr, count), 1.5)
    err = _shim_error(recs)
    if err:
        return _j({"ok": False, "address": "0x%x" % addr, "error": err,
                   "advice": ("executing" in err) and "目标在跑：先 rede_shim_interrupt()" or None})
    mem = _as_list_of(recs, "memory")
    if not mem:
        return _j({"ok": False, "address": "0x%x" % addr, "error": "无 memory 字段"})
    blob = "".join(str(x.get("contents") or "") for x in mem)
    try:
        raw = bytes.fromhex(blob)
    except ValueError:
        return _j({"ok": False, "error": "hex 解析失败", "raw_hex": blob[:200]})
    return _j({"ok": True, "address": "0x%x" % addr, "count": len(raw),
               "hex": raw.hex(),
               "ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in raw)})


@mcp.tool()
def rede_shim_disasm(spec: str = "pc", count: int = 8) -> str:
    """反汇编。spec 可以是 pc / 函数名 / &符号 / 0x地址；count 为指令条数（上限 64）。

    ARM 按 4 字节/条估算区间；Thumb 代码可能不准，必要时直接用 0x 地址。
    """
    count = max(1, min(64, int(count)))
    if spec.strip().lower() in ("pc", "$pc"):
        d = _shim_done(_shim_parsed('-data-evaluate-expression "$pc"', 1.2))
        m = re.match(r"^(0x[0-9a-fA-F]+|\d+)", str(d.get("value") or ""))
        addr = int(m.group(1), 0) if m else None
    else:
        addr = _shim_addr(spec)
    if addr is None:
        return _j({"ok": False, "error": "无法解析起始位置: %s" % spec})
    recs = _shim_parsed("-data-disassemble -s 0x%x -e 0x%x -- 0"
                        % (addr, addr + count * 4), 2.0, timeout=15.0)
    err = _shim_error(recs)
    if err:
        return _j({"ok": False, "error": err})
    insns = []
    for it in _as_list_of(recs, "asm_insns"):
        insns.append({"address": it.get("address"), "func": it.get("func-name"),
                      "offset": _int_or_none(it.get("offset")), "inst": it.get("inst")})
    return _j({"ok": True, "start": "0x%x" % addr, "count": len(insns), "insns": insns[:count]})


@mcp.tool()
def rede_shim_break(spec: str, temporary: bool = False) -> str:
    """在 IDE 会话里下断点，返回断点号与解析出的位置。

    spec: 函数名 / file.c:123 / *0x00206774
    temporary=True 为一次性断点（命中后自动删除，等价 IDE 的 "t"）。
    """
    s = (spec or "").strip()
    if not s:
        return _j({"ok": False, "error": "spec 不能为空"})
    cmd = "-break-insert %s%s" % ("-t " if temporary else "", s)
    recs = _shim_parsed(cmd, 2.0)
    err = _shim_error(recs)
    if err:
        return _j({"ok": False, "spec": spec, "error": err,
                   "advice": "断点过多或符号不存在；先用 rede_shim_snapshot 看现有断点"})
    b = _shim_done(recs).get("bkpt")
    if not isinstance(b, dict):
        return _j({"ok": False, "error": "未返回 bkpt", "raw": [r.get("raw") for r in recs][:3]})
    return _j({"ok": True, "number": b.get("number"), "type": b.get("type"),
               "temporary": temporary, "enabled": b.get("enabled"),
               "addr": b.get("addr"), "func": b.get("func"), "file": b.get("file"),
               "line": _int_or_none(b.get("line")),
               "pending": b.get("pending")})


def main() -> None:
    """stdio 入口（pip 安装后为 rede-mcp 命令，等价 python rede_mcp_server.py）。"""
    mcp.run()


if __name__ == "__main__":
    main()
