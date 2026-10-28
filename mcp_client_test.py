# -*- coding: utf-8 -*-
"""用官方 SDK 的 stdio 客户端连本 MCP server，验证握手、工具枚举与远端调用。"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "rede_mcp_server.py")
ELF = os.path.join(HERE, "simtest", "demo.elf")


def text_of(result) -> str:
    parts = []
    for c in result.content:
        if hasattr(c, "text"):
            parts.append(c.text)
    return "\n".join(parts)


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("== 握手成功 ==")
            print("server:", init.serverInfo.name, init.serverInfo.version)
            print("protocol:", init.protocolVersion)

            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("\n== 工具清单 (%d 个) ==" % len(names))
            for t in tools.tools:
                desc = (t.description or "").strip().splitlines()[0][:48]
                print("  - %-22s %s" % (t.name, desc))

            print("\n== 调用 rede_sim ==")
            r1 = await s.call_tool("rede_sim", {"symbol_file": ELF})
            print(text_of(r1)[:400])

            print("\n== 调用 rede_break_insert ==")
            r2 = await s.call_tool("rede_break_insert", {"spec": "compute"})
            print(text_of(r2)[:300])

            print("\n== 调用 rede_resume(continue) ==")
            r3 = await s.call_tool("rede_resume", {"mode": "continue", "timeout_s": 8})
            print(text_of(r3)[:400])

            print("\n== 调用 rede_backtrace ==")
            r4 = await s.call_tool("rede_backtrace", {})
            print(text_of(r4)[:400])

            print("\n== 调用 rede_probe(真机 30253) ==")
            r5 = await s.call_tool("rede_probe", {"host": "192.168.0.100", "port": 30253})
            print(text_of(r5)[:400])

            print("\n== 调用 rede_probe(真机 1234) ==")
            r6 = await s.call_tool("rede_probe", {"host": "192.168.0.100", "port": 1234})
            print(text_of(r6)[:400])

            print("\n== 调用 rede_tcf_hello(真机 1534) ==")
            r7 = await s.call_tool("rede_tcf_hello", {"host": "192.168.0.100", "port": 1534})
            print(text_of(r7)[:600])


if __name__ == "__main__":
    asyncio.run(main())
