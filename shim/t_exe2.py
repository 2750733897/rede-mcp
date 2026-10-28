# -*- coding: utf-8 -*-
"""验证打包版 redb_shim.exe 的 mi_wait（_inject_and_collect）语义：
   注入一条 MI 后，只返回该命令之后新产生的记录，不夹带历史。
"""
import json, os, socket, subprocess, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, 'bin', 'redb_shim.exe')
ELF = os.path.join(os.path.dirname(HERE), 'simtest', 'demo.elf')
LOG = os.path.join(HERE, 'exe2_session.jsonl')
PORT = 30268
if os.path.exists(LOG):
    os.remove(LOG)

env = dict(os.environ)
env['REDB_SHIM_LOG'] = LOG
env['REDB_SHIM_PORT'] = str(PORT)

p = subprocess.Popen([EXE, '--interpreter=mi2', '--nx'], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     env=env, bufsize=0)
out = []
threading.Thread(target=lambda: [out.append(d) for d in iter(lambda: p.stdout.read(4096), b'')],
                 daemon=True).start()
time.sleep(2.0)


def send(c, wait=0.5):
    p.stdin.write((c + '\n').encode())
    p.stdin.flush()
    time.sleep(wait)


def ctrl(req, timeout=30):
    s = socket.socket()
    s.settimeout(timeout)
    s.connect(('127.0.0.1', PORT))
    s.sendall((json.dumps(req) + '\n').encode())
    b = b''
    while b'\n' not in b:
        d = s.recv(65536)
        if not d:
            break
        b += d
    s.close()
    return json.loads(b.decode())


print('=== 1) 启动 + 载入 ELF + 连模拟器 ===')
send('-gdb-set confirm off')
send('-file-exec-and-symbols %s' % ELF.replace('\\', '/'))
send('-target-select sim --memory-region 0x0,0x1000000')
send('-target-download')
print('state:', json.dumps(ctrl({'op': 'state'}), ensure_ascii=False)[:160])

print('\n=== 2) mi_wait: 下断点（应只返回本条命令的 ^done）===')
r = ctrl({'op': 'mi_wait', 'cmd': '-break-insert compute', 'wait': 1.0})
print('  ok=%s count=%d' % (r.get('ok'), r.get('count', -1)))
for rec in r.get('records', []):
    print('   [%s] %s' % (rec['dir'], rec['mi'][:110]))

print('\n=== 3) mi_wait: 运行（应只返回 stdout 的 running 与本条 done）===')
r = ctrl({'op': 'mi_wait', 'cmd': '-exec-run', 'wait': 2.5})
print('  ok=%s count=%d' % (r.get('ok'), r.get('count', -1)))
for rec in r.get('records', []):
    print('   [%s] %s' % (rec['dir'], rec['mi'][:110]))

print('\n=== 4) 历史是否被正确切分（不应重复出现上一步记录）===')
r2 = ctrl({'op': 'mi_wait', 'cmd': '-stack-list-frames 0 3', 'wait': 1.0})
print('  ok=%s count=%d' % (r2.get('ok'), r2.get('count', -1)))
for rec in r2.get('records', []):
    print('   [%s] %s' % (rec['dir'], rec['mi'][:110]))

print('\n=== 5) tail 全局视图（含 IDE 侧历史）===')
for rec in ctrl({'op': 'tail', 'n': 5}).get('records', []):
    print('   [%s] %s' % (rec['dir'], rec['mi'][:100]))

print('\nIDE 收到字节:', sum(len(x) for x in out))
nlog = sum(1 for _ in open(LOG, encoding='utf-8'))
print('JSONL 落盘条数:', nlog)

p.stdin.close()
try:
    p.wait(timeout=8)
except Exception:
    p.kill()
