# -*- coding: utf-8 -*-
"""由原始 hello.launch 派生 hello_ai.launch：只改 DEBUG_NAME，保持 CRLF 与其余字节一致。

输出两处：
  1) _ai_artifacts/rede_mcp/shim/hello_ai.launch   —— 归档模板
  2) workspace/.metadata/.../.launches/hello_ai.launch —— 让 ReDe 的
     Debug Configurations 里直接出现 hello_ai（纯新增，可整文件删除还原）
"""
import os
import shutil

REDE = r"D:\guangxin7045\6.1.1-ARM"
SRC = os.path.join(REDE, r"workspace\.metadata\.plugins\org.eclipse.debug.core\.launches\hello.launch")
SHIM = os.path.join(REDE, r"workspace\_ai_artifacts\rede_mcp\shim\bin\redb_shim.exe")
KEEP = os.path.join(REDE, r"workspace\_ai_artifacts\rede_mcp\shim\hello_ai.launch")
DROP = os.path.join(REDE, r"workspace\.metadata\.plugins\org.eclipse.debug.core\.launches\hello_ai.launch")

OLD_KEY = b'key="org.eclipse.cdt.dsf.gdb.DEBUG_NAME" value="D:\\guangxin7045\\6.1.1-ARM\\tools\\debug\\arm\\redb.exe"'
NEW_KEY = ('key="org.eclipse.cdt.dsf.gdb.DEBUG_NAME" value="%s"' % SHIM).encode("ascii")


def main():
    raw = open(SRC, "rb").read()
    assert OLD_KEY in raw, "原始 DEBUG_NAME 不是预期值，先看一眼"
    out = raw.replace(OLD_KEY, NEW_KEY)

    # 归档模板
    shutil.copyfile(SRC, KEEP + ".orig.bak")
    open(KEEP, "wb").write(out)

    # 放进 Eclipse 的 launch 目录
    open(DROP, "wb").write(out)

    for p in (KEEP, DROP):
        b = open(p, "rb").read()
        print("%-6d bytes  CRLF=%d  %s" % (len(b), b.count(b"\r\n"), p))
    # 差异校验：除 DEBUG_NAME 行外必须完全相同
    a = open(SRC, "rb").read().split(b"\r\n")
    c = out.split(b"\r\n")
    diff = [(i, x, y) for i, (x, y) in enumerate(zip(a, c)) if x != y]
    print("差异行数:", len(diff), "（应为 1）")
    for i, x, y in diff:
        print("  L%d\n   - %s\n   + %s" % (i + 1, x.decode("latin-1"), y.decode("latin-1")))


if __name__ == "__main__":
    main()
