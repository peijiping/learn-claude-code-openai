# full_subagent_langchain.py: LangChain 版完整子智能体入口

说明对象: [`agents/full_subagent_langchain.py`](../../agents/full_subagent_langchain.py)

> *"主对话负责决策和收口, 子智能体负责隔离执行"* -- 复杂任务进入独立上下文, 父智能体只接收摘要结果。

## 定位

`full_subagent_langchain.py` 不是 `s01-s12` 中的单课最小示例, 而是一个整合版主入口。它把几个机制接到同一个 LangChain 循环里:

- 会话持久化: `.chathistory/session_N.jsonl`
- 会话级 todo 看板: `.todo/session_N.json`
- 通用型 `sub_agent` 分发
- 后台任务通知注入
- 上下文压缩
- Skill 描述列表与按需加载
- 并行工具调用

它适合用来观察这些机制在一个真实主循环里如何配合, 而不是只看某一课的孤立实现。

## 问题

单文件课程示例便于学习一个机制, 但真实 coding agent 往往要同时处理几类压力:

1. 对话要能恢复, 不能每次启动都丢上下文。
2. 复杂任务要有 todo 状态, 否则长流程容易跑偏。
3. 大量文件读取、搜索和实现工作要隔离到子智能体里, 避免污染父上下文。
4. 慢命令要能后台运行, agent 不能一直阻塞等待。
5. 工具结果必须按 LangChain / OpenAI tool calling 格式回填, 否则下一轮调用会失败。

`full_subagent_langchain.py` 的价值就在于把这些运行时边界放在一个主循环中统一调度。

## 解决方案

```
User input
    |
    v
+-----------------------------+
| main()                      |
| - load / create session     |
| - handle slash commands     |
+-------------+---------------+
              |
              v
+-----------------------------+
| agent_loop()                |
| - drain background results  |
| - maybe compact context     |
| - invoke LangChain LLM      |
| - execute tool calls        |
+-------------+---------------+
              |
      +-------+--------------------------+
      |                                  |
      v                                  v
+-------------------+          +-----------------------+
| normal tools      |          | sub_agent tool        |
| TOOL_HANDLERS     |          | run_subagent(prompt)  |
+-------------------+          +-----------------------+
      |                                  |
      +---------------+------------------+
                      |
                      v
              ToolMessage results
                      |
                      v
              next LLM turn
```

父智能体只保留主决策链路。子智能体内部可以多轮调用工具, 但最终只把摘要作为 `sub_agent` 的工具结果返回。

## 工作原理

### 1. 系统提示定义运行规则

`SYSTEM` 是这个文件的控制中心。它不仅告诉模型工作目录, 还明确了几类行为规则:

- 上下文保护: 不直接 `cat` 二进制文件, 不一次读取大文件, 工具输出要限量。
- `sub_agent` 强制场景: 读取多个文件、搜索代码库、执行多步骤操作、实现功能、设计方案等。
- todo 看板使用规则: 新任务建新看板, 更新时传完整 items, 同一时间只保留一个 `in_progress`。
- 子智能体工具范围: 通过 `allowed_tools` 可以把子智能体限制为只读工具集。
- 并行执行规则: 独立工具调用可设置 `parallel=true`。
- Skill 列表: 通过 `SKILL_LOADER.get_descriptions()` 把可用技能的低成本描述写入系统提示。

这层提示词让模型先学会"什么时候该分发、什么时候该记录进度、什么时候该压缩"。

### 2. 父智能体绑定 PARENT_TOOLS

```python
llm_with_tools = create_llm_with_tools(PARENT_TOOLS)
```

`PARENT_TOOLS` 来自 `tools.py`, 包含:

- 基础工具: `bash`, `read_file`, `read_pdf`, `write_file`, `edit_file`
- 会话 todo 工具: `todo_new_board`, `todo`
- Skill / 后台任务工具: `load_skill`, `background_run`, `check_background`
- 子智能体入口: `sub_agent`

注意: `sub_agent` 只在父智能体工具集中出现。子智能体使用 `CHILD_TOOLS_SUBAGENT`, 不包含 todo 工具, 也不能递归创建新的 `sub_agent`。

### 3. 子智能体由 subagent.py 执行

当模型调用 `sub_agent` 时, 主循环不会直接执行 prompt, 而是转给 `run_subagent()`:

```python
if tool_name == "sub_agent":
    allowed_tools = tool_args.get("allowed_tools")
    tool_output = run_subagent(tool_args["prompt"], allowed_tools=allowed_tools)
```

`run_subagent()` 位于 [`agents/subagent.py`](../../agents/subagent.py)。它会创建一组新的 `sub_messages`, 绑定子智能体工具, 循环调用模型和工具, 最后只返回摘要文本。

这种边界带来两个效果:

- 父上下文不会被大量搜索结果、文件内容和命令输出撑爆。
- 子任务结束后, 父智能体拿到的是可继续决策的摘要, 而不是完整执行日志。

### 4. 工具按 parallel 分组执行

LLM 一轮可能返回多个工具调用。主循环会按 `parallel` 参数拆成两组:

```python
if tool_call["args"].get("parallel", False):
    parallel_calls.append(tool_call)
else:
    sequential_calls.append(tool_call)
```

并行组使用 `ThreadPoolExecutor` 执行, 串行组按顺序执行。适合并行的典型场景是多个只读搜索、多个互不依赖的子智能体分析。涉及同一文件写入、依赖前一步输出的任务应保持串行。

### 5. ToolMessage 保持 LangChain 对话合法

工具结果不会作为普通文本追加, 而是按每个 `tool_call_id` 生成 `ToolMessage`:

```python
tool_msg = ToolMessage(content=tool_content, tool_call_id=tc["id"])
history_messages.append(tool_msg)
session_manager.append_message_to_session(session_file, tool_msg)
```

这是 LangChain / OpenAI tool calling 的关键约束: 每个 assistant tool call 后面必须有对应的 tool result。否则下一轮模型调用可能因为消息结构不合法而报错。

### 6. 会话和 slash command 在 main() 中处理

`main()` 负责 CLI 交互和会话生命周期:

| 命令 | 作用 |
|------|------|
| `/newsession` | 创建新的会话文件和 todo 看板 |
| `/switchsession <数字>` | 切换到已有会话 |
| `/clearsession` | 清空当前会话历史 |
| `/todo` | 渲染当前会话 todo 看板 |
| `/compact` | 手动触发上下文压缩检查 |
| `/tasks` | 提示当前入口已改用会话级 todo |
| `/q`, `/exit`, 空输入 | 退出 |

普通用户输入会被追加为 `HumanMessage`, 写入当前 session 文件, 然后进入 `agent_loop()`。

## 调用链速查

| 关注点 | 代码位置 | 说明 |
|--------|----------|------|
| 主入口 | `main()` | 初始化会话、处理 CLI 命令、接收用户输入 |
| 主循环 | `agent_loop()` | 调用 LLM、执行工具、写回工具结果 |
| 上下文压缩 | `session_manager.maybe_compact_context()` | 调用 `SessionManager.compact_manager` 检查和压缩 |
| 工具执行 | `_execute_tool_call()` | 分发普通工具或 `sub_agent` |
| 子智能体 | `run_subagent()` | 在独立消息上下文中执行子任务 |
| 工具注册 | `tools.py` | 定义 `PARENT_TOOLS`, `CHILD_TOOLS_SUBAGENT`, `TOOL_HANDLERS` |
| 模型创建 | `llm_manage.py` | 使用 `ChatOpenAI.bind_tools()` 绑定工具 |

## 适合观察的设计点

1. **父子上下文隔离**: 父智能体只看到子智能体摘要, 不继承子智能体内部工具历史。
2. **主控 todo 边界**: 子智能体不能操作 todo, 避免多个执行者同时改同一个会话看板。
3. **只读子任务限制**: `allowed_tools=["bash","read_file","read_pdf"]` 可以把探索类任务限制为只读。
4. **并行与串行共存**: 同一轮工具调用可部分并行、部分串行。
5. **后台通知注入**: 后台任务完成后, 结果会在下一轮 LLM 调用前以 `<background-results>` 注入。
6. **会话恢复**: 历史消息和 todo 看板都按 session 编号保存, 便于切换和继续。

## 试一试

```sh
cd learn-claude-code
python agents/full_subagent_langchain.py
```

运行前需要在 `.env` 中配置 LangChain OpenAI 兼容接口:

```sh
OPENAI_MODEL_ID=...
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
```

可以尝试这些 prompt:

1. `只读方式分析 agents 目录的主要模块职责，并用 sub_agent 隔离读取文件`
2. `创建一个 todo 看板，规划如何给这个项目补充测试，然后先完成第一步`
3. `并行启动两个子智能体：一个分析 docs/zh，一个分析 agents/history，最后合并结论`
4. `后台运行一个简单命令 sleep 5 && echo done，同时继续检查 README-zh.md`
5. `切换新会话后，查看 /todo 和 /compact 的行为`

## 和 s04-subagent 的关系

[`s04-subagent.md`](./s04-subagent.md) 讲的是最小心智模型: 父智能体通过一个 `task` 工具创建干净上下文, 子智能体执行后只返回摘要。

`full_subagent_langchain.py` 是这个模型的工程化版本:

| 维度 | s04 最小示例 | full_subagent_langchain.py |
|------|--------------|----------------------------|
| 模型调用 | Anthropic 示例风格 | LangChain `ChatOpenAI.bind_tools()` |
| 子智能体工具名 | `task` | `sub_agent` |
| 会话持久化 | 无 | `.chathistory/session_N.jsonl` |
| todo | 无或课程级示例 | 会话级多看板 |
| 并行工具调用 | 无 | `parallel=true` + 线程池 |
| 后台任务 | 无 | 完成后注入通知 |
| 上下文压缩 | 无 | 自动检查 + `/compact` |
| Skill | 无 | 描述常驻, 内容按需加载 |

如果想先理解概念, 看 `s04-subagent.md`。如果想看多个机制如何接成一个可运行 CLI, 看 `full_subagent_langchain.py`。
