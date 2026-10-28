# -*- coding: utf-8 -*-
"""端到端演练 takeover.py：本脚本扮演"IDE"，拉起 shim 并用模拟器后端建会话，
   然后以子进程方式跑 takeover.py 的各子命令，验证接管链路（不碰真板）。
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.dirname(HERE)
PY = sys.executable
EXE = os.path.join(HERE, 'bin', 'redb_shim.exe')
ELF = os.path.join(MCP, 'simtest', 'demo.elf')
LOG = os.path.join(HERE, 'takeover_session.jsonl')
TAKEOVER = os.path.join(HERE, 'takeover.py')

if os.path.exists(LOG):
    os.remove(LOG)

env = dict(os.environ)
env['REDB_SHIM_LOG'] = LOG
env['REDB_SHIM_PORT'] = '30265'

print('== 0) 先验：控制口没起时 takeover.py 应给出友好提示 ==')
r = subprocess.run([PY, TAKEOVER, 'status'], capture_output=True, text=True)
print('   rc=%d  %s' % (r.returncode, (r.stdout + r.stderr).strip()))

print('\n== 1) 扮演 IDE 拉起 shim ==')
p = subprocess.Popen([EXE, '--interpreter=mi2', '--nx'], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     env=env, bufsize=0)
nbytes = [0]


def drain():
    while True:
        d = p.stdout.read(4096)
        if not d:
            return
        nbytes[0] += len(d)


threading.Thread(target=drain, daemon=True).start()
time.sleep(2.0)


def ide_send(c, wait=0.6):
    p.stdin.write((c + '\n').encode())
    p.stdin.flush()
    time.sleep(wait)


ide_send('-gdb-set confirm off')
ide_send('-file-exec-and-symbols %s' % ELF.replace('\\', '/'))
ide_send('-target-select sim --memory-region 0x0,0x1000000')
ide_send('-target-download', 1.0)
print('   IDE 侧已建会话，累计收到 %d 字节' % nbytes[0])


def run(*args):
    r = subprocess.run([PY, TAKEOVER] + list(args), capture_output=True, text=True)
    print(r.stdout.rstrip() or r.stderr.rstrip())
    return r


print('\n== 2) takeover.py status（体检 + 流水） ==')
run('status')

print('\n== 3) takeover.py bp compute（AI 替人下断点） ==')
run('bp', 'compute')

print('\n== 4) takeover.py go（AI 替人继续，等 *stopped） ==')
run('go')

print('\n== 5) takeover.py demo（读断点表/栈/局部量/寄存器） ==')
run('demo')

print('\n== 6) 收尾 ==')
print('   IDE 侧全程收到 %d 字节（透传未中断）' % nbytes[0])
print('   JSONL 条数 %d' % sum(1 for _ in open(LOG, encoding='utf-8')))
p.stdin.close()
try:
    p.wait(timeout=8)
except Exception:
    p.kill()
