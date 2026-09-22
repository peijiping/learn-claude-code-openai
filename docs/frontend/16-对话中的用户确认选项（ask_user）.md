# 16 - 对话中的用户确认选项（`ask_user`，2026-09-21）

> **2026-09-22 增补两处交互调整**（用户 review 后拍板）：
> ① 面板出现时**输入区整块让位**（§5.2）；② 小结块与正文按**当时的先后**交错渲染
> ——提问前那句话回到卡片**上方**（§5.4）。

> 阅读顺序建议：先看 §1「要解决什么」，再看 §2「为什么用工具调用」——后者是本设计
> 唯一"容易被做错"的地方（很容易本能地想成"让模型输出一段特殊 JSON，前端解析一下"）。
> 之后 §3 数据流、§4 数据契约、§5 前端交互、§6 边界与竞态、§7 改动清单。

***

## 1. 要解决什么

模型在干活时经常需要用户做个决定：「用哪个模型？」「导出成 Markdown 还是 PDF？」
「要不要覆盖已有文件？」。没有这个能力时，模型只能：

- 猜一个默认值继续（用户可能不满意，且事后才发现）；
- 或者在正文里写一段"请告诉我你想要哪种"，然后**结束本轮** —— 等用户手打一句话再开新的一轮。

后者不只是体验差，还会把上下文切碎：提问在一轮、回答在下一轮，中间夹着一次完整的
`turn_end` / 新 `run_turn`，模型要在两轮之间自己把"我问了什么"和"用户答了什么"接起来。

本特性让模型可以**在同一个回合内**问、等、拿到答案、接着干：

```
模型产出 tool_call(ask_user)  →  工具 handler 阻塞等待  →  用户在前端作答
        ↑                                                      │
        └──────────  答案作为 tool_result 回填 ─────────────────┘
                         （同一个 turn 内继续）
```

***

## 2. 为什么用「工具调用」而不是「特殊正文块」

最初的直觉方案（**范式 B**）是：模型输出一段约定的 JSON / XML / Markdown 块，
客户端解析后弹 UI；用户选完后，把"选择 + 结果"当成**一条新的用户消息**发回去。

这个方案能跑，但它把已经在别处解决过的问题**重做一遍**，而且要做得一样好：

| 环节 | 范式 B（正文块）必须自己实现 | 工具调用（范式 A）现成 |
| --- | --- | --- |
| 结构校验 | 自己在客户端实现一套校验（模型会乱填） | provider 侧按 `parameters` schema 强校验 |
| 流式聚合 | 正文是**分片流式**到达的，JSON 可能被切成两半 → 必须自己拼完再解析 | `tool_call_start` / `tool_call_delta` / `tool_call` 三事件 |
| 配对 | 自己发明 id 并把"答"配回"问" | `tool_call_id` 天然配对 |
| 落盘 | 要新发明一种消息形状存进 jsonl | 普通 `assistant.tool_calls` + `role=tool` 行 |
| 回放 | 要写一套新的解析/渲染分支 | 复用既有 `_history_to_ui` 配对逻辑 |
| 压缩兼容 | 正文块会被 `compact` 截断/摘要掉，半截 JSON 直接崩 | 是标准 tool 消息，压缩逻辑本来就要处理它 |
| 提示词 | 要写一大段"请按这个格式输出"（且模型不一定听） | 工具 `description` 就是这段提示词，且**带 schema 约束** |

还有一条更根本的：**范式 B 的"回答"会开启新一轮**（新用户消息 = 新 turn），
而范式 A 的"回答"是 `tool_result`，**同一个 turn 内继续** —— 模型不用跨轮去接上下文。

Claude Code（`AskUserQuestion`）、Codex（`request_user_input`）、Trae（`AskUserQuestion`）、
Cline / Roo（`ask_followup_question`）**都是范式 A**。

> 范式 C（"专用事件 + 会话挂起"）是另一回事：Codex / Claude 用它做**权限审批**
> （"这条 bash 命令要不要放行"），因为那个决定不该由模型看见、也不该出现在对话历史里。
> 本特性问的是**业务问题**，答案必须回给模型，所以不适用。

**为什么"阻塞工具线程"是安全的**：`SessionRuntime.start_turn()` 用
`asyncio.to_thread()` 把 `run_turn` 派发到工作线程。阻塞那个工作线程**不会卡住
asyncio 事件循环** —— 事件循环继续收发 WebSocket、处理 `ask_answer` 命令。
这是整个设计能成立的前提（若把 `run_turn` 放在事件循环里跑，这条路直接死）。

***

## 3. 数据流（实时 / 回放两条通道）

```
                            ┌─────────────── 后端（turn 工作线程）───────────────┐
模型返回 tool_call(ask_user) │ agent_loop 分桶 → ask 桶（独立于 background/串行桶）│
                            │  → run_ask_user → broker.ask(questions, tc_id)    │
                            │      ① 规范化（模型会乱填，一律收敛）             │
                            │      ② 注册 pending + 广播 ask_request           │
                            │      ③ 阻塞 done.wait(0.2s) 轮询 stop_event      │
                            └───────────────────┬───────────────────────────────┘
                                                │  WS 信封（ConnectionHub 广播）
                       ┌────────────────────────▼─────────────────────────────┐
                       │ 前端 store：ask_request → interactionBySession[sid]   │
                       │ AskUserPanel（输入框上方）弹出，一题一屏作答          │
                       └────────────────────────┬─────────────────────────────┘
                                                │  用户点「提交」/「取消」
                                                │  或直接在输入框打字发送
                       ┌────────────────────────▼─────────────────────────────┐
                       │ 前端 → 后端：{kind:"ask_answer"} / {kind:"ask_cancel"}│
                       │ 主进程只做形状校验 + 转发（fire-and-forget）          │
                       └────────────────────────┬─────────────────────────────┘
                                                │
                       ┌────────────────────────▼─────────────────────────────┐
                       │ broker.resolve()：锁内改状态 + 移出 pending          │
                       │   → 广播 ask_resolved（status/answers/result_text）   │
                       │   → done.set() 唤醒工作线程（**必须在锁外**）         │
                       │ 工具返回 result_text → agent_loop 回填 tool 消息      │
                       │  → 模型在同一次 run_turn 内继续                       │
                       └──────────────────────────────────────────────────────┘
```

**回放通道**（切会话 / 重启）走的是另一条路，且与实时通道展示**同一份文本**：

```
session_history.messages[]
  assistant 行：tool_calls[] 里的 ask_user  → 摘出到 askUsers[{tool_call_id, args}]
  tool      行：content（= broker 生成的 result_text）→ 按 tool_call_id 配回去
                                          → askUsers[].result / .status
                    ↓
  store.historyToMessage() 映射成 `Message.askUsers[]` → AskUserBlock 渲染
```

`result_text` 是**唯一展示载体**：它由 broker 生成，与回填给模型的 `tool_result`
**逐字节相同**。所以：

- 前端**不做任何解析**（不解析 options、不解析用户选了什么），只 `white-space: pre-wrap` 原样展示；
- "实时看到的"与"重开会话看到的"必然一致 —— 不存在两套渲染逻辑走偏的可能。

`askUsers[].status`（结局徽标）由后端 `interaction.status_of_result()` 从 `result_text`
反推（jsonl 里不存 outcome）。这个映射刻意**只留在 interaction 模块一处**：
前端 / 桥层只搬运，不去 `startswith` 猜文案 —— 否则改一句常量文案，回放徽标就会静默错位。

***

## 4. 数据契约

### 4.1 工具 schema（`agents/tools.py` 的 `_ask_user_tool_def`）

```jsonc
{
  "name": "ask_user",
  "description": "向用户提出 1–4 个选择题并等待作答（阻塞）。…何时用/何时不用/禁止索取敏感信息…",
  "parameters": {
    "type": "object",
    "properties": {
      "questions": {
        "type": "array", "minItems": 1, "maxItems": 4,
        "items": {
          "type": "object",
          "properties": {
            "id":        { "type": "string" },              // 批内唯一，作答按它对号入座
            "header":    { "type": "string" },              // ≤12 字，步骤条与小结块标题
            "question":  { "type": "string" },              // 完整问句
            "multi_select": { "type": "boolean" },
            "allow_custom": { "type": "boolean" },          // 是否额外给「其他」自由文本
            "custom_label": { "type": "string" },           // 缺省「其他」
            "options": {
              "type": "array", "minItems": 2, "maxItems": 4,
              "items": { "type": "object",
                         "properties": { "label": {"type":"string"},
                                         "description": {"type":"string"} },
                         "required": ["label"] }
            }
          },
          "required": ["id", "header", "question", "options"]
        }
      }
    },
    "required": ["questions"]
  }
}
```

**刻意不加 `parallel` / `run_in_background`**：这条工具**必须串行且独占**（同一会话
至多一个在途提问）。但 `description` 里**明确写出**这两个参数在本工具上无效 ——
因为模型见过别的工具带这两个参数，不写它会尝试传；写了它就不会传。
（`tests/test_ask_user_schema_contract.py` 同时守这两点：schema 里**没有**这两个字段、
description 里**提到**它们无效。）

### 4.2 信封

| 方向 | kind | payload |
| --- | --- | --- |
| 后端 → 前端 | `ask_request` | `{session_id, request_id, tool_call_id, questions[], created_at}` |
| 后端 → 前端 | `ask_resolved` | `{session_id, request_id, tool_call_id, status, answers[], result_text}` |
| 前端 → 后端 | `ask_answer` | `{session_id, request_id, answers[{question_id, selected[], custom_text?}]}` |
| 前端 → 后端 | `ask_cancel` | `{session_id, request_id}` |

`status` 取值：`answered` / `cancelled` / `stopped`（`stopped` = 被停止按钮或会话销毁结算）。

### 4.3 回填给模型的固定文案

| 结局 | `result_text` 首行 | 对模型的指示 |
| --- | --- | --- |
| 已作答 | `用户已完成选择：` | 每行 `- [header] 题目 → 选项、选项；其他：xxx` |
| 自由文本 | `用户没有选择选项，而是直接回复了：` | 原文附在下一行 |
| 取消 | `用户取消了本次提问（未作答）。` | 不要重复追问，自选默认方案并说明 |
| 被停止 | `本轮已被用户停止，提问未获回答。` | 立即停手，不要再调工具 |

选项之间用 `、`，自定义文本用 `；` 单独分层 —— 否则整句拉平后分不清哪部分
是用户自己写的（例：`Markdown、PDF；其他：还想要 EPUB`）。

***

## 5. 前端交互

### 5.1 面板（输入框上方常驻）

**组件**：`components/Chat/AskUserPanel.tsx`；样式 `chat.css` 的 `.ask-panel*` 系列。
**位置**：`.chat` 内 `MessageList` 与 `.composer-wrap` 之间，**紧贴输入框**
（它是"必须现在做决定"的交互，越靠近手边越好），与 `TaskBoard` 上下相邻。
**出现条件**：当前会话存在在途提问（`interactionBySession[activeSession]` 非空）。

| 部分 | 内容 |
| --- | --- |
| 表头（38px，紫调底） | 图标 + `需要你确认` + `第 N/M 题` + `可多选` 提示 + 「取消」 |
| 步骤条 | 每题一个可点击的胶囊（序号/对勾 + `header`），**可任意跳题** |
| 题目区（≤260px 纵向滚动） | `header` 小标 + 完整问句 + 选项列表（单选=圆点 / 多选=方框）+ 「其他」行 + 自定义输入框 |
| 底部 | 提示文案（`逐题选择后提交；要自己写答案请选「其他」`）+ 「上一步」+ 「下一步」/「提交」 |

**几条刻意的取舍**：

- **不强制作答**：允许空提交（后端渲染「（未选择）」）。强行要求选择只会逼用户编答案，
  不如让模型拿到"用户没选"这个真实信号。
- **单选可反选**：再点一次已选项 = 取消选择 —— 否则点错一次就只能刷新或取消整个提问。
- **多选/单选的形状差异**：`●` 圆点 vs `☐` 方框，形状直接告诉用户"能不能多选"，不必读提示。
- **步骤条可跳题**：一题一屏必须配一个"全局视角"，否则第 4 题的进度无从得知。
- **Enter = 下一步 / 提交**（只在焦点位于面板内时触发，**不抢输入框的键**）。
- **提交后不本地关面板**：等 `ask_resolved` 关闭（见 §6）。乐观关面板会在"后端丢弃了
  这次作答"时造成假象 —— 后端对迟到/重复提交是幂等丢弃，前端必须能区分"还没结算"与"已结算"。
  提交按钮有 3s 自动解锁（避免作答在传输层丢失后面板变成点不动的砖；重复提交无副作用）。

### 5.2 等待期的输入区：**整块让位**（2026-09-22 改）

面板出现时输入区**整块隐藏**（`.chat--asking`），作答期间只保留：选择题 / 各题的
「其他」自由文本 / 右上角「取消」。

**为什么改**（原设计"不锁输入框"的取舍被推翻）：两条作答路径并存时，用户可能
在输入框里把答案打一半、又去点选项 —— 两边都像"已经答了"，最后到底以哪个为准
连用户自己都说不清。面板是"此刻只有一个动作"的交互，输入框就该让位。

| 关键点 | 实现 | 为什么这么做 |
| --- | --- | --- |
| 隐藏判据 | `ChatPanel` 的 `askOpen`（`interactionBySession[activeSession]` 且 `questions.length > 0`） | 与面板的渲染条件**同源**，不做第二份推断：面板出现的那一刻输入区就收起 |
| 隐藏方式 | `.chat--asking .composer-wrap { display: none }` | **绝不卸载** `InputBox` —— 编辑器是非受控的（内容只在内部、永不回灌），卸载即丢草稿。作答完 `askOpen` 变回 false，草稿原样回来 |
| 焦点 | `InputBox.suspended` → `editor.commands.blur()` | 光隐藏不够：残留在编辑器上的焦点会让用户"盲打"进看不见的输入框 |
| 发送兜底 | `ChatPanel.doSend()` 开头 `if (askOpen) return` | CSS 只是表现，判据必须落在状态上：万一有残留焦点 / 快捷键把发送打进来，也绝不与作答面板抢答 |
| 底部留白 | `.chat--asking .ask-panel { padding-bottom: var(--sp-4) }` | 面板原本靠 `.composer-wrap` 的上边距与输入框拉开距离；输入区一藏，面板就落到最底部，得自己承担底部留白（实测 16px） |
| 提示文案 | 底部提示改为 `逐题选择后提交；要自己写答案请选「其他」` | 原文案"也可以直接在输入框打字发送"此时**是假的**（输入框根本不可见） |

**自由作答的入口变了，后端能力保留**：等待期"用户在输入框直接发消息 = 自由回答"
这条后端通路（`resolve_free_text_any` 拦截 + `format_free_text` 回填）**保留不动** ——
它是 chat 分支的通用能力，其他客户端 / 旧前端 / 手工构造的消息仍可能命中；
只是**本前端不再暴露这个入口**。想要"放弃选择、直接说句话"的用户路径改由
「取消」承担（后端回填固定文案，模型据此自选默认值继续）。

> 副作用（已知并接受）：输入区同时是「停止」按钮的所在地，让位期间**停止按钮不可见**。
> 想彻底停下来的操作路径是：先「取消」提问（面板消失、输入区回来）→ 再点「停止」。

#### 后端通路（保留）：等待期发来的 chat 消息 = 自由作答

这条通路**不随前端让位而删除**：等待作答期间若仍有 chat 消息进来（其他客户端、
旧前端、脚本），它**不走正常 chat 分支**，而是被 `ws_bridge` 的 chat 处理**拦截**：

```python
# ws_bridge.chat 分支，在 `if rt.busy:` 判断**之前**
if rt.has_pending_interaction():
    if await asyncio.to_thread(rt.resolve_ask_free_text, text):
        continue   # 该消息已被当作"自由回答"，不再派发新一轮
```

于是「用户没选选项，而是直接回复了：<原文>」作为 tool_result 回填，
**原 turn 在同一个回合内继续**（不另起 turn）。

> 拦截必须**先于 `busy` 判断**，且必须用 broker 侧的原子操作
> （`resolve_free_text_any` 内部"取第一个在途 + 结算"在同一把锁里完成）——
> 否则会出现 TOCTOU：判定时还有在途提问、结算时已被别的路径结算掉了。

### 5.3 只读小结块（消息下方）

**组件**：`components/Chat/AskUserBlock.tsx`；样式 `.ask-block*`。

提问结算后，在**发起它的那条 assistant 消息**下留一条只读小结，作为"当时问了什么、
用户选了什么"的历史凭证（与普通消息文本一样，是历史，不能再改 —— 改了就与回填给
模型的 `tool_result` 不一致，模型和用户看到的两份事实会分叉）。

| 结局 | 左侧色条 | 徽标 |
| --- | --- | --- |
| `answered` | 绿 | ✓ 已确认选项 |
| `cancelled` | 琥珀 | × 已取消提问 |
| `stopped` | 红 | ■ 提问已中断 |
| `incomplete`（回放发现 `result` 为空） | 灰 | 🕐 提问未完成 |

`status === 'pending'`（实时在途）**不渲染** —— 此刻面板正承载交互，
再在消息下挂一条空小结就是重复表达（由 `MessageItem` 过滤）。

### 5.4 正文与小结块的先后（2026-09-22 改）

**规则**：小结块插在正文里**发起提问那一刻**的位置 —— 之前的正文在卡片**上方**，
之后的正文留在卡片**下方**。

**为什么**：模型的话总在提问之前（"好，先跟你确认几个打地基的关键项，确认完我再出
完整攻略"，然后才 `ask_user`）。原实现把正文整块渲染在卡片之后，于是这句话被排到了
卡片下面 —— 与"先有这句话、才有后面的选项与确认"的事实顺序相反。

**切分点 `AskUserMsg.contentOffset`**（两条路径一致性是这条规则的关键）：

| 路径 | `contentOffset` 的来源 | 结果 |
| --- | --- | --- |
| 实时 | `tool_call_start`（`ask_user`）那一刻本条消息已累积的正文长度（`upsertAskBlock` 时记录） | 精确切分：提问前的话在上、提问后**同一气泡内**续写的正文在下（实时路径整轮正文合并进一条消息） |
| 回放 | 缺省（不落盘）→ 视作"全量正文在卡片之前" | 同样正确：jsonl 每次 LLM 调用一行，正文只可能出自发起该 `tool_call` 的那次响应，必然在提问之前 |

**渲染**：`MessageItem` 用 `parts` 把正文切成若干段与卡片交错渲染（`MarkdownBody`
逐段渲染，样式与改造前的单块完全一致；流式光标只挂在最后一段上）。偏移做了
单调夹取（`clamp(offset, cursor, content.length)`），老数据 / 越界值都退化为
"正文全在卡片上方"，不会出现前后乱序。

> 实测（真实组件 harness，2026-09-22）：改造前 `.ask-block` top=55、正文 top=206
> （正文全在卡片下方）；改造后 `markdown-body[0]` top=80 < 卡片 top=111 <
> `markdown-body[1]` top=262。

### 5.5 `ask_user` 不进普通工具条

与 `sub_agent` 同一条纪律：`ask_user` 的调用**不出现在工具条**里（否则会和面板 /
小结块重复表达同一件事）。两条路径都要处理：

- **实时**：`tool_call_start` 里判 `tool_name === 'ask_user'` → 不进 `toolCalls`，
  改为在消息上建一个 `askUsers[]` 占位块（`status='pending'`）当**锚点**；
- **回放**：后端 `_history_to_ui` 已把它从 `toolCalls` 摘出放进 `askUsers[]`，
  但 `_tc_ids` **保留**它的 id（唯一锚点规则与前端锚定都依赖它）。

***

## 6. 竞态与边界（每一条都有对应用例）

| 场景 | 处理 |
| --- | --- |
| **等待期持锁** | `ask()` 的轮询 `wait()` **在锁外**。锁内等待 = 事件循环线程的 `resolve()` 永远拿不到锁 → 死锁。这是本模块最要命的回归。 |
| **广播前被结算** | `announced` 标志 + `_announced_locked()` 闸门：`ask_request` **一定先于**任何外部触发的 `ask_resolved` 到达前端。否则前端会先收到 `ask_resolved` → "面板还没出现就被判已解决"、只读小结缺标题。 |
| **迟到 / 重复提交** | `_settle()` 唯一结算出口，锁内判 `pend.outcome` 非空即幂等丢弃；前端只读取 `bool` 结果。 |
| **停止按钮** | `SessionRuntime.request_stop()` → `broker.cancel_all("stopped")`（**即使 agent 还没建好也执行**）。结算出 `stopped` + 固定文案，阻塞线程被唤醒。前端**不本地清面板**，等 `ask_resolved` —— 本地清会让随后到达的事件找不到 in-flight 的问题文本。 |
| **兜底停止** | 某条停止路径漏了 `cancel_all` 时，`ask()` 每 0.2s 检查 `stop_event`，命中即结算。 |
| **会话被删 / 销毁** | `SessionRuntimeRegistry.remove()` → `rt.close()` → broker 标记关闭 + `cancel_all`，解锁所有阻塞线程。 |
| **孤儿 tool_call** | `load_session_history` 会**自愈重写**"有 tool_calls 无 tool_result"的会话文件 → `ask()` **绝不抛异常、绝不挂死**，所有退出路径都返回字符串（含 `Error:` 文本）。 |
| **断线重连** | 后端在 `handle()` 连接建立时用 `_ask_snapshot_lines()` 重放所有在途 `ask_request`；前端在 `setConnection('connected')` 时清空 `interactionBySession`（避免断连期间已被结算的提问留下永不消失的僵尸面板）。 |
| **`ask_resolved` 先到、`tool_call` 后到** | broker 在工具返回**之前**就广播了结果，所以 `ask_resolved` 先到。两条事件都只补自己的字段（前者补结果、后者补 questions），互不覆盖。 |
| **同一会话并发提问** | 不允许：`pending` 非空时 `ask()` 直接返回 `Error:` 文本（`BUSY_TEXT`）。 |
| **silent / cron / 无前端** | **不做 `input()` 兜底**，也不设超时。无 broker 时工具返回明确的 `Error:` 文本；无人应答时线程会一直等（不设超时是产品决定：模型的问题通常重要到值得等）。 |

***

## 7. 改动清单

### 后端

| 文件 | 层 | 改动 |
| --- | --- | --- |
| `agents/interaction.py` | **新增叶子模块** | broker 全部逻辑（只依赖标准库 + `logger`，不 import 任何引擎模块，避免成环） |
| `agents/tools.py` | 引擎 | `set/get_interaction_broker` holder、`_run_ask_user` handler、`_ask_user_tool_def()`（进 `main_agent_tools`，**不进** `base_tools` —— 子智能体不提问） |
| `agents/agent_full_v2.py` | 引擎 | 新增模块级 `_tool_args_of` / `_partition_tool_calls`（四桶：background / parallel / serial / ask）；`_make_executor` 注入 `questions`/`_tool_call_id`/`_stop_event`；`_execute_tool_call` 的后台判断排除 `ask_user`；新增「阶段 4」执行 ask 桶 |
| `agents/session_runtime.py` | 薄层 | 构造 broker 并注入 `tools`；`request_stop()` 额外 `cancel_all`；新增 `resolve_ask` / `resolve_ask_free_text` / `cancel_ask` / `has_pending_interaction` / `pending_interactions` / `close`；`remove()` 调 `close()` |
| `agents/ws_bridge.py` | 薄层 | `_ask_snapshot_lines()`（连接重放 / status_query）、chat 分支自由文本拦截、`ask_answer` / `ask_cancel` 两个命令、`_history_to_ui` 的 `askUsers[]` 配对与 `status` 反推 |

**执行顺序**：四桶按 `background → parallel → serial → ask` 执行，但**回填严格按模型
声明的顺序**（否则 jsonl 里 tool 消息与 tool_calls 顺序错位）。

### 前端

| 文件 | 改动 |
| --- | --- |
| `protocols/agentProtocol.ts` | `AskOption` / `AskQuestion` / `AskStatus` / `AskAnswer` / `HistoryAskUser`；`UiEvent` 加 `ask_request` / `ask_resolved`；`ControlKind` 加 `ask_answer` / `ask_cancel`；`HistoryMessage.askUsers?` |
| `store/agentStore.ts` | `Message.askUsers?`；`AskInteraction` 与 `interactionBySession` slice；`answerAsk` / `cancelAsk`；`handleEvent` 两个新分支；`tool_call_start` / `tool_call` 的 `ask_user` 特判；`historyToMessage` 映射；重连清 slice |
| ↑（2026-09-22） | `AskUserMsg.contentOffset?`（发起提问时的正文长度）+ `upsertAskBlock(…, contentOffset)`：切分点的**唯一产地**（§5.4） |
| `main/index.ts` | 两个 `ipcMain.handle('agent:answerAsk' / 'agent:cancelAsk')`，带 `isTrustedSender` 守卫，**fire-and-forget** |
| `preload/index.ts` + `index.d.ts` | 同上两个 API |
| `lib/browserAgent.ts` | 纯浏览器预览回退的同名实现 |
| `components/Chat/AskUserPanel.tsx` | **新增**：待确认面板 |
| `components/Chat/AskUserBlock.tsx` | **新增**：只读小结块 |
| `components/Chat/ChatPanel.tsx` | 挂载 `<AskUserPanel />`（`TaskBoard` 之后、输入框之前）；**2026-09-22**：`askOpen` 判据 + `chat--asking` 类 + `doSend` 守卫 + 传 `suspended`（§5.2） |
| `components/Chat/MessageItem.tsx` | assistant 分支渲染 `askUsers`（过滤 `pending`）；**2026-09-22**：`parts` 交错渲染 + 抽出 `MarkdownBody`（§5.4） |
| `components/Chat/InputBox.tsx` | **2026-09-22**：新增 `suspended` 入参 —— 让位期间 `blur()` 收回焦点（编辑器**不卸载**，草稿不丢） |
| `styles/chat.css` | `.ask-panel*` / `.ask-step*` / `.ask-option*` / `.ask-custom` / `.ask-btn*` / `.ask-block*`；**2026-09-22**：`.chat--asking .composer-wrap { display:none }` + `.chat--asking .ask-panel { padding-bottom: var(--sp-4) }` |

> **为什么 IPC 走 fire-and-forget 而不是 `request()`**：主进程 `pending` 表按 `kind`
> FIFO 配对、**不带 id**，同类并发会串台；而且回执本来就走 `ask_resolved` **广播**，
> 主进程拿它没有意义（渲染层直接从 WS 广播收）。这里只做形状校验 + 转发。

### 存储

**没有新增任何存储**。`ask_user` 就是一次普通工具调用，落在既有 jsonl 里
（`assistant.tool_calls[]` + `role=tool` 行）；面板的实时状态是**纯 UI 态**
（`interactionBySession`，不落盘）。这与"确认信息不用单独像 task 一样存储"的要求一致。

***

## 8. 验收

| 项 | 命令 / 方法 |
| --- | --- |
| 后端全量回归 | `HOME=<隔离目录> .venv/bin/python -m unittest discover -s tests` |
| 新增守护用例 | `interaction` broker 跨线程问答与幂等、schema 契约、四桶分桶、会话转发、回放配对、bridge 命令流 |
| 前端类型 + 构建 | `npm run typecheck` / `npm run build` |
| 交互实测（2026-09-22） | headless Chromium + **真实组件** harness（`MessageItem` / `ChatPanel` 真渲染 + 真实 `chat.css`，A/B 的 old 端是本次改动**逐条反向替换**后的代码）：让位 `.composer-wrap` `display none / h=0`（改前 `block / h=141`）、面板卡片底部留白 16px、正文与卡片先后 `md0.top=80 < ask.top=111 < md1.top=262`（改前 `ask.top=55 < md.top=206`）、无提问时输入区照常、无横向溢出 |
| 真机联调 | 起后端 + 前端，让模型触发一次 `ask_user`：作答 / 取消 / 自由文本 / 停止 四条路径各走一遍，再切走切回确认只读小结一致 |

***

## 9. 明确不做的

- **不设超时**：无人在场时线程就一直等（用户拍板）。要收敛只能靠停止按钮或关会话。
- **不做 CLI `input()` 兜底**：`input()` 会把线程挂死（silent / cron / 后台线程场景），
  与本项目的"工具层永远返回字符串、绝不挂死"原则冲突。
- **不给子智能体这个工具**：只有主智能体能向用户提问（子智能体是后台执行体，
  向用户提问会引入"谁在问"的困惑）。
- **不做多会话共享的提问队列**：一会话一个 broker，面板也只显示当前会话的。
- **前端不解析 `result_text`**：只原样展示（这是"实时/回放一致"的保证）。
