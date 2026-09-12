# s09: Memory 系统

## AI 为什么需要记忆？

LLM 天生是"金鱼记忆"——每次对话都是从头再来。上次你告诉它"用 bun 不要 npm"，下次又忘了。没有记忆的 Agent 永远是新手：每次都要重新了解你的偏好、重新踩同样的坑。

Claude Code 的解决方案是**多层记忆架构**，每一层解决不同时间尺度的记忆需求：

| 层级 | 名称 | 生命周期 | 核心职责 |
|------|------|---------|---------|
| 1 | CLAUDE.md 指令文件 | 永久 | 人类预写的静态指令与规则 |
| 2 | Auto Memory（memdir） | 跨会话 | AI 自动提取的持久化知识 |
| 3 | Session Memory | 单次会话 | 当前会话的结构化笔记 |
| 4 | Agent Memory | 跨会话 | 特定 Agent 的专属记忆 |
| 5 | Relevant Memories | 按需注入 | 实时召回的相关记忆 |

s09 主要聚焦第 2 层 **Auto Memory**，后面几层在实际 CC 源码中也有对应实现。

---

## 一、CLAUDE.md — 静态指令的层级发现

CLAUDE.md 是最基础的"记忆"，本质是人类预写的指令文件。

### 加载顺序（从低到高优先级）

```
1. Managed Memory（如 /etc/claude-code/CLAUDE.md）→ 全局，适用于所有用户
2. User Memory（~/.claude/CLAUDE.md）→ 用户私有的全局指令
3. Project Memory（CLAUDE.md, .claude/CLAUDE.md, .claude/rules/*.md）→ 项目代码库中的指令
4. Local Memory（CLAUDE.local.md）→ 项目私有，不提交到 git
```

精妙设计：**加载顺序与优先级相反**。高优先级的后加载，因为 LLM 对靠后的内容关注度更高（recency bias）。

### 遍历规则

从 CWD 逐级向上到根目录，每个目录查找 `CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/*.md`。先 push 的是 CWD，reverse 后从根向 CWD 方向遍历——离 CWD 越近的文件优先级越高。

### @include 指令

支持 `@path` 语法引用外部文件（只支持 70+ 种文本扩展名，防止二进制泄露），有循环引用检测和深度限制。

### 注入到 System Prompt

通过 `getMemoryFiles()` → `filterInjectedMemoryFiles()` → `getUserContext()` 注入。有一个 feature gate `tengu_moth_copse`：开启后，MEMORY.md 索引不再通过用户上下文注入，改为走异步预取 + Attachment 注入（详见第五节）。

---

## 二、Auto Memory — AI 的持久化知识库

这是记忆系统的核心——内容**完全由 AI 生成和维护**，不是人写的。

### 目录结构

```
~/.claude/projects/<sanitized-git-root>/memory/
├── MEMORY.md                  # 索引文件（≤200行/25KB）
├── user_role.md               # 用户角色记忆
├── feedback_testing.md        # 反馈记忆
├── project_auth_rewrite.md    # 项目记忆
├── reference_linear.md        # 参考记忆
├── team/                      # 团队共享记忆
└── logs/                      # 日志模式
```

路径优先级：环境变量覆盖 → Settings 覆盖 → 基于 Git 根路径的 sanitized 路径。`sanitizePath()` 把非字母数字替换为连字符（如 `/Users/foo/my-project` → `-Users-foo-my-project`）。所有 Git worktree 共享同一个记忆目录。

### 四类记忆

| 类型 | 含义 | 写入时机 | 不应记什么 |
|------|------|---------|-----------|
| user | 用户角色、偏好、知识水平 | 了解到用户信息时 | 负面评价 |
| feedback | 行为纠正 + 正向确认 | 用户纠正或确认做法时 | 只记纠正不记确认 |
| project | 项目背景、决策、截止日期 | 得知不可从代码推导的信息时 | 可从 git log 读到的 |
| reference | 外部系统指针 | 得知外部资源位置时 | 系统的具体内容（只记"在哪"） |

**什么不记（`WHAT_NOT_TO_SAVE`）**：代码模式（从代码读）、Git 历史（`git log`）、调试方案（修复在代码里）、CLAUDE.md 已有的、临时任务状态。最关键的一条：**即使用户明确要求记，如果内容是派生信息，也要反问"哪里是让人意外或有价值的"**。

### DIR_EXISTS_GUIDANCE

源码里有一句 Prompt：**"这个目录已经存在了，直接用 Write 工具写，不要跑 mkdir 或检查是否存在"**。原因是之前 AI 每次写记忆前要先 `ls`/`mkdir -p`，白白浪费 1-2 个 tool call。代码保证目录存在，Prompt 告诉 AI 这个承诺成立——**代码 + Prompt 协同**的设计模式。

---

## 三、提取记忆 — 从对话中自动萃取

### 触发时机

CC 源码里通过 `stopHooks` 在每轮对话结束时 fire-and-forget 触发，和教学版的 `stop_reason != "tool_use"` 分支是同一个思路。

### 闭包状态与互斥

用闭包作用域管理状态，核心变量：
- `lastMemoryMessageUuid`：游标，记上次处理到哪条消息
- `inProgress`：互斥锁，防止并行执行
- `pendingContext`：提取进行中又来新触发时，**只保留最后一个**上下文，等当前提取完了继续处理

### 主 Agent 互斥

主 Agent 如果已经自己写了记忆文件（比如用户说"记住这个"），后台提取就**跳过并推进游标**，避免两个 Agent 同时写同一个文件。

### Forked Agent 沙箱

提取通过独立的 forked agent 执行，权限严格受限：
- **允许**：FileRead、Grep、Glob（只读）
- **允许**：Bash 仅限只读命令（ls, find, grep, cat）
- **允许**：FileEdit/FileWrite **仅限** memoryDir 内的路径
- **禁止**：MCP、Agent、写入型 Bash

即使 Prompt 被注入，也动不了记忆目录以外的文件。

### 两步高效策略

提取 Agent 被要求两步走：**turn 1 并行读所有可能要改的文件 → turn 2 并行写所有文件**。限制 `maxTurns: 5`，防止陷入验证深坑。

---

## 四、Session Memory — 当前会话的笔记

### 解决的问题

对话很长需要 auto-compact 时，如何保留关键的会话上下文。

### 结构化模板

10 段 Markdown 模板：Session Title / Current State / Task specification / Files and Functions / Workflow / Errors & Corrections / Codebase and System Documentation / Learnings / Key results / Worklog。**模板固定 + 内容可变**，确保结构稳定。

### 双阈值触发

不是每轮都更新。Token 增长 ≥ 10K 才初始化，之后 Token 增长 ≥ 5K **且** tool call ≥ 3 次才触发更新。Token 增长是必要门槛，光 tool call 多但内容没变就不更新。

### 与 Compact 协同

auto-compact 时，Session Memory 可以直接作为摘要用，**免去额外调 LLM 总结**的 API 调用（`sessionMemoryCompact.ts`）。提取完成后等待最多 15 秒确保持续能拿到最新笔记。

### 大小控制

每个 section ≤ 2000 token，总文件 ≤ 12000 token。

---

## 五、Agent Memory — 每个 Agent 的专属记忆

自定义 Agent 也有自己的记忆空间，分三种 scope：

| Scope | 路径 | 版本控制 | 适用 |
|-------|------|---------|------|
| user | `~/.claude/agent-memory/<type>/` | 否 | 跨项目通用知识 |
| project | `<cwd>/.claude/agent-memory/<type>/` | 是 | 项目特定，团队共享 |
| local | `<cwd>/.claude/agent-memory-local/<type>/` | 否 | 本地特定，不分享 |

加载在同步路径中调用，目录创建用 **fire-and-forget**——Agent 从 spawn 到实际写文件之间至少有一个 API 往返（几百毫秒），`mkdir` 只要微秒级，不阻塞。

---

## 六、Relevant Memories — 按需召回

前面四层管存储，这一层管**何时以及如何召回**——不是全塞进去，是只注入相关的。

### 双阶段：Scan → Select

**阶段一：扫描**（`scanMemoryFiles`）—— 读 memory 目录下所有 .md 文件的前 30 行 frontmatter，按 mtime 降序，最多 200 个。`readFileInRange` 同时 stat 获取 mtime，**一次 syscall 解决两个需求**。

**阶段二：选择**（`selectRelevantMemories`）—— 用 Sonnet 做轻量 side query，传用户查询 + 记忆清单（文件名+描述），让模型选最多 5 个相关文件。选择器强调**审慎**：不确定就不要选。还有一个反直觉的细节：如果最近用了某个工具，它的参考文档不召回（AI 已经在用了），但已知问题/注意事项仍要召回（正在用的时候最需要知道坑在哪）。

### 异步预取 + Attachment 注入

记忆召回是异步的，和主对话并行：
```
用户提交 → startRelevantMemoryPrefetch() → sideQuery → 选中的记忆 → 注入 attachment
```

注入时附带**预计算的新鲜度标记**（"today"/"yesterday"/"3 days ago"），不在渲染时计算，**保证 Prompt Cache 字节稳定性**——否则每次调用 `memoryAge()` 结果都可能变，导致 cache 失效。

### Session 级去重

已经展示过的记忆路径不在 sideQuery 时传入，让 5 个名额花在新候选上。同时有 session 总量上限，防止记忆注入累积太多。

精巧设计：compact 后旧的 attachment 消息被删除，**surfacedPaths 自然重置**——那些记忆可以合理地重新注入到压缩后的上下文。

---

## 七、Auto Dream — 记忆的后台巩固

类似人类睡眠时的记忆巩固——攒够了就自动整理。

### 三重门控（实际是四层）

```
isGateOpen() → 时间门控(≥24h) → 会话门控(≥5个新会话) → 锁门控(文件锁)
```

- **KAIROS 模式或远程模式**：直接不走 Dream
- **时间门控**：距上次巩固 ≥ 24 小时
- **会话门控**：此期间至少有 5 个新会话 transcript
- **锁门控**：`.consolidate-lock` 文件锁，防止多进程同时巩固

### 锁与回滚

巩固失败或用户手动 kill → **回滚锁的 mtime**，让时间门控下次能再次通过，防止"梦被永久打断"。

锁文件的 mtime 就是 lastConsolidatedAt。崩溃恢复：1 小时后锁自动过期。

---

## 八、架构全景

写入有三条路径：
1. **主 Agent**：用户说"记住这个" → 直接写 memory 目录
2. **后台提取**：每轮结束后 forked agent 自动提取
3. **Session Memory**：会话中持续更新结构化笔记

长期积累后，**Auto Dream** 定期巩固合并。

读取有三条路径：
1. **CLAUDE.md** 通过 `getUserContext()` 注入 System Prompt
2. **MEMORY.md**（传统路径）同样注入用户上下文；新路径通过 sideQuery 选文件 + attachment 按需注入
3. **Session Memory** 在 compact 时复用为摘要

---

## 教学版 vs 真实 CC vs 本项目的实现

| 维度 | 教学版 (s09_code.py) | CC 真实源码 | **本项目 (s09_code_cc.py / agent_full_v2.py)** |
|------|---------------------|-----------|----------------------------------------------|
| 写入方式 | turn 结束后额外 LLM 调用批量抽取 | 主 Agent tool 写入 + forked agent 后台提取 | **Tool 驱动**：模型通过 `write_memory`/`forget_memory` 即时写入，零额外 LLM 调用 |
| 选择记忆 | 每轮调 LLM 选相关索引 + 关键词兜底 | Sonnet side-query 异步预取 | **无选择逻辑**：MEMORY.md 索引始终在 system prompt 中，模型直接可见 |
| 记忆注入 | 在 user 消息前拼接完整正文 | attachment 按需注入 | **不注入正文**：模型需要详情时通过 `read_file .memory/<name>` 自己读 |
| 整合/做梦 | `consolidate_memories` 全量重写 | Auto Dream：三重门控 + 锁 + 回滚 | **无** — Tool 模式不会产生重复/脏数据，无需清洗 |
| 触发规则 | 系统提示中一句话带过 | 详细规则嵌入 system prompt | **显式规则**：何时写、写什么类型、怎么用，模型自行判断 |
| 并发控制 | 无 | 闭包互斥锁 + 文件锁 + pendingContext | 无（单进程，不需要） |
| 存储位置 | `.memory/`（项目内） | `~/.claude/projects/<hash>/memory/` | 同教学版 |

### 关键差异说明

本项目代码（`s09_code_cc.py`）**舍弃了教学版事后分析的复杂设计**，采用了和真实 CC 一致的 Tool 驱动模式：

- **删除了** `select_relevant_memories`、`load_memories`、`extract_memories`、`consolidate_memories` — 这些函数每轮需要额外 1-2 次 LLM 调用，且依赖"猜哪些值得记"的不可靠策略
- **新增了** `write_memory` 和 `forget_memory` 两个工具，模型在对话中自主判断何时写入/删除
- **重写了** `build_system()`，加入清晰的记忆触发规则，告诉模型何时该保存
- **简化了** `agent_loop()`，去掉所有记忆预处理和后处理，只保留 s08 压缩管道

核心变化是 **记忆的触发权从代码转移到了模型**：代码不再替模型做"哪些值得记"的判断，而是给模型工具和规则，让它在对话中实时决策。

---

## 可迁移的设计模式

1. **分类法驱动记忆质量**：闭合的四类分类法，每种定义"能记什么"和"不应该记什么"。
2. **索引-内容分离 + 智能召回**：轻量索引常驻 + 审慎 side-query 选内容按需注入，比全塞入高效得多。
3. **后台提取 + 主 Agent 互斥**：fire-and-forget 不阻塞主线，互斥检测避免并发写冲突。
4. **代码 + Prompt 协同**：代码保证前置条件（目录存在），Prompt 告知 AI 不用检查——省 tool call。
