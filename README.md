# TRAE 复刻桌面端 · 智能体工具

> 从一个「从 0 到 1 学习 Coding Agent Harness」的个人仓库，**转变为一个可运行的桌面智能体工具**。
> 前端做壳，Python 后端做脑：所有 Agent 逻辑、工具执行、流式生成都在后端，前端只负责「渲染 + 交互 + 转发」。

---

## 这个项目是什么

沿 **v1 → v2 → v2.1** 的学习脉络，我自研了一套 **OpenAI SDK 版 Coding Agent**，并统一改造为**流式输出**。
现在把这套智能体能力包装成一个 **Electron 桌面应用**，复刻 TraeWork 式的交互界面。

**核心心法**：

> Agency（感知—推理—行动）来自模型训练，我们构建的是 **Harness** —— 让模型在特定领域里干活的脚手架。

**当前状态**：桌面壳（侧边栏 / 聊天 / 输入区）+ 流式打字机 + 工具调用可视化 + 会话管理已打通，可直接对话。

---

## 已实现能力

- **流式对话**：`thinking_delta`（思考，可折叠）+ `content_delta`（正文，打字机）+ `turn_end`（usage）逐字呈现
- **工具调用可视化**：工具名一出现即预测式展示折叠条，参数随流续写，完成后标注状态
- **会话管理**：任务树（新建 / 切换 / 清空会话，复用后端 `session_*.jsonl`）
- **目标 / 待办 / 技能**：只读面板浅接入（Drawer 抽屉）
- **断线自愈**：Python 子进程拉起监控 + WebSocket 指数退避重连 + 「重新连接」提示
- **Markdown 渲染**：正文支持表格 / 代码块等（`react-markdown` + GFM）

后续增量（模型管理 / 历史搜索 / 插件市场 / 附件多模态 / 账号）见 [开发路线图](docs/frontend/04-开发路线图与后续增量.md)。

---

## 技术框架

```
┌────────────────────────── Electron 桌面端 ──────────────────────────┐
│  渲染进程 (React UI)                                                  │
│    Sidebar(任务) │ ChatPanel(消息流) │ InputBox │ 状态条               │
│    store(Zustand)      hooks(useAgentStream)                        │
│        ▲  contextBridge.invoke('agent:send') / on('agent:event')     │
│   preload.ts（contextBridge 白名单 API，contextIsolation 开启）        │
│        ▲  IPC                                                       │
│  主进程 (Main)                                                       │
│    pythonManager 拉起/监控/重启 Python 子进程                          │
│    agentWS 连后端 WebSocket + 指数退避重连 + 事件转发                   │
└──────────────┬──────────────────────────────────────────────────────┘
               │ 启动 ws_bridge.py（127.0.0.1:8765）
┌──────────────▼──────────────────────────────────────────────────────┐
│  Python 后端（OpenAI SDK，零业务改动）                                  │
│    agents/ws_bridge.py  薄桥：命令进 → Agent 方法调用，事件出 → WSSink  │
│    agents/streaming_client.py  StreamEvent / EventSink / WSSink      │
│    agents/agent_full_v2.py      Agent 引擎（run_turn / 会话 / 目标…）  │
└──────────────────────────────────────────────────────────────────────┘
```

### 技术选型

| 层面 | 选择 | 说明 |
|------|------|------|
| 桌面壳 | **Electron** | 渲染 + 主进程分离 |
| 语言 | **TypeScript** | 编译期类型安全 |
| UI | **React**（Hooks）+ **Zustand** | 数据驱动，贴流式渲染 |
| 构建 | **Vite + electron-vite** | 主 / 预加载 / 渲染三端一体化 |
| 样式 | 原生 CSS 变量（design tokens） | 轻量、无重型框架 |
| 通信 渲染↔主 | **contextBridge + ipcRenderer** | 白名单安全桥 |
| 通信 主↔Python | **WebSocket** + JSON 行协议 | 后端 `WSSink` 天然流式推送 |

> **为什么让主进程连 Python，而不是渲染进程直连？** WebSocket 是 Node 层的事，未来可能处理鉴权 / 重连 / 远端运行；渲染进程保持纯净，只经 `/agent API` 通信。

---

## 目录结构

```
learn-claude-code-main/
├── agents/                       # 🛠️ Python 后端（OpenAI SDK 流式版）
│   ├── agent_full_v2.py          # ⭐ Agent 引擎
│   ├── agent_cli.py              # CLI 入口（python agents/agent_cli.py）
│   ├── streaming_client.py       # 流式统一：StreamEvent / WSSink / consume_stream
│   ├── ws_bridge.py              # ⭐ 桌面薄桥：命令进、事件出（新增）
│   └── ...tools/skills/mcp_manager/session_manage 等
│
├── frontend/                     # 🖥️ Electron + React 桌面端
│   ├── src/main/                 #   主进程：pythonManager / agentWS / IPC
│   ├── src/preload/              #   contextBridge 白名单
│   ├── src/renderer/src/
│   │   ├── components/           #   Sidebar / Chat / SettingsPanel / common
│   │   ├── store/                #   Zustand：agentStore（事件聚合）/ sidebarStore
│   │   ├── hooks/                #   useAgentStream
│   │   ├── protocols/            #   事件线协议类型 / 解析
│   │   └── styles/               #   design tokens（色板/间距/字体）
│   └── devDeps: electron / electron-vite / vite
│
├── docs/frontend/                # 📐 桌面端设计与开发文档（协议 / 界面 / 路线图）
├── anthropic/                    # ✅ v1 教程（只读）
├── anthropic_v2/                 # ✅ v2 教程（只读）
├── anthropic_v2.1/               # 🚧 v2.1 教程更新版（只读）
├── history/                      # 早期版本留档（流式版备份见 history/v2/openai流式版本/）
├── skills/ mcp_servers/ tests/   # 学习资源 / 本地 MCP 测试 / 单元测试
├── WorkSpace/                    # agent 跑过的实际任务留档
├── requirements.txt              # 后端依赖（含 openai / mcp / websockets）
└── README.md                     # 你正在读这个
```

---

## 安装与运行

### 0. 前置

- **Python 3.13+**
- **Node.js 18+**（含 npm）

### 1. 后端（Python Agent）

```bash
pip install -r requirements.txt
cp .env.example .env     # 配置 OPENAI_MODEL_ID / OPENAI_API_KEY / OPENAI_BASE_URL
```

> 桌面端会自动拉起仓库根 `.venv/bin/python agents/ws_bridge.py`；若你没有 venv，请手动安装后端依赖后再运行（见上文）。

### 2. 桌面端（Electron）

```bash
cd frontend
npm install
npm run dev
```

> **国内网络**：若 Electron 二进制下载超时，设镜像后重装：
> `ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/" npm install`

`npm run dev` 会打开桌面窗口，主进程自动拉起 Python 桥并接通 `ws://127.0.0.1:8765`。
底部状态条显示连接状态；断线会自动重连。

### 3. 纯 CLI（不装桌面端时）

```bash
python agents/agent_cli.py
```

REPL 命令：直接输入对话；`/tasks` 任务看板 · `/compact` 压缩 · `/newsession` 新会话 · `/switchsession <id>` 切会话 · `/clearsession` 清空 · `/q` 退出。

---

## 一次对话的数据流

```
用户在输入框敲一句话
  → React InputBox → store.send(text)
  → preload window.agent.send(text)
  → 主进程 ipc 'agent:send' → agentWS.send({kind:'chat', text})
  → Python ws_bridge → agent.run_turn(text)
  → WSSink 逐条 emit：thinking_delta / content_delta / tool_call_* / turn_end
  → 主进程转发 → useAgentStream 写进 agentStore
  → MessageList 因状态更新自动重渲染，界面逐字刷新
```

> 增量事件**只 append 不覆盖**，React 每次仅追加一小段文本，形成打字机效果。

---

## 通信协议（简）

同一条 WebSocket 上用 `kind` 区分消息：
- **事件（后端→前端）**：`{kind:'event', payload:{type:'content_delta', text:'…'}}`，类型与后端 `StreamEvent` 一一对应
- **命令（前端→后端）**：`chat` / `session_new` / `session_switch` / `session_clear` / `sessions_list` / `goal_status` / `tasks` / `skills`

完整协议见 [docs/frontend/03-前后端通信协议.md](docs/frontend/03-前后端通信协议.md)。

---

## 设计文档索引（`docs/frontend/`）

| 文档 | 内容 |
|------|------|
| [00-README.md](docs/frontend/00-README.md) | 目录导航 + 术语对照 |
| [01-技术框架设计.md](docs/frontend/01-技术框架设计.md) | Electron 进程模型 / 选型 / 前后端边界 |
| [02-界面功能设计.md](docs/frontend/02-界面功能设计.md) | 三栏布局 / design tokens / 组件树 |
| [03-前后端通信协议.md](docs/frontend/03-前后端通信协议.md) | JSON 行协议 / IPC 通道 / 薄桥设计 |
| [04-开发路线图与后续增量.md](docs/frontend/04-开发路线图与后续增量.md) | P0–P4 分期 / 后续增量 |

---

## 开发路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | electron-vite 骨架 + 三栏空布局 | ✅ |
| P1 | TraeWork 静态界面复刻（design tokens） | ✅ |
| P2 | 后端桥 + 流式打字机主链路 | ✅ |
| P3 | 工具调用可视化 + 断线重连 + 会话操作 | ✅ |
| P4 | 目标 / 待办 / 技能浅接入 | ✅ |
| 后续 | 模型管理 / 历史搜索 / 插件市场 / 附件 / 账号 | 🚩 增量 |

---

## 学习脉络（仓库由来）

这不是单纯的工程，而是一条学习轨迹：跟着 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 从 v1 → v2 → v2.1，**用 OpenAI SDK 自研复刻** Coding Agent 的核心机制。教程代码（`anthropic*`）只读留档，自己的实现（`agents/` + `frontend/`）持续演进。流式版早期快照见 [`history/v2/openai流式版本/`](history/v2/openai流式版本)。

---

> **Bash is all you need. Real agents are all the universe needs.**
>
> **这不是"抄源码"，是"抓住关键设计，自己造一遍"。**