# -*- coding: utf-8 -*-
"""验证「IDE 只杀 bootloader」时 shim 能否自救（孤儿回收）。

背景
----
PyInstaller onefile 的进程结构是：

    redb_shim.exe(bootloader)      <-- IDE CreateProcess 出来的就是这个 pid
      └─ redb_shim.exe(python)     <-- 真正监听控制口的那个（ctl 登记的就是它）
           └─ redb.exe

如果 IDE 只终止自己派生的那个 pid（不打进程树），python 这一层会变孤儿并一直
占着控制口 —— 真机踩到过（pid 43816 占着 30266 不撒手）。

本脚本用**独立端口/独立日志**模拟这条路径，不碰现场会话：

    场景 A：只杀 bootloader（taskkill 不加 /T） -> 期望 python 层 2~4s 内自退
    场景 B：正常关 stdin（IDE 正常结束调试）   -> 期望 1s 内退出（回归）

用法：python t_orphan.py
"""

import glob
import json
import os
import socket
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 可用 REDB_SHIM_EXE 指向待测的 exe（比如 bin_next 里的新版），默认测正式路径
EXE = os.environ.get("REDB_SHIM_EXE") or os.path.join(HERE, "bin", "redb_shim.exe")
CTRL = os.path.join(os.path.dirname(EXE), "ctrl")   # shim 在 exe 同目录建 ctrl
PORT_A, PORT_B = 30291, 30292


def up(port, timeout=0.6):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def alive(pid):
    """用 OpenProcess 判存活（比 tasklist 稳，不受名字截断影响）。"""
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
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def ctl_pid(port):
    for p in glob.glob(os.path.join(CTRL, "ctl_*.json")):
        try:
            r = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if r.get("port") == port:
            return r.get("pid")
    return None


def spawn(port, logname):
    env = dict(os.environ)
    env["REDB_SHIM_PORT"] = str(port)
    env["REDB_SHIM_LOG"] = os.path.join(HERE, logname)
    return subprocess.Popen([EXE, "--interpreter", "mi2", "--nx"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


def wait_ctrl(port, timeout=15.0):
    """onefile 解包有延迟，最多等 15s 看到控制口 + 登记。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if up(port):
            pid = ctl_pid(port)
            if pid:
                return pid
        time.sleep(0.3)
    return None


def _kill(pid, tree=False):
    """杀进程。输出直接丢弃——taskkill 的中文提示是 GBK，capture 会解码报错。"""
    cmd = ["taskkill", "/PID", str(pid), "/F"]
    if tree:
        cmd.insert(3, "/T")
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def scenario_a():
    print("== 场景 A：只杀 bootloader（模拟 IDE 只终止自己派生那个 pid）==")
    p = spawn(PORT_A, "orphan_session_a.jsonl")
    boot = p.pid
    child = wait_ctrl(PORT_A)
    if not child:
        print("   !! 控制口没起来，测试无法进行")
        p.kill()
        return False
    print("   bootloader pid=%d（IDE 眼里的 shim）" % boot)
    print("   python   pid=%d（真正监听 %d 的那个）" % (child, PORT_A))
    if boot == child:
        print("   !! 只有一层进程，不是 onefile 结构，本场景没意义")
        p.kill()
        return False

    print("   --> taskkill /PID %d /F（**不加 /T**，只杀这一层）" % boot)
    _kill(boot)
    t0 = time.time()
    exited = None
    for _ in range(16):                    # 最多看 8s
        time.sleep(0.5)
        if not alive(child):
            exited = time.time() - t0
            break
    if exited is None:
        print("   !! %0.1fs 后 python 层仍存活 -> 孤儿未回收（修复无效）" % (time.time() - t0))
        _kill(child, tree=True)
        return False
    print("   python 层自退耗时 %0.1fs" % exited)
    time.sleep(0.5)
    print("   端口 %d: %s" % (PORT_A, "OPEN(仍占用)" if up(PORT_A) else "closed(已释放)"))
    left = [os.path.basename(x) for x in glob.glob(os.path.join(CTRL, "ctl_%d.json" % child))]
    print("   登记清理: %s" % ("已删除" if not left else "残留 " + str(left)))
    return (not up(PORT_A)) and (not left)


def scenario_b():
    print("\n== 场景 B：正常关 stdin（IDE 正常结束调试的路径，回归）==")
    p = spawn(PORT_B, "orphan_session_b.jsonl")
    child = wait_ctrl(PORT_B)
    if not child:
        print("   !! 控制口没起来")
        p.kill()
        return False
    print("   python pid=%d，端口 %d 已监听" % (child, PORT_B))
    t0 = time.time()
    p.stdin.close()
    exited = None
    for _ in range(16):
        time.sleep(0.5)
        if not alive(child):
            exited = time.time() - t0
            break
    if exited is None:
        print("   !! 8s 内未退出")
        _kill(child, tree=True)
        return False
    print("   退出耗时 %0.1fs" % exited)
    print("   端口 %d: %s" % (PORT_B, "OPEN" if up(PORT_B) else "closed(已释放)"))
    return not up(PORT_B)


if __name__ == "__main__":
    if not os.path.exists(EXE):
        print("找不到", EXE)
        raise SystemExit(2)
    # 先确认测试端口干净，别和现场会话搅在一起
    for port in (PORT_A, PORT_B):
        if up(port):
            print("端口 %d 已被占用，换个基址再跑" % port)
            raise SystemExit(2)
    ok_a = scenario_a()
    ok_b = scenario_b()
    print("\n== 结论 ==")
    print("   A 只杀 bootloader  -> %s" % ("通过：python 层自退、端口释放、登记清除"
                                        if ok_a else "失败"))
    print("   B 正常关 stdin     -> %s" % ("通过：1s 级退出、端口释放"
                                        if ok_b else "失败"))
    raise SystemExit(0 if (ok_a and ok_b) else 1)
