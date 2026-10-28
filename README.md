# ReDe 调试 MCP Server（POC）

让 AI 直接使用锐华 ReDe 内置调试能力的 MCP 服务。**不写 Eclipse 插件，不改 IDE 安装目录。**

两条落地路线：

| 路线 | 做法 | 状态 |
|---|---|---|
| **A · 自建 MI 客户端** | MCP 直接拉起 `redb.exe`（模拟器 / 任意 gdbserver） | ✅ 已通，40 个工具 |
| **D · MI 中间人（shim）** | IDE 照常点调试，把 IDE 拉起的 "GDB" 换成 shim；shim 原样转发给真 redb，同时开控制口给 AI | ✅ 真板闭环（借 IDE 会话） |

> **路线 D 正是「AI 不自建调试服务器，只让 IDE 起服务，AI 代替人操作、代替人接收信息」。**

---

## 0. 安装与接入

**先说清形态**：这不是 Eclipse 插件（不用装 `.jar`、不用改 IDE），而是一个 **MCP Server**（stdio 协议）。
所以"安装"分两半：

| 半边 | 内容 | 代价 |
|---|---|---|
| **MCP 侧** | 在客户端的 `mcp.json` 里声明一条 `command` + `args` | 一段 JSON，声明即装 |
| **shim 侧**（仅路线 D） | 把 `shim/bin/redb_shim.exe` 放到本地，再把 `.launch` 的 `DEBUG_NAME` 指向它 | 改一个字段 + 点一次调试 |

只跑路线 A（自建 redb / 模拟器）**只需要 MCP 侧**，不用碰 shim。

**环境要求**：Windows（`redb.exe`/`redb_shim.exe` 都是 cygwin 程序）、Python ≥ 3.9、`mcp>=1.30,<2`。

### 0.1 方式一：clone（推荐，功能最全）

```bash
git clone https://github.com/2750733897/rede-mcp.git
cd rede-mcp
python -m pip install -r requirements.txt
python selftest_sim.py      # 模拟器端到端自检，不需要板子
```

客户端配置（WorkBuddy / Claude Desktop / Cursor 等同构，改路径即可）：

```json
{
  "mcpServers": {
    "rede-debug": {
      "command": "python",
      "args": ["D:/path/to/rede-mcp/rede_mcp_server.py"],
      "env": {
        "REDE_ROOT": "D:/guangxin7045/6.1.1-ARM",
        "REDB_EXE": "D:/guangxin7045/6.1.1-ARM/tools/debug/arm/redb.exe"
      }
    }
  }
}
```

### 0.2 方式二：从 git 直接装（不用 clone 全仓）

```bash
pip install "git+https://github.com/2750733897/rede-mcp.git"
rede-mcp                     # 装出的控制台命令，stdio 直跑
```

```bash
uvx --from "git+https://github.com/2750733897/rede-mcp.git" rede-mcp
```

> ⚠️ pip/uvx 只装 MCP 服务本体，`shim/` 不在 wheel 里。**走路线 D 必须 clone**（或用 0.3 的 Release 单独拿 exe）。

### 0.3 方式三：只拿 shim 二进制

到 [Releases](https://github.com/2750733897/rede-mcp/releases) 下 `redb_shim.exe`，
放到任意固定目录，再把它设成 `.launch` 的 `DEBUG_NAME`（做法见 §8.3）。
shim 靠 `REDB_REAL` 环境变量或"同目录 `redb_real.exe`"找到真 redb。

### 0.4 两个必配环境变量

| 变量 | 含义 | 默认 |
|---|---|---|
| `REDE_ROOT` | ReDe 安装根 | `D:\guangxin7045\6.1.1-ARM` |
| `REDB_EXE` | 真 `redb.exe` 全路径 | `$REDE_ROOT\tools\debug\arm\redb.exe` |

其余可选项：`REDE_HOST` / `REDE_PORT`（默认 `192.168.0.100:30253`）、`REDE_SHIM_PORT`、
`REDE_SHIM_CTRL_DIR`、`REDB_REAL`、`REDB_SHIM_CWD`、`REDB_SHIM_NO_WATCHDOG`。

---

## 1. 为什么能这么做

ReDe 的"内置调试"并不是私有黑箱，分层如下（**已实测确认**）：

| 层 | 实体 | 端点 | 说明 |
|---|---|---|---|
| IDE 前端 | Eclipse 3.6.1 + CDT 7.0.2 | — | 断点图标、源码高亮、变量视图 |
| 调试后端 | `tools/debug/arm/redb.exe` | — | **GNU gdb 7.4.1** 锐华定制，`--target=arm-elf`，完整 **MI2** |
| 桥接层 | `GDBBackendForTCF`（Java） | IDE 本机 **30253** | 自己实现 gdbserver，把 GDB RSP 翻译成 TCF |
| 目标通道 | TCF agent | 板子 **1534** | Eclipse TCF 协议，14 个服务 |
| 目标系统 | ReWorks | — | 由 `CLBInit(...)` 拉起（`usrArgs.h: CLB_NETWORK_SERV_PORT=1534`） |

```
AI ──MI2──> redb.exe ──RSP──> [IDE 代理:30253] ──TCF──> [板子 agent:1534]
IDE ─MI2──> redb.exe ──RSP──>         同上

路线 D（shim）：
IDE ──MI2──> redb_shim.exe ──MI2──> redb.exe ──RSP──> [IDE 代理:30253] ──TCF──> [板子 agent:1534]
                    ↑
                AI 经 127.0.0.1:30265 注入 MI / 读流水
```

关键点：

1. `redb.exe` 是标准 GDB，MI2 完整可用。IDE 的 `FinalLaunchSequence` 继承
   `org.eclipse.cdt.dsf.gdb.launching.GdbLaunch`，**IDE 自己也只是 redb 的一个 MI 客户端**。
2. 板上跑的是 **Eclipse TCF agent**（不是锐华私有协议）。连上 1534 会主动推送
   `E / Locator / Hello / ["MemAnalyzer","DynamicModule","SymbolTable","TargetViewer",
   "Registers","Memory","Breakpoints","RunControl",...]`。
3. **30253 不是板子的端口**，是 IDE 进程在本机开的 gdbserver 代理端口
   （`GDBBackendForTCF` 里有 `server`/`clientPort`/`createConnection`，并构造
   `qSupported:PacketSize=3fff;...;QNonStop+` 应答）。IDE 不跑调试会话，这个端口就不存在。

验证证据（实测）：

```
$ redb.exe --version
GNU gdb (GDB) 7.4.1 (ReDe-special support n32)

$ printf -- '-break-insert UserInit\n' | redb.exe --interpreter=mi2 --nx
^done,bkpt={number="1",addr="0x00206774",func="UserInit",
            file="../../usrInit.c",line="41", ...}

$ rede_tcf_hello -> frame_type=E service=Locator command=Hello services=14
```

---

## 2. 文件

| 文件 | 作用 |
|---|---|
| `rede_mcp_server.py` | MCP 服务本体（MI2 客户端 + 40 个工具） |
| `pyproject.toml` / `requirements.txt` | 打包与依赖（`pip install` / `uvx` 用） |
| `shim/redb_shim.py` | **路线 D**：MI 透明中间人（源码版） |
| `shim/bin/redb_shim.exe` | 同上，PyInstaller onefile 打包（8,418,189 B / build 2026-10-27.5），给 IDE 用 |
| `shim/hello_ai.launch` | 启动配置模板，`DEBUG_NAME` 指向 shim（其余照抄 `hello.launch`） |
| `shim/test_shim.py` / `t_exe.py` / `t_exe2.py` | shim 机制验证（源码版 / 打包版 / `mi_wait` 语义） |
| `shim/t_takeover.py` | 演练：本脚本扮演 IDE 拉起 shim，再用 `takeover.py` 接管 |
| `shim/t_port_escalate.py` | 验证控制口 30265..30284 独占绑定与实例登记 |
| `shim/t_orphan.py` | 验证孤儿回收：只杀 bootloader（不打进程树）与正常关 stdin 两条路径 |
| `shim/t_bp_churn.py` | 断点反复增删压测（插入/禁用/启用/删除 × N 轮，校验回到基线） |
| `shim/t_multi.py` | 多会话压测（同时起多个实例，校验端口升级与"选最新"） |
| `shim/t_concurrent.py` | 并发注入压测（多客户端同时写同一控制口） |
| `shim/t_struct.py` | 真机验证 5 个结构化工具（snapshot/eval/read_mem/disasm/break），跑完自动恢复运行 |
| `shim/reap_session.jsonl` / `orphan_session_*.jsonl` | 上述压测的独立日志（不污染主日志） |
| `shim/takeover.py` | 接管脚本（自动发现控制口，不依赖 MCP 会话） |
| `shim/ps_tree.py` | 列 shim/redb/ReDe 的 pid、ppid、启动时间（多会话排查） |
| `shim/make_launch.py` | 由原始 `hello.launch` 派生 `hello_ai.launch`（只改 DEBUG_NAME） |
| `selftest_sim.py` | 模拟器端到端自检（断点/单步/变量/内存/寄存器/反汇编） |
| `mcp_client_test.py` | 通过官方 SDK 的 stdio 客户端验证握手与工具调用 |
| `simtest/demo.c` | 验证用裸机程序（无 libc，`-gdwarf-2` 编出） |
| `board_shell.py` | 经 `localhost:9999` 串口桥在 `reworks>` shell 执行命令 |
| `tcf_frame_probe*.py` | TCF 帧格式对照实验（路线 A2 的起点） |

MCP 配置写在客户端的配置文件里（WorkBuddy 为 `~/.workbuddy/mcp.json`，服务名 `rede-debug`；
其他客户端见 §0.1）。

---

## 3. 工具清单

| 工具 | 作用 |
|---|---|
| `rede_probe` | 探测端口是否为 GDB stub（只发 `qSupported`，不打断目标） |
| `rede_tcf_hello` | 探测板上 TCF agent（1534），返回服务清单与调试能力判断 |
| `rede_attach` | 连板上 gdbserver（host/port/symbol_file/non_stop） |
| `rede_sim` | 用 redb 内置 ARM 模拟器跑，无需板子 |
| `rede_load_symbols` | 不重连、只换符号文件 |
| `rede_status` | 会话状态、最近停机原因、断点表、任务列表 |
| `rede_break_insert` / `_list` / `_delete` / `_enable` | 断点增删查启停，支持条件与忽略次数 |
| `rede_resume` | continue / step / next / stepi / nexti / finish / until |
| `rede_interrupt` | 暂停（相当于 IDE 暂停按钮） |
| `rede_backtrace` / `rede_locals` / `rede_args` | 调用栈、局部变量、函数参数 |
| `rede_eval` | 求值 C 表达式（变量、解引用、强转） |
| `rede_struct` | 展开结构体/数组成员 |
| `rede_read_memory` / `rede_write_memory` | 读（hex+ASCII 双栏）/ 写内存 |
| `rede_registers` | 读全部寄存器 |
| `rede_threads` / `rede_thread_select` | 任务/线程列表与切换（任务级调试） |
| `rede_disassemble` | 反汇编（按符号或地址） |
| `rede_console` | 任意 GDB 控制台命令（含锐华定制 `reworks_load_symbol_file`） |
| `rede_detach` | 恢复运行并断开 |
| **`rede_shim_list`** | 列出所有 shim 实例（pid/端口/启动时间/build），多会话时先看这个 |
| **`rede_shim_status`** | 查 shim 是否在线、真 redb 路径、日志落盘位置 |
| **`rede_shim_tail`** | 读最近 N 条 MI 流水（AI 代替人"看调试窗口"） |
| **`rede_shim_mi`** | 注入一条 MI 命令，只返回该命令之后新产生的记录 |
| **`rede_shim_resume`** | 语义化继续/单步/暂停（continue/step/next/stepi/nexti/finish/interrupt），**轮询等到真停下**再返回，带 `stopped`/`stop_line` |
| **`rede_shim_interrupt`** | 暂停正在运行的目标（等价 IDE 暂停按钮）。目标在跑时所有读取都会报 `Target is executing.`，先调这个 |
| **`rede_shim_is_stopped`** | 读数据前的先决条件：判断目标是否已停下，未停会给 advice |
| **`rede_shim_step_line`** | 确保停下后单步一行（next/step），返回停机位置 |
| **`rede_shim_snapshot`** | **接手会话的第一步**：一次读全现场，返回结构化 `thread/frames/locals/regs/breakpoints`；目标在跑会自动暂停 |
| **`rede_shim_eval`** | 求值 C 表达式，返回 `{value, addr, symbol}`（`g_counter` / `p.x` / `$sp`） |
| **`rede_shim_read_mem`** | 读内存（默认 32B，上限 4KB），返回 hex + ascii；address 支持 `0x…` / `&sym` / `sym` |
| **`rede_shim_disasm`** | 反汇编（spec=`pc` / 函数名 / `&sym` / `0x…`），返回 `{address,func,offset,inst}` |
| **`rede_shim_break`** | 下断点（函数名 / `file.c:123` / `*0x…`），`temporary=True` 为一次性断点 |

---

## 4. 典型会话

### 4.1 主路径：借用 IDE 的会话（路线 D，推荐）

前提：IDE 已用 `hello_ai.launch` 起了调试（见 8.3）。

```
1. rede_shim_list()                        # 有几个会话？选最新那个
2. rede_shim_snapshot()                    # 一次读全：线程/栈/局部量/PC-SP-LR/断点
   （目标在跑会自动暂停，不用先按暂停）
3. rede_shim_break("pl_bram_rw_echo_proof")# 下断点（semantic，返回断点号）
4. rede_shim_resume("continue")            # 轮询等到真停下，不是盲等
5. rede_shim_eval("req->len")              # 看变量
6. rede_shim_read_mem("0x7aa00000", 64)    # 看 PL 共享窗口
7. rede_shim_disasm("pl_bram_rw_echo_proof", 12)
8. rede_shim_resume("continue")            # 收尾让板子继续跑
```

需要裸 MI 时用 `rede_shim_mi("-xxx")` 兜底；每一步 IDE 界面都会同步刷新。

### 4.2 备用路径：自建 redb 会话（IDE 未开调试时）

```
1. rede_probe(host, port)                  # 先确认目标有没有 gdbstub
2. rede_attach(symbol_file=".../hello.obj") # 连上，默认停住目标
3. rede_break_insert("pl_bram_rw_echo_proof")
4. rede_resume("continue", timeout_s=30)
5. rede_backtrace()
6. rede_locals()
7. rede_eval("buf[0]")
8. rede_detach(resume=True)                 # 让板子继续跑
```

> 两条路径**互斥**：自建会话会占掉目标那唯一的客户端槽位（1534 / 30253 都只认一个）。
> IDE 在调试时只能走 4.1。

---

## 5. 自测结果（内部自测 / 回归基线）

模拟器全链路，`selftest_sim.py` 与 `mcp_client_test.py` 均通过：

| 能力 | 结果 |
|---|---|
| 握手与工具枚举 | 40 个工具，protocol 2025-11-25 |
| 加载符号 + 下断点 | `compute` → `0x00008040`，带 `demo.c:26` 与完整路径 |
| 继续到断点 | `reason=breakpoint-hit`，帧 `compute@demo.c:26` |
| 调用栈 | 2 帧：`compute` ← `main`，行号正确 |
| 局部变量 | `p=0x7e8(struct point*)`、`s=0`、`i=0` |
| 单步 | 连续 `next` 走到 29/30/31 行，`s` 由 0 变 7 |
| 结构体展开 | `p.x=7`、`p.y=9` |
| 寄存器 | 26 个，`r0=0x7` 符合 `add` 参数 |
| 读内存 | `&g_array` 读到 `0b 00 00 00 16 ...` = 11/22/33/44 |
| 反汇编 | `push {r11,lr}` … 6 条 ARM 指令 |
| 回归（新增 shim 后重跑） | `selftest_sim.py` 17 步全通"无异常"；`mcp_client_test.py` 握手 ok、40 工具、远端调用正常 |

---

## 6. 踩过的坑（都已修，换机器会再遇到）

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有 MI 结果字段为空 | MI 的 `k=v` 首段没有前导逗号，解析器漏了 | `_parse_results` 支持首段 |
| `-stack-list-frames -no-args` 报 Usage | GDB 7.4 没有该选项 | 改 `-stack-list-frames 0 N` |
| `-stack-list-variables --frame 0` 报 Usage | 同上，7.4 无 `--frame` | 先 `-stack-select-frame` 再取 |
| `-data-disassemble -c 6` 报 Unknown option | 7.4 只支持 `-s/-e` 区间 | 用符号解析地址后算 `end` |
| 断点没有行号、变量看不到 | 工程编出了 **DWARF 4/5**，GDB 7.4 只认 **DWARF 2** | 编译加 `-gdwarf-2` |
| `target sim` 直接崩 | arm-elf sim 必须给内存映射 | `target sim --memory-region 0x0,0x1000000` |
| 目标在跑时后续命令全超时 | MI 全停模式下命令会排队 | 超时提示里给出 `rede_interrupt` 引导 |
| `-data-list-register-values x pc` 返回 `number="0"` | 该命令只吃**寄存器编号**，`pc` 被当成 0 | 用 `-data-evaluate-expression "$pc"`（得到 `0x8040 <compute+16>`） |
| 本机端口没监听却报 timeout 而非 refused | Windows 对未绑定端口会丢 SYN | 判定"没起"要把 refused / timeout 归一到一起 |
| `.launch` 改完 ReDe 里不生效 | 换行必须是 CRLF，且属性值逐字节一致 | `make_launch.py` 由原始 launch 派生，只改 1 行 |

**最重要的一条**：符号文件必须是 DWARF 2。ReDe 自己编出来的 `.obj` 就是 DWARF 2，
所以直接用它没问题；自己用命令行编测试程序时要显式 `-gdwarf-2`。

---

## 7. 真机链路：现状与结论

### 实测（IDE 未开/已开 两种状态都测了）

| 端口 | 归属 | IDE 未开调试 | IDE 开着调试 | 说明 |
|---|---|---|---|---|
| `1534` | 板子 TCF agent | 通，主动推 14 个服务 | **不再推问候**，连接被忽略 | **单客户端**，被 IDE 占住 |
| `30253/UDP` | **IDE 本机** gdbserver 代理 | 不存在 | 监听，但只服务 IDE 自己的 redb | **单客户端**，`clientAddr/clientPort` 只有一个 |
| `21` / `23` | 板上 FTP / telnet shell | 通 | 通 | 与调试无关 |

关键实测记录：

```
# IDE 未开时
$ rede_tcf_hello(1534) -> E/Locator/Hello, services=14      # 有问候
# IDE 开调试后
$ rede_tcf_hello(1534) -> TimeoutError                       # 无问候，被占用
$ redb -ex "target remote udp:127.0.0.1:30253" -> 挂住无响应   # 代理不理新客户
```

### 由此得出三条硬结论

1. **30253 是 IDE 进程的内部桥**，不是目标端口。它随调试会话生灭，且只认 IDE 自己拉起的那个 redb。
   所以 **A1「借道 IDE」不可行**——抢不到客户端槽位，会话结束端口也没了。
2. **1534 的 TCF agent 是单客户端**。IDE 调试期间 AI 无法接入，反之亦然。
   → **直连路线下 AI 调试与 IDE 调试互斥**。
3. 因此**直连路线只剩 A2（直连 TCF）**，且必须在 IDE 未开调试时使用。
4. **单客户端约束反过来是路线 D 的立足点**：既然只有一个槽位，那就让 IDE 把槽位占住，
   但把"操作者"换成可插拔的——shim 坐在 IDE 与 redb 之间，IDE 与 AI 用的是**同一条**底层连接，
   不争抢槽位。这就是第 8 节。

### 已探到的 TCF 线格式（A2 的起点）

连接 1534 后 agent 主动推送：

```
45 00 4c 6f 63 61 74 6f 72 00 48 65 6c 6c 6f 00 5b ... 5d 00 03 01
E  \0 L o c a t o r \0 H e l l o \0 [<service list>] \0 ETX ..
```

即：`<type:1>` + `token\0` + `service\0` + `command\0` + `args\0` + `\x03\x01`
（问候帧的 token 为空，所以紧跟一个 `\0`；周期事件形如 `E\0Locator\0peerHeartBeat\0"TCP:..."\0\x03\x01`）

**尚未验证**：命令帧（`C`）的确切写法与 args 终止符；这需要 agent 空闲时才能试。
IDE 侧参考实现：`rse/eclipse/plugins/org.eclipse.tm.tcf.core_0.3.0.201503181024.jar`
（`StreamChannel` 里有 `ESC` / `bin_data_size`，`AbstractChannel` 负责消息编解码）。

### 下一步

需要在 **IDE 停止调试**（agent 释放 1534）时，用 `tcf_frame_probe*.py` 继续确认命令帧格式，
再实现 `RunControl` / `Breakpoints` / `Registers` / `Memory` 四个服务 + `pyelftools` 符号层。
届时 MCP 里新增一组 `rede_tcf_*` 工具，走 1534，完全不依赖 IDE。

> 这条路线优先级已下调：路线 D 已能用且不需要碰 TCF 线协议，A2 作为"彻底脱离 IDE"的长期选项保留。

---

## 8. 路线 D：MI 中间人（shim）—— 让 IDE 起服务，AI 代替人操作

### 8.1 思路

单客户端约束下，抢槽位走不通。那就**不抢**：让 IDE 照常发起调试，只把 IDE 拉起的那一个
"GDB" 换成一个透明中间人。

```
ReDe ──MI2──> redb_shim.exe ──MI2──> redb.exe ──RSP──> [30253 代理] ──TCF──> 板子
                    │
                    ├── 每条 MI 记录落盘 JSONL（AI 读 = 代替人接收信息）
                    └── 本地控制口 127.0.0.1:30265（AI 写 = 代替人操作）
```

- **对 IDE 完全透明**：IDE 发的每一条 MI 原样转发给真 redb，真 redb 的每一行原样回给 IDE。
  IDE 的断点图标、源码行高亮、变量视图、反汇编窗口全部照常工作。
- **AI 只是"多一个客户端"**，但这个客户端接在 shim 上，不占用目标侧槽位。
  因此**可以做到 IDE 与 AI 同时在线**——人点界面、AI 敲命令，看到的是同一台机器同一份状态。

### 8.2 为什么必须打包成 exe

ReDe 的 `.launch` 只能指定 `org.eclipse.cdt.dsf.gdb.DEBUG_NAME`（可执行文件路径），
**不能追加命令行参数**。所以要"换掉 GDB"只能换文件本身。
`redb_shim.py` 用 PyInstaller `--onefile` 打成 `bin/redb_shim.exe`
（当前 **8,418,189 B / build 2026-10-27.5**）。

> IDE 实际调用它的两种方式（实测已确认，都原样转发）：
> `<shim> --version`（版本探测，一次性）和 `<shim> --interpreter mi2 --nx`（真会话，注意是
> **空格**不是等号）。`--version` 这类探测不占控制口、不登记实例。

shim 找真 redb 的顺序（`DEF_CANDIDATES`）：

1. 环境变量 `REDB_REAL`
2. 同目录 `redb_real.exe`
3. 同目录 `redb.exe`
4. 编译期烧死的 `D:\guangxin7045\6.1.1-ARM\tools\debug\arm\redb.exe`

子进程 **cwd 默认继承 IDE 给的目录**（`REDB_SHIM_CWD` 可覆盖）。不要改成 redb 所在目录：
IDE 可能用相对路径传 `.gdbinit` / `-x`，改了会解析错位；而 `cygwin1.dll` 走
"exe 所在目录优先 + PATH"，与 cwd 无关。

### 8.3 使用步骤

1. 把 `shim/hello_ai.launch` 复制成 ReDe 里可见的启动配置（或直接在 IDE 里
   *Run → Debug Configurations → 导入*）。
2. 确认 `org.eclipse.cdt.dsf.gdb.DEBUG_NAME` =
   `<你 clone 的目录>\shim\bin\redb_shim.exe`（本机实测为
   `D:\guangxin7045\6.1.1-ARM\workspace\_ai_artifacts\rede_mcp\shim\bin\redb_shim.exe`）
3. 其余字段保持与 `hello.launch` 一致（HOST `192.168.0.100`、PORT `30253`、
   `NON_STOP=true`、`PROGRAM_NAME=hello.obj`、`STOP_AT_MAIN_SYMBOL=UserInit`、
   `level=task_level`、`debug_mode=net`）。
4. 点调试。IDE 正常停在 `UserInit`。
5. AI 侧：`rede_shim_status()` 确认在线 → `rede_shim_tail()` 看流水 →
   `rede_shim_mi("-break-insert pl_bram_rw_echo_proof")` → `rede_shim_resume("continue")`。

### 8.4 控制口协议与多会话发现

控制口 = `127.0.0.1:30265` 起，**依次独占绑定**（30265..30284）。不用 `SO_REUSEADDR`：
Windows 允许多个 socket 绑同一端口，`connect` 会固定落到先绑的那个，多会话时 AI 就会
操纵错的目标（这个坑真机踩到过一次）。

绑定成功后写登记文件 `<shim>/bin/ctrl/ctl_<pid>.json`：

```json
{"pid": 38108, "port": 30265, "started": 1793097696.9,
 "build": "2026-10-27.3", "real": "...\\redb.exe", "log": "...jsonl", "cwd": "..."}
```

客户端（`takeover.py` / MCP 的 `rede_shim_*`）按 `started` 倒序挑**最新且能连上**的那个；
旧版 exe 不写登记文件时退回默认 30265。

问答协议（UTF-8 JSON 单行）：

| `op` | 参数 | 返回 |
|---|---|---|
| `state` | — | `{ok, build, pid, port, real, log, records, cwd, args, child_alive}` |
| `tail` | `n` | 最近 n 条 `{i, ts, dir, mi}` |
| `mi` | `cmd` | 注入并立即返回（不等结果） |
| `mi_wait` | `cmd`, `wait` | 注入后等 `wait` 秒，**只返回 `i` 大于注入前序号的记录** |

`dir` 只有三种：`ai>gdb`（AI 注入）、`gdb>ide`（redb 输出给 IDE 的，即 AI 要接收的信息）、
`shim`（自身事件，含 `start real=… args=…`，排障第一现场）。
落盘文件默认 `<shim 目录>/rede_mi_session.jsonl`，可用 `REDB_SHIM_LOG` 覆盖。

#### 进程生命周期（onefile 是两层，这点很关键）

PyInstaller `--onefile` 跑起来是**两层同名的 exe**：

```
redb_shim.exe(bootloader)   <-- IDE CreateProcess 拿到的是这个 pid
  └─ redb_shim.exe(python)  <-- 真正监听控制口、ctl 里登记的是这个 pid
       └─ redb.exe
```

所以「IDE 结束调试」有三条路径，shim 对每条都有应对：

| 路径 | 机制 | 实测退出耗时 |
|---|---|---|
| IDE 正常结束调试（关掉 shim 的 stdin） | `_pump_stdin_to_child` 收到 EOF → 5s 宽限内子进程自退，否则 terminate→kill → `_unregister()` | **0.5s** |
| IDE 崩溃 / 只杀 bootloader（不打进程树） | `_watch_parent` 看门狗：每 2s 用 `OpenProcess+WaitForSingleObject` 查父进程是否已 signaled，没了就收 redb、注销登记、自退 | **1.5s** |
| 目标进程或 redb 自己退出 | stdout 泵结束 → 主线程 `child.wait` → 注销 → 退出 | 同步 |

> 看门狗的判据不能用 `os.getppid() != ppid`：Windows 上进程退出后已记录的 ppid **不会变**，
> 必须开 handle 看它是否 signaled。
> 逃生开关：设 `REDB_SHIM_NO_WATCHDOG=1` 可关掉看门狗（当你用 wrapper 脚本启动、
> 父进程会先退出的场景）。

### 8.5 验证记录（内部自测 / 回归基线）
**A. shim 机制**（`shim/t_exe2.py`，打包版 exe + redb 模拟器后端）：

| 步骤 | 注入 | 返回条数 | 关键记录 |
|---|---|---|---|
| 断点 | `-break-insert compute` | 3 | `^done,bkpt={...addr="0x00008040",func="compute",file="demo.c"}` |
| 运行 | `-exec-run` | 10 | `^running` → `*stopped,reason="breakpoint-hit",...func="compute"` |
| 取栈 | `-stack-list-frames 0 3` | 3 | `^done,stack=[frame={level="0",addr="0x00008040",...}]` |
| 全局 | — | — | IDE 侧累计收到 **2203 字节**；JSONL 落盘 **42 条** |

关键点：每次 `mi_wait` 返回的记录里**只含本条命令之后新产生的行**，不夹带历史——
这是 AI 侧能可靠"对上号"的前提。

**B. 接管脚本**（`shim/t_takeover.py`：脚本扮演 IDE 建会话，再跑 `takeover.py` 各子命令）：

| 子命令 | 结果 |
|---|---|
| `status`（控制口未起） | rc=3，提示「控制口 30265 未监听，IDE 还没用 hello_ai.launch 起调试」 |
| `status`（shim 已起） | `build=2026-10-27.2`、`child_alive=True`、`records=42`、流水含 IDE 侧 `-target-download` 应答 |
| `bp compute` | `^done,bkpt={number="1",addr="0x00008040",func="compute"}` |
| `go` | `^running` → `*stopped,reason="breakpoint-hit",frame={addr="0x00008040",func="compute",line="26"}` |
| `demo` | 断点表 1 行；栈 2 帧（`:26` ← `main`）；局部量 `p=0x7e8(struct point*)`/`s=0`/`i=0`；`$pc="0x8040 <compute+16>"` |
| 收尾 | IDE 侧**全程收到 3127 字节**，透传未中断；JSONL **54 条** |

**C. 真板 + 真 IDE**（2026-10-27 18:41，`hello_ai.launch` 实跑，非自测环境）：

IDE 侧完整走通 `-environment-cd` → `source .gdbinit`（无此文件，报错但不影响）→
`-file-exec-and-symbols hello.obj` → `-gdb-set target-async/non-stop` →
`-target-select remote udp:localhost:30253` → `^connected` → **停在 `UserInit`**。

AI 通过控制口拿到的实测数据：

| 查询 | 结果 |
|---|---|
| `-stack-list-frames 0 3` | 0:`UserInit@0x00206778` `usrInit.c:43`；1:`Init@0x0042d7f4`；2:`thread_handler@0x0044be30` |
| `-thread-info` | `Thread 74995712 'init' PRI:155`（停在 UserInit）、`Thread 70809600 'shell' PRI:170`（running） |
| `-break-list`（IDE 的自动断点已被 GDB 删掉） | `nr_rows="0"` |
| `$pc` / `$sp` / `$lr` | `0x206778 <UserInit+16>` / `0x477ed14` / `0x42d7f4` |
| `-break-insert compute` | `^done,bkpt={addr="0x00008040",...}`（模拟器端点，用于回归） |

**AI 驱动执行**（决定性一条）：

```
[ai>gdb]  -exec-next
[gdb>ide] *stopped,reason="end-stepping-range",frame={addr="0x00206778",func="UserInit",
          file="../../usrInit.c",line="43"},thread-id="1"
[ide>gdb] 21-stack-info-depth --thread 1 2
[ide>gdb] 22-list-thread-groups i1
[ide>gdb] 23-thread-info 1
[ide>gdb] 25-stack-list-frames --thread 1 1 2
```

板子从 `usrInit.c:41` 走到 `:43`，**IDE 自己随即发出一连串刷新命令**——说明
"AI 操作 → 目标变化 → IDE 界面同步跟随"这条闭环在真机上成立，且透传无损。

> 注意：以上是**单次现场记录**，不是覆盖性验证。多会话并发、断点反复增删、
> 目标长时间奔跑后再打断等场景尚未系统测过。

**D. 客户端健壮性（离线演练 + 真机各修一处）**

| 问题 | 现象 | 处理 |
|---|---|---|
| 运行后不知道该等多久 | 固定 `wait` 秒数要么白等、要么错过停机 | `go`/`step` 改为**发命令后轮询 `tail`**，看到 `*stopped` / `=thread-group-exited` / `^error` 才返回（只用 state/mi_wait/tail，旧版 shim 也兼容）；另加 `wait N` 只监听不注入 |
| `Invalid selected thread.` | 目标刚 run 过、线程表变了，AI 的命令靠"当前线程"就失效；真板同理会遇到 | `mi_robust()` 捕获该错误 → `-thread-info` 取一个有效线程 → `-thread-select` → 重试一次 |
| IDE 关调试后 shim 不退出 | 留下"控制口还占着但没人用"的**孤儿进程**（18:53 实测踩到，占着 30266） | stdin EOF 后给子进程 5s 自行退出，否则 `terminate`→`kill`；主流程随后 `_unregister()` 清登记 |
| 目标在跑时读数据 | `^error,msg="Target is executing."`，栈/变量/寄存器全读不到 | 新增 `interrupt`（等价 IDE 暂停按钮）；`demo` 读之前自动 `ensure_stopped()`，本来就在跑就先暂停并明确打印 |

**E. build 2026-10-27.4 真机复验**（18:59 会话，`hello_ai.launch`）

| 项 | 实测 |
|---|---|
| 实例登记 | `takeover.py list` → `pid=46104 port=30265 build=2026-10-27.4`；`ctrl/ctl_46104.json` 存在 |
| 自动发现 | `takeover.py status` 直接选中 46104，无需手工给端口 |
| 暂停 | `-exec-interrupt` → `^done` + `*stopped,reason="signal-received",frame={addr="0x00433810",func="kernel_job_execute_with_schedule"},core="0"`，IDE 随即发 `16-list-thread-groups`、`17-stack-info-depth` |
| 读栈 | 4 帧：`kernel_job_execute_with_schedule` ← `ksem_take` ← `sem_wait2` ← `read_line_ldisc`（shell 任务等信号量读输入，链完全自洽） |
| 读任务 | `Thread 70809600 'shell' PRI:170 state=stopped core=0` |
| 读寄存器 | `$pc=0x433810 <kernel_job_execute_with_schedule+80>`、`$sp=0x437e42c`、`$lr=0x433810` |
| 恢复执行 | `go 6`（可选超时）→ `^running` → 6s 内无停机，按预期超时退出 |
| 退出回收 | 独立实测：关掉 stdin 后**第 1 秒** shim 退出 rc=0，端口释放，`ctl_*.json` 自动清除 |

**F. 多会话并发 & 断点增删压测**（真机会话在跑的同时做的，未打断它）

多会话（`shim/t_multi.py`，真冻结 exe）：

| 步骤 | 实测 |
|---|---|
| 已有真实会话占 30265，再起 2 个 exe | 依次拿到 **30266 / 30267**（连续分配，不冲突） |
| `takeover.py list` | 列出 3 个，按 `started` 倒序 |
| `status` | 选中最新的 30267（不是真实会话）——说明"最新优先"生效，多会话时也能用 `--pid` 指定 |
| 杀掉测试实例后 | 进程退出（约 10s 内），`list` **自动清掉死登记**，`status` 回落真实会话 46104 |

要点：`list`/`status` 的探活是"真连一下"，所以残留登记会自愈；不需要手工清理 `ctrl/`。

断点增删（`shim/t_bp_churn.py`，5 轮，只动不会再命中的 `UserInit`）：

| 每轮步骤 | 结果 |
|---|---|
| `-break-insert UserInit` | 新号 5…9 依次分配，表 2 条 |
| `-break-disable` / `-break-enable` | 均 `^done` |
| `-break-delete` | 表精确回到基线 1 条，新号消失 |
| 5 轮后 | **全通过，会话仍在线**，断点号不复用（GDB 行为） |

**G. 并发注入 & 长跑后打断**（`shim/t_concurrent.py` + 真机会话）

并发（3 客户端 × 5 条 MI，同一条 shim 控制口，1.3s 完成）：

| 指标 | 实测 |
|---|---|
| 写入是否串字节 | 否 —— 15/15 次调用都能看到自己的命令回显，会话 `child_alive=True` |
| 每次拿到的记录数 | 3~9 条 |
| 其中 `^done,value=` 条数 | **1~3 条** ← 关键性质 |
| 压测后 | `-thread-info` 正常，会话未失联 |

> **重要性质**：`mi_wait` 的"只返回本条之后的记录"是按**记录序号**过滤的，不是按命令归属。
> 所以多个客户端并发时，你会看到**别人的**输出。写入安全 ≠ 窗口私有。
> 需要精确归属时：MI 命令自带 token 才行，目前 shim 注入不带 token（避免干扰 IDE 的
> token 记账），所以**建议同一时刻只用一个 AI 客户端**；多人/多 agent 并发时按
> `ai>gdb` 回显行 + 值语义自行判别。

长跑后打断（板子连续运行约 1.9 分钟后）：

| 步骤 | 结果 |
|---|---|
| `interrupt` | `^stopped,reason="signal-received"`，`kernel_job_execute_with_schedule`，IDE 发 `36-list-thread-groups`、`37-stack-info-depth` |
| 读栈 | 5 帧：`kernel_job_execute_with_schedule` ← `ksem_take` ← `sem_wait2` ← `read_line_ldisc` ← `read` |
| 恢复 | `-exec-continue` → `^running`，5s 内无停机（正常态） |

**H. build 2026-10-27.5 孤儿回收**（`shim/t_orphan.py`，独立端口 30291/30292，不碰现场会话）

先确认了进程结构（真机现场 `ps_tree.py`）：

```
ReDe.exe       pid=43708
redb_shim.exe  pid=30364  ppid=40140   <- bootloader，IDE 只认识这个
redb_shim.exe  pid=46104  ppid=30364   <- python 层，监听 30265 的是它
redb.exe       pid=29772  ppid=46104
```

**这条结构决定了风险**：IDE 若只终止自己 CreateProcess 的那个 pid，46104 会变孤儿继续占着
30265。之前 18:53 就踩到过（pid 43816 占着 30266 不撒手）。

| 场景 | 操作 | 结果（连跑 2 次一致） |
|---|---|---|
| **A 只杀 bootloader** | `taskkill /PID <bootloader> /F`（**不加 `/T`**，最恶劣路径） | python 层 **1.5s** 自退；30291 释放；`ctl_<child>.json` 已删除 |
| **B 正常关 stdin** | `p.stdin.close()`（IDE 正常结束调试） | **0.5s** 退出；30292 释放 |

结论：两条路径都能自愈，**不需要人工重启，也不需要手工清 `ctrl/`**——即使真的漏了，
`takeover.py list` 的探活也会把死登记清掉。

**I. 结构化工具真机验证**（`shim/t_struct.py`，现场会话 `pid 46104` / build `.4`）

这一组是给 AI 直接用的：返回 JSON 字段，不必自己啃 MI 文本。

| 工具 | 实测返回 |
|---|---|
| `snapshot(6)` | 目标原在运行 → **自动暂停**；`thread={tid:"70809600", name:"shell", priority:170, core:"0"}`；6 帧 `kernel_job_execute_with_schedule` ← `ksem_take` ← `sem_wait2` ← `read_line_ldisc` ← `read` ← `gl_read_unmasked`；`regs={pc:"0x433810", pc_symbol:"…+80", sp:"0x437e42c", lr:"0x433810"}`；断点表 1 条 `UserInit@usrInit.c:41` |
| `eval("$pc")` | `value="0x433810 <kernel_job_execute_with_schedule+80>"`，另拆出 `addr` 与 `symbol` |
| `disasm("pc", 6)` | 6 条 ARM：`mov r0,r5` / `sub sp,r11,#20` / `ldm sp,{r4,r5,r11,sp,lr}` / `b 0x470944 <int_cpu_unlock>` / `bl 0x433da0 <kernelLockGive>` / `mov r0,r5`，每条带 func+offset |
| `read_mem("0x00206774", 16)` | `hex=8fd704eb00f020e300a89de80dc0a0e1` —— 小端即 `bl` / `nop` / … / `mov r12,sp`，标准 ARM 函数序言，与 `disasm` 对得上 |
| `break("UserInit", temporary=True)` | `number=14, addr=0x00206774, file=../../usrInit.c, line=41, type=breakpoint` |
| 收尾 | 断点已删（`-break-delete 14`）、目标已 `-exec-continue` 恢复运行 |

真机上撞到的三个解析细节（已修）：

1. **`-thread-info` 没有 name/priority 字段**——藏在 `details="'shell' PRI: 170"` 里，
   要正则抠出来；`target-id="Thread 70809600"` 才是 OS 线程号（≠ MI 的 `id="1"`）。
2. **`$lr` 没有符号时 GDB 返回十进制**（`4405264`），必须统一成 `0x` 才好和地址对照。
3. **停在内核模块时所有帧都没有 `file`/`line`**——不是解析失败，加了 `frames_note` 说明，
   免得 AI 误判成"符号没加载"。

### 8.6 现场排障

| 现象 | 判据 | 处理 |
|---|---|---|
| `rede_shim_status` 报「控制口未监听」 | 30265 起都没有监听 | IDE 没走 shim：检查 Debug Configuration 的 GDB 路径 |
| 控制口连上但无应答 | shim 在等 redb | 目标可能在跑，先 `interrupt` 再读 |
| IDE 界面卡在 "Launching" | 看 `…jsonl` 首行 `start real=… args=…` | 核对 IDE 实际传的参数是否被 shim 原样转发 |
| 断点没有行号 / 变量看不到 | 符号表 DWARF 版本 | 见第 6 节，必须 DWARF 2 |
| **AI 操作的是"另一个"会话** | `takeover.py list` 看有几个实例；`state` 里的 `pid` 是否是新会话 | 旧版 exe 不登记实例、且都用 30265，连接固定落到先绑的那个。用新版 exe（30265..30284 独占 + 登记），或 `takeover.py --pid N` 指定 |
| 启动时报 `Non-stop mode requested, but remote does not support non-stop` | 首次 `-target-select` 时代理还没就绪（日志里有 `Ignoring packet error`） | IDE 重试一次即可；这是 IDE/代理的时序问题，与 shim 无关 |
| 关了调试但 30265 还占着 | `takeover.py list` 看看有没有 `build` 是旧版（`.4` 及更早，无看门狗）的残留实例 | 用 build `.5` 及以后的 exe；急用就 `taskkill /PID <pid> /T /F`，或直接 `takeover.py list`（会顺带清掉死登记） |
| 怀疑 `ctrl/` 里登记与实际情况不符 | 登记文件只是"线索"，不是真相 | `list` 会真连一遍，连不上就删登记并跳过；不要手工编辑 `ctrl/` |

