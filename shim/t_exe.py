import json, os, socket, subprocess, sys, threading, time
HERE=os.path.dirname(os.path.abspath(__file__))
EXE=os.path.join(HERE,'bin','redb_shim.exe')
ELF=os.path.join(os.path.dirname(HERE),'simtest','demo.elf')
LOG=os.path.join(HERE,'exe_session.jsonl'); PORT=30267
if os.path.exists(LOG): os.remove(LOG)
env=dict(os.environ); env['REDB_SHIM_LOG']=LOG; env['REDB_SHIM_PORT']=str(PORT)
p=subprocess.Popen([EXE,'--interpreter=mi2','--nx'],stdin=subprocess.PIPE,
                   stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,bufsize=0)
out=[]
threading.Thread(target=lambda:[out.append(d) for d in iter(lambda:p.stdout.read(4096),b'')],daemon=True).start()
time.sleep(2.0)
def send(c):
    p.stdin.write((c+'\n').encode()); p.stdin.flush(); time.sleep(0.5)
def ctrl(req):
    s=socket.socket(); s.settimeout(5); s.connect(('127.0.0.1',PORT))
    s.sendall((json.dumps(req)+'\n').encode()); b=b''
    while b'\n' not in b:
        d=s.recv(65536)
        if not d: break
        b+=d
    s.close(); return json.loads(b.decode())
send('-gdb-set confirm off')
send('-file-exec-and-symbols %s'%ELF.replace('\\','/'))
send('-target-select sim --memory-region 0x0,0x1000000')
send('-target-download')
print('state:', json.dumps(ctrl({'op':'state'}),ensure_ascii=False)[:150])
print('inject:', ctrl({'op':'mi','cmd':'-break-insert compute'}))
time.sleep(0.6)
print('run:', ctrl({'op':'mi','cmd':'-exec-run'}))
time.sleep(2.0)
recs=ctrl({'op':'tail','n':6}).get('records',[])
for r in recs: print('   [%s] %s'%(r['dir'], r['mi'][:100]))
print('IDE 收到字节:', sum(len(x) for x in out))
p.stdin.close()
try: p.wait(timeout=8)
except Exception: p.kill()
