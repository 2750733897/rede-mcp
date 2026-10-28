# -*- coding: utf-8 -*-
"""列出 redb_shim / redb / ReDe 的 PID、PPID、创建时间（ctypes，不依赖 wmic）。"""
import ctypes
import ctypes.wintypes as wt
import time

TH32CS_SNAPPROCESS = 0x00000002
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def snapshot():
    h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out = []
    if not k32.Process32FirstW(h, ctypes.byref(pe)):
        k32.CloseHandle(h)
        return out
    while True:
        out.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.szExeFile, pe.cntThreads))
        if not k32.Process32NextW(h, ctypes.byref(pe)):
            break
    k32.CloseHandle(h)
    return out


def main():
    procs = snapshot()
    want = ("redb_shim.exe", "redb.exe", "ReDe.exe")
    k32.OpenProcess.restype = wt.HANDLE
    for pid, ppid, name, threads in procs:
        if name in want:
            # 进程创建时间
            hp = k32.OpenProcess(0x0400, False, pid)  # QUERY_INFORMATION
            created = ""
            if hp:
                ft = wt.FILETIME()
                if k32.GetProcessTimes(hp, ctypes.byref(ft),
                                       ctypes.byref(wt.FILETIME()),
                                       ctypes.byref(wt.FILETIME()),
                                       ctypes.byref(wt.FILETIME())):
                    t = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
                    created = time.strftime("%H:%M:%S", time.localtime(t / 1e7 - 11644473600))
                k32.CloseHandle(hp)
            print("%-14s pid=%-6d ppid=%-6d threads=%-3d started=%s" % (name, pid, ppid, threads, created))
    print("\n--- 30265..30284 监听者 ---")
    import subprocess
    out = subprocess.run(["netstat", "-ano"], capture_output=True).stdout
    text = out.decode("gbk", "replace")
    for line in text.splitlines():
        if "LISTENING" in line and any(":%d " % p in line for p in range(30265, 30285)):
            print("  ", line.strip())


if __name__ == "__main__":
    main()
