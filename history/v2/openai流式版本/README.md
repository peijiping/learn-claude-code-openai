# 流式版智能体（OpenAI SDK 流式改造）备份说明

> 本目录是从项目根目录归档过来的**流式版实现快照**，也是桌面端后端的核心。
> 这是当前 `agents/` OpenAI SDK 版智能体的早期留档 —— 完整保留了「从 langchain 剥离、改用原生 OpenAI SDK，并统一为流式输出」那一版的关键实现与设计要点。

---

## 这一版是什么

在 v1 → v2 → v2.1 的学习脉络里，这是**自己动手做的那一套 OpenAI SDK 版**，并做了两件关键改造：

1. **剥离 langchain 全部依赖**，改用裸 `openai` 库直调：
   `chat.completions.create(...)` + `response.choices[0].message.tool_calls`，不带任何抽象层。
2. **统一流式（streaming）**：把六个 LLM 调用点从同步 `create()` 收敛到 `streamed_create()`，
   增量事件经 `EventSink` 分发，既能逐字打印到 CLI，也能经 `WSSink` 序列化成 JSON 行协议推给桌面端。

核心心法不变：

> **Agency（感知—推理—行动）来自模型训练，我们构建的是 Harness —— 让模型在特定领域里干活的脚手架。**

---

## 流式架构（本版最有价值的设计）

所有机制都围绕同一个 Agent Loop 叠加，而「流式」是这一版对外最重要的外观。

```
├─ StreamEvent        一条流式增量（thinking_delta / content_delta /
│                     tool_call_start / tool_call_delta / tool_call / turn_end）
├─ EventSink          只负责消费事件，不关心事件从哪来
│   ├─ PrintSink      CLI 增量打印（含预测式工具调用显示）
│   ├─ FilterSink     按事件类型转发（subagent 把工具事件转给 UI）
│   └─ WSSink         把 StreamEvent 序列化成 JSON 行协议推给前端（桌面端接缝）
├─ consume_stream     迭代流式响应：分派增量事件 + 聚合出完整消息
└─ streamed_create    统一流式入口，等价 create() 但走流式
```

**桌面对接铁律**：前端只需把 `WSSink.send_func` 换成 webSocket 发送函数，后端其余代码**零改动**。

### 事件模型（JSON 行协议）

`StreamEvent.to_json()` 输出一行 JSON，字段对齐 OpenAI 增量：

```json
{ "type": "content_delta", "text": "…", "tool_id": "",
  "tool_name": "", "args": "", "finish_reason": "", "usage": {} }
```

**预测式工具调用（P3）**：工具名一出现（首个 name delta）就发 `tool_call_start`，
参数碎片随流发 `tool_call_delta`，拼完再发带完整参数的 `tool_call` —— 前端能在参数还没拼完时先把工具行显示出来。

---

## 代码布局（本快照）

| 模块 | 职责 |
|------|------|
| `agent_full_v2.py` | 智能体引擎（`Agent` 类，多实例支持） |
| `agent_cli.py` | REPL 交互入口 |
| `streaming_client.py` | 流式统一模块（`StreamEvent` / `EventSink` / `PrintSink` / `FilterSink` / `WSSink` / `consume_stream` / `streamed_create`） |
| `llm_manage.py` | 兼容 reasoning 模型的 `OpenAI` 原生客户端封装 |
| `system_prompt.py` | System Prompt 运行时组装 + cache boundary |
| `session_manage.py` | 会话管理（新建 / 切换 / 清空 / 持久化 jsonl） |
| `subagent.py` | 子智能体（隔离上下文） |
| `tools.py` | 工具注册表（`ToolRegistry` 类） |
| `cron_scheduler.py` | Cron 定时调度器 |
| `skills.py` | skill loader |
| `memories.py` | Tool 驱动持久化记忆 |
| `todo_manager.py` | TodoWrite（短清单） |
| `task_manager.py` | 文件式 Task System（依赖图） |
| `background_manager.py` | 后台任务 + 通知队列 |
| `context_compact.py` | 三层上下文压缩 |
| `error_recovery.py` | 错误恢复状态机（429/503 退避 / max_tokens 升级 / 超长压缩） |
| `hooks.py` | Hook 系统（UserPromptSubmit / PreToolUse / PostToolUse / Stop） |
| `check_permission.py` | 权限三闸门 |
| `paths.py` | 所有路径常量单一来源 |
| `worktree.py` / `mcp_manager.py` / `message_bus.py` / `teammate_manager.py` | Worktree 隔离 / 真实 MCP 客户端 / 队友邮箱 / 持久队友 |

> `agents/` 当前版本已在此基础上迭代（目标循环 / 工作流运行时 / 队友协作等），
> 本目录仅作流式版的早期留档；最新实现请以项目根目录 README 为准。

---

## 核心模式：Agent Loop

```python
def agent_loop(messages):
    while True:
        response = streamed_create(llm, sinks=[self.stream_sink], messages=messages, tools=tools)
        msg = response.choices[0].message  # 或聚合出的 StreamedMessage
        messages.append(msg)
        if not msg.tool_calls:
            return
        # 执行工具 → 追加 tool role 消息 → 继续循环
```

每一节、每一个机制（子智能体、技能、任务、后台、队友、压缩、MCP）都是在这个 loop 外面**加一层**。

---

## 安装与运行

```bash
# 后端依赖（含 mcp / openai / websockets）
pip install -r requirements.txt
cp .env.example .env    # 配置 OPENAI_MODEL_ID / OPENAI_API_KEY / OPENAI_BASE_URL

# CLI 入口
python agents/agent_cli.py
```

**REPL 命令**：

| 命令 | 作用 |
|------|------|
| 直接输入 | 跟 agent 对话 |
| `/tasks` | 列出任务看板 |
| `/compact` | 手动压缩上下文 |
| `/newsession` | 开新会话 |
| `/switchsession <id>` | 切到历史会话 |
| `/clearsession` | 清空当前会话 |
| `/q` / `/exit` | 退出 |

---

## 后续演进

这一版是纯 CLI。基于它之上的桌面化改造（`ws_bridge.py` 薄桥 + Electron 前端 + JSON 行协议）见项目根目录 README 与 [`docs/frontend/`](../../docs/frontend) 设计文档。

---

> **Bash is all you need. Real agents are all the universe needs.**
>
> **这不是"抄源码"，是"抓住关键设计，自己造一遍"。**