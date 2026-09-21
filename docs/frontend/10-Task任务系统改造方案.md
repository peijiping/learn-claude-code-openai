# 10 - Task 任务系统改造方案（下线 Todo + 实时任务面板）

> 状态：**待审核**（未实施）。审核通过后再动代码。
> 前置讨论：Todo 与 Task 的语义分界、Claude Code 的 TodoWrite→Task 演进复盘（2026-09-16）。

---

## 0. 结论摘要

| # | 需求 | 落地手段 |
|---|---|---|
| 1 | 去掉 todo | 工具定义与 handler 注释掉；`todo_manager.py` 保留不引用；system prompt 的单套化改写；agent_loop 里 3 轮 nag 注入**必须删除** |
| 2 | 完善 task 数据结构 | 新增 `parentId/depth/orderIndex/path/group_id/时间戳/result`，**全部带默认值**（旧文件自动兼容） |
| 3 | UI + 实时进度 | 输入框上方固定高度任务面板；后端发**幂等快照**（不是增量）；子智能体经同一 `_deliver` 通路推送 |
| 4 | 不跨会话、随会话删除 | 沿用 `scope` 文件名前缀；`delete_session_permanent` 增加 task 文件清理；**关闭**现有的"全完成即删文件"GC |
| 5 | 多组只显示最后执行中 | 引入 `group_id`（一次派活一组），纯函数算出"唯一未完成组" |
| 6 | 切换/回放只显示运行中 | `session_history` 后补发 `task_board`；前端在 `session_history` 时先把该会话 board 置空 |

**一句话架构**：后端把「当前未完成组」算成一份**小快照**，在每次任务状态变化时整体推给前端；前端只做「替换 + 渲染」，不做增量合并。

---

## 1. 现状盘点

### 1.1 两套系统的事实对照

| | `TodoManager`（`agents/todo_manager.py`） | `TaskManager`（`agents/task_manager.py`） |
|---|---|---|
| 存储 | 单文件 `.todo/session_<id>.todo.json` | 每任务一文件 `.tasks/task_<scope>_<ts>_<rand>.json`（**第二轮已改为**每会话一文件、文件内以组为 key，见 §16） |
| 结构 | 扁平列表，`{id,text,status}` | `{id,subject,description,status,owner,blockedBy}` |
| 状态 | pending / in_progress / completed（同时仅 1 个 in_progress） | pending / in_progress / completed |
| 更新语义 | **整表替换**（`update(items, fresh_start)`） | 逐条 CRUD |
| 会话绑定 | `set_todo_manager(session_id)` | `set_scope(f"{session_prefix}{session_id}")` |
| 依赖 | 无 | `blockedBy` |
| 归属 | 无 | `owner` |
| 自动回收 | 无（随会话删除） | `_gc_scoped_tasks()`：全部完成即删文件 |

**结论**：TodoManager 是"无 owner、无依赖的退化 TaskManager"，且其**整表替换**语义正是 Claude Code 撞墙的根源。按第 1 节需求整体下线，能力并入 Task。

### 1.2 Todo 引用点全清单（下线时逐条处理）

| 文件 | 位置 | 处理 |
|---|---|---|
| `agents/todo_manager.py` | 整文件 | **保留**，文件头加「已下线，禁止新引用」注释 |
| `agents/tools.py` | `_tools_cache` 内 `"todo"` 工具定义（≈L674-685） | 注释掉，注明下线原因与替代 |
| `agents/tools.py` | `_build_handlers()` 内 `"todo":`（L467） | 注释掉 |
| `agents/tools.py` | `_todo_manager`（L81）/ `set_todo_manager`（L164）/ `get_todo_manager`（L180） | 保留定义（无害），**注释掉全部调用方** |
| `agents/paths.py` | `TODO_DIR` / `todo_file_for_session` / `ensure_dirs` 内 mkdir | 保留（幂等、被测试引用） |
| `agents/system_prompt.py` | `_get_tools()` 内「待办与任务（两套并存）」整段（≈L204-215） | 改写为单套 Task 说明 |
| `agents/agent_full_v2.py` | `init_session` L551 / `new_session` L801 / `switch_session` L826 的 `set_todo_manager` | 注释掉 |
| `agents/agent_full_v2.py` | `_inject_todo_reminder`（L904-925）及其 2 处调用（L554 / L828） | 整段注释掉 + 调用点注释掉 |
| `agents/agent_full_v2.py` | `clear_session` 内 todo 重置（L844） | 改为清空本会话 task |
| `agents/agent_full_v2.py` | `show_tasks()`（L852-854） | 改为返回 task 看板文本 |
| `agents/agent_full_v2.py` | `agent_loop` 内 `rounds_since_todo`（L1261 / L1466-1470 / L1586-1595） | **整段删除**（3 轮未更新就注入提醒 = 死守清单反模式） |
| `agents/ws_bridge.py` | `kind == "tasks"` 分支（L709-717）的 todo 措辞 | 改为 task 看板（见决策点 5） |
| `agents/session_manage.py` | `delete_session_permanent` 内 todo 文件删除（L1296-1302） | 替换为 task 文件清理 |
| `agents/task_manager.py` | 注释中「与 todo 的 set_todo_manager 平行」（L67） | 措辞更新 |
| `tests/test_system_injection_contract.py` | `_StubTools.get_todo_manager` + `test_todo_reminder_is_wrapped`（L129-134）、L184 调用 | **必须同步改**（注入移除后该测试失效） |
| `tests/test_promise_guard.py` / `tests/test_session_id_naming.py` | `get_todo_manager` stub / `todo_file_for_session` 断言 | 保留（不引用即无害） |
| `frontend/src/**` | 全量 grep 无 todo 引用 | 无需改动 |

> 实施时配合两条既有铁律：**同文件多个 Edit 必须串行**；改完全局 grep 残留。

---

## 2. 需求的工程化定义

| 需求原文 | 工程定义 | 可验收判据 |
|---|---|---|
| 「有 task 就固定展示」 | 该会话存在**未完成组**时，面板可见 | 无未完成组 → 面板不存在（不占位） |
| 「固定高度、可有滚动条」 | 展开态高 `168px`，超出 `overflow-y:auto` | 20 条任务不撑破布局 |
| 「可缩回、可展开」 | 折叠态高 `40px`，单行摘要 | 折叠状态按 `group_id` 记忆，不被快照重置 |
| 「执行完之前不能关闭」 | `status==='running'` 时不渲染任何 dismiss 入口 | DOM 里找不到关闭按钮 |
| 「直到全部执行完」 | 最后一个任务 completed 时组关闭、面板转 `done` | 面板显示 `N/N 完成` |
| 「不跨会话」 | 任务文件名含会话 scope；`session_history` 不携带其它会话任务 | 切会话看到的组只属于该会话 |
| 「会话删除时 json 一起删」 | `delete_session_permanent` 内删该会话的任务文件（第二轮起就是那**一个** `paths.task_scope_file(scope)`，不再 glob） | 删除后 `.tasks/` 无该会话残留 |
| 「多组只显示最后执行中」 | 存在唯一未完成组（见 4.2） | 连续派 3 组活，面板只显示第 3 组 |
| 「切换/复现只显示运行中的」 | 重放只补发未完成组 | 已完成的组切回来不显示 |

---

## 3. 阶段一：下线 TodoWrite

按 §1.2 表格逐条执行。**特别强调三条**：

1. **`rounds_since_todo` 必须整段删除**。它是"每 3 轮注入 `<reminder>Update your tasks.</reminder>`"，与 Claude Code 后来删掉的"每 5 轮提醒看清单"完全同构，且该注入是 `<reminder>` 而非 `<system-reminder>`，会**漏成用户气泡**（与 `ws_bridge._history_to_ui` 的过滤前缀约定不符）。
2. `_inject_todo_reminder` 是 `<system-reminder>` 包裹的合规注入，移除后 `tests/test_system_injection_contract.py` 的对应断言必须同步删，否则测试红。
3. `todo_manager.py` **保留不删**——它是教程对齐产物，且 `paths.todo_file_for_session` 仍被测试引用。只加文件头注释标注下线。

**提示词改写**（`system_prompt._get_tools()` 尾部整段替换）：

```
# 任务看板（task）

会话级任务看板，**只活在当前会话**，不承担跨会话续接。

**何时用**：步骤 >7 / 要派 subagent / 多 agent 共享清单 / 任务间有依赖。

**规范**：
- 动手前先 create_task 把计划铺开（父子拆解用 parent_id，无依赖的用 blockedBy 声明）
- 派 subagent 前先拆好任务 → 让 subagent 用 claim_task 认领 → 完成后 complete_task 回填
- 有依赖的任务被阻塞时无法认领，这是预期行为，不要绕过
- 收尾用 list_tasks 汇总一次

**注意**：跨会话的"干到哪、下一步"一律用 write_memory 落盘，不要依赖任务板。
```

---

## 4. 阶段二：Task 数据结构完善

### 4.1 新结构（`agents/task_manager.py`）

```python
@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"          # pending | in_progress | completed
    owner: str | None = None

    blockedBy: list[str] = field(default_factory=list)

    # ── 层级（新增）──
    parentId: str | None = None      # 父任务 id；None = 根
    depth: int = 0                   # 层级深度，冗余但必要（免递归 + 卡上限）
    orderIndex: int = 0              # 同级排序
    path: str = ""                   # 物化路径 "rootId/childId"，根为自身 id
    MAX_DEPTH: ClassVar[int] = 3     # 层级上限，超出 create_task 直接拒绝

    # ── 分组（新增，需求 5）──
    group_id: str = ""               # 一次派活的分组标识

    # ── 时间与产物（新增）──
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    result: str = ""                 # 完成摘要，供面板行内展示
```

**关键设计：所有新增字段必须有默认值。** 因为 `_load_task` 走的是 `Task(**json.loads(...))`，带默认值即可让**存量旧文件直接加载**，零迁移脚本。这是本阶段最重要的实现约束。

### 4.2 组（group）语义 —— 需求 5 的核心

**定义**：一次"派活"= 一组。

**规则（纯函数，只读文件即可算出，不依赖内存状态）**：

1. `_create_task` 时：若存在「未完成组」→ 新建任务归入该组；否则生成新 `group_id = g_{int(time)}_{rand4}`。
2. 「未完成组」= 该 `group_id` 下存在 `status != completed` 的任务。
3. 「当前展示组」= 全部组中**创建时间最新**的那一组，且该组未完成。
4. 推论（重要且更强）：**同一会话内同时最多只有一个未完成组**。因为规则 1 保证未完成时不新开组。

于是需求 5（多组只显示最后正在执行的）与需求 6（已结束不显示）用**同一条判据**解决：

```python
def current_board(scope) -> dict | None:
    """返回该会话当前唯一未完成组的快照；无则 None。"""
```

### 4.3 不变量与规则

| 类型 | 规则 | 违反时 |
|---|---|---|
| 不变量 | `depth == parent.depth + 1` | `_create_task` 拒绝 |
| 不变量 | `path == f"{parent.path}/{new_id}"`，根为 `new_id` | 由实现保证 |
| 不变量 | `parentId` 不得指向自己的后代（移动时查环） | 拒绝（本期无移动工具，先留校验函数） |
| 不变量 | `depth <= MAX_DEPTH` | 拒绝并返回原因给模型 |
| 派生 | `blocked` 状态**不落盘**，由 `blockedBy` 实时计算 | 快照里输出为 `derived_status` |
| 聚合 | 父任务有子项时，其展示进度由子项 rollup（派生，不改落盘 status） | 快照里输出 `child_progress` |
| 删除 | 会话删除 → 级联清空该 scope 全部文件（复用需求 4） | §7 |

> `blocked` 取**派生而非落盘**，避免"依赖状态"与"任务状态"双写不一致——这是本方案里最容易做错的一点。

### 4.4 工具面变更（保持最小）

- `create_task` 参数新增 `parent_id`（可选）、`description` 语义收紧为"含验收标准"。
- `claim_task` / `complete_task`：`complete_task` 新增可选 `result`（完成摘要）。
- ~~**不新增** `update_task` / `fail_task`：先不加工具，避免工具膨胀；确有需要再单独立项。~~
  → **已被推翻（2026-09-18，见第 17 节）**：新增 `update_task` / `delete_task`。
  原判断错在把"工具面膨胀"当主要成本，忽略了**没有清理出口**的真实代价 ——
  模型写错一条 `blockedBy` 就再也改不回来，只能另建"修正版"新任务，
  旧任务永久留在板上把面板钉死在「执行中」。
- `list_tasks` 返回增加层级缩进与派生阻塞标记。

---

## 5. 阶段三：实时进度推送（后端）

### 5.1 事件模型：幂等快照 + revision（本方案的核心决策）

**不做增量事件。** 每次任务状态变化，推一份**当前组的完整快照**。

理由（这决定了后面所有实现细节）：

1. 面板最大 20 行，快照体积可忽略（< 4KB）；
2. 前端 store 退化为「整体替换」，**没有增量合并状态机** → 不会出现"漏一个事件状态就永久错位"；
3. 断线重连 / 会话切换 / 回放三条路径**天然幂等**，都只需"拿最后一份快照"；
4. 后台子智能体多在守护线程里改任务，多线程乱序下增量语义极难保证，快照天然免疫。

**信封**（复用既有 `UiEvent` 体系，作为「非增量产物事件」）：

```jsonc
{
  "kind": "task_board",
  "payload": {
    "session_id": "Kx7mQ2vT8p",
    "group_id": "g_1758000000_1234",
    "revision": 7,                  // 组内单调递增，用于丢弃乱序快照
    "status": "running",            // running | done
    "counts": { "total": 5, "completed": 2, "in_progress": 1,
                "pending": 1, "blocked": 1 },
    "tasks": [
      { "id": "task_session_xxx_1758000000_0001",
        "subject": "修复登录接口 500",
        "status": "in_progress",
        "derived_status": "in_progress",
        "owner": "agent-a",
        "parentId": null, "depth": 0, "orderIndex": 0,
        "blockedBy": [], "result": "",
        "started_at": 1758000000.1, "updated_at": 1758000002.3 }
    ]
  }
}
```

- 与 `AgentEvent` 区分：它不是 token 级流事件，而是 UI 产物事件，归入 `UiEvent`（与 `session_status` / `context_stats` 同类）。
- `revision` 只在本进程内保证单调；跨重连以最新快照**整体替换**为准，前端不比较跨连接 revision。

### 5.2 发射点与接线

**发射点唯一**：`TaskManager` 内所有 mutation 收敛到一个 `_emit_board()`：

```
_create_task / _claim_task / _complete_task  →  末尾统一调用 _emit_board()
                                                （未来 update_task 同样）
```

**接线（改一处即可覆盖主/子智能体）**：

```python
# task_manager.py
def set_emitter(self, emit: Callable[[dict], None] | None) -> None: ...

# session_runtime.py  —— 在 build_agent() 内、switch_session() 之后
def _bind_task_board(self, agent: Agent) -> None:
    def emit(payload: dict) -> None:
        payload["session_id"] = self.sid
        self._deliver("task_board", payload)
    agent.tools.task_manager.set_emitter(emit)
```

**为什么这样接线能覆盖子智能体**：`teammate_manager` 走的是 `self.tools.task_manager._claim_task/_complete_task`（同一实例），所以子智能体认领/完成**不需要额外接线**就自动推送。这是选择"在 TaskManager 层发射"而不是"在工具 handler 层发射"的直接收益。

**并发保护**：`_emit_board()` 内用一把 `threading.Lock` 串行化「构造快照 → 取 revision → 投递」。`SessionRuntime._deliver` 本身是线程安全的（投到事件循环队列），后台线程调用安全。

**新增事件类型的安全性**（已核对三处消费方）：

| 消费方 | 行为 |
|---|---|
| `session_runtime._note_response_event` | 只认 content/thinking/tool_call/turn_end，未知类型忽略 ✓ |
| `subagent_store._build_transcript` | 忽略未知事件类型 ✓ |
| 前端 `isKnownAgentEvent` | 未列入则忽略并告警；本事件走 `UiEvent` 通道，不入该白名单 ✓ |

另：`_emit_board()` **不得写盘额外文件**（快照是内存计算产物），也不得进入会话 jsonl。

### 5.3 无 Agent 也能算快照（重放的关键）

`ws_bridge` 在 `session_switch` / `session_history` 时**不该为了显示任务就构造一个 Agent**（成本高、且与"运行中不回放"守卫冲突）。因此提供**模块级纯函数**：

```python
# task_manager.py
def build_task_board(scope: str, tasks_dir: Path = TASKS_DIR) -> dict | None:
    """只读该 scope 的任务文件，算出当前未完成组快照；无则 None。"""
```

于是重放路径与实时路径产出**同一份结构**，前端无需区分。

### 5.4 重放接线（`ws_bridge.py`）

| 时机 | 动作 |
|---|---|
| `session_switch` 后下发 `session_history` | 紧跟补发一封 `task_board`（可为 `payload=null` 表示无） |
| 新连接状态重放（`handle` 内 `current_status` 那段） | 对当前 active 会话同样补发 |
| 会话**运行中**（`registry.is_active(sid)`） | **跳过重放**，沿用既有守卫；运行期的快照由实时通道持续推送，重放反而会覆盖成旧数据 |
| `session_delete` 成功后 | 无需补发（前端本地移除该会话 board） |

---

## 6. 阶段四：前端 UI

### 6.1 挂载与布局

挂载点：`ChatPanel.tsx` 中 `<MessageList />` 与 `<div className="composer-wrap">` **之间**（即"对话框（输入框）上方"，符合需求原文）。

```
.chat (flex column)
├── MessageList            flex:1, min-height:0
├── TaskBoard              flex:none   ← 新增，固定高度
└── .composer-wrap         flex:none
```

CSS（`styles/chat.css`）：

```css
.task-card        { flex:none; max-width:940px; width:100%; margin:0 auto;
                    border:1px solid var(--color-border); border-radius:var(--radii-btn);
                    background:var(--color-bg-main); }
.task-card__body  { height:168px; overflow-y:auto; }      /* 固定高度 + 滚动 */
.task-card--collapsed .task-card__body { display:none; }  /* 折叠态仅剩 40px 表头 */
```

### 6.2 组件结构

新文件 `components/Chat/TaskBoard.tsx`：

```
TaskBoard
├── 表头：任务进度 · 3/7   [owner chip]  [状态徽标 执行中/全部完成]  [收起/展开 ⌄]
├── 进度条：细条，completed/total
└── 列表（滚动区）
    └── TaskRow ×N
        ├── 状态点（pending ○ / in_progress ●脉冲 / blocked ⊘ / completed ✓）
        ├── 缩进（按 depth，每级 14px）
        ├── subject（折 1 行，超出 tooltip）
        ├── owner chip（子智能体名）
        └── 依赖标记（blockedBy 非空且派生为 blocked → 显示"等待 N 项"）
```

数据源：`useAgentStore(s => s.taskBoardBySession[s.activeSession ?? ''])`。

折叠状态用组件内 `useState`，并 `useEffect(() => setCollapsed(false), [group_id])` —— **按组记忆**，避免每个快照回来都重置成展开（快照是整份替换，若不按组重置会打断用户操作）。

### 6.3 关闭语义（需求 3 的硬约束）

| 组状态 | 收起/展开 | 关闭（dismiss） |
|---|---|---|
| `running` | ✅ 允许 | ❌ **不渲染任何关闭入口** |
| `done` | ✅ 允许（默认自动收起） | ✅ 显示「关闭」，点击后本会话 board 置 null |

"自动收起"实现：`status` 从 running 变 done 时置 `collapsed=true`，表头显示 `全部完成 N/N`。**不做定时消失**——用户可能想回看本轮做了什么。

### 6.4 store 增量（`store/agentStore.ts`）

```ts
// state
/** 每个会话当前的任务板快照（task_board 事件整份替换；session_history 时先置 null） */
taskBoardBySession: Record<string, TaskBoard | null>

// handleEvent 新增分支
case 'task_board': {
  const p = ev.payload
  const prev = s.taskBoardBySession[p.session_id]
  // 同组内丢弃乱序/过期快照
  if (prev && prev.group_id === p.group_id && p.revision < prev.revision) return s
  return { taskBoardBySession: { ...s.taskBoardBySession, [p.session_id]: p.tasks ? p : null } }
}
```

`protocols/agentProtocol.ts` 新增 `TaskBoard` / `TaskItem` 接口与 `UiEvent` 的 `task_board` 分支。

### 6.5 会话切换 / 回放（需求 6 的关键实现）

**必须在 `session_history` 处理里先清空该会话的 board**：

```ts
case 'session_history': {
  // 已结束的组不再下发 → 先置 null，等随后的 task_board 事件覆盖
  taskBoardBySession: { ...s.taskBoardBySession, [p.session_id]: null }
}
```

若不置 null，用户在会话 A 看到完成的组 → 切到 B → 切回 A，面板会残留上次的 `done` 快照，违反需求 6。这是本阶段最容易漏掉的一处。

---

## 7. 阶段五：生命周期与回收

### 7.1 随会话删除（需求 4）

`paths.py` 新增：

```python
def task_files_for_session(session_id: str, session_prefix: str) -> list[Path]:
    """返回该会话的任务文件：0 或 1 个（第二轮起一个会话只有一个文件）。
    路径口径唯一出处是 paths.task_scope_file，task_manager 直接 import 它。"""
```

`session_manage.delete_session_permanent` 中，把原 todo 文件删除段替换为：

```python
for p in task_files_for_session(session_id, self.session_prefix):
    try: p.unlink(missing_ok=True)
    except OSError: pass   # 单个失败不阻断会话删除（与旁路文件同策略）
```

`clear_session` 同步清空本会话 task 文件（与"清空 chat"语义对齐）。
`trash_session` / `restore_session` **不动 task 文件**（与 todo 现状一致，还原后任务还在）。

### 7.2 关闭"全完成即删文件"的 GC

现状 `_gc_scoped_tasks()`：最后一个任务 completed 时立即 `unlink` 全部文件。**建议关闭**（决策点 3），原因：

1. 与前端渲染竞争：`complete_task` 里先删文件再 emit，快照可能算不出"刚完成"的终态；
2. 违背需求 4 的语义（文件应留到会话删除）；
3. 需求 6 的"已结束不显示"是**展示层**问题，不该用删数据实现。

替代：文件保留，`current_board()` 返回 `None` → 面板自然不显示。

---

## 8. 文件改动清单（总表）

| 文件 | 类型 | 改动 |
|---|---|---|
| `agents/todo_manager.py` | 保留 | 仅加文件头「已下线」注释 |
| `agents/task_manager.py` | 引擎 | dataclass 扩字段；`group_id`/层级规则；`_emit_board`；`build_task_board`；`current_board`；`set_emitter`；`_gc_scoped_tasks` 停用；工具面微调 |
| `agents/tools.py` | 引擎 | 注释 todo 工具定义与 handler；`create_task`/`complete_task` schema 微调 |
| `agents/system_prompt.py` | 引擎 | `_get_tools()` 尾部整段改写（单套化） |
| `agents/agent_full_v2.py` | 引擎 | 注释 todo 绑定/reminder；删 nag；`clear_session`/`show_tasks` 改 task |
| `agents/paths.py` | 引擎 | 新增 `task_files_for_session` |
| `agents/session_manage.py` | 引擎 | `delete_session_permanent` task 清理；`clear_session` 同步 |
| `agents/session_runtime.py` | 薄层 | `_bind_task_board` 接线 |
| `agents/ws_bridge.py` | 薄层 | 重放补发 `task_board`；`tasks` kind 重定向 |
| `frontend/.../protocols/agentProtocol.ts` | 前端 | `TaskBoard`/`TaskItem` + `UiEvent.task_board` |
| `frontend/.../store/agentStore.ts` | 前端 | state 字段 + `handleEvent` 分支 + `session_history` 置空 |
| `frontend/.../components/Chat/TaskBoard.tsx` | 前端 | **新增** |
| `frontend/.../components/Chat/ChatPanel.tsx` | 前端 | 挂载 TaskBoard |
| `frontend/.../styles/chat.css` | 前端 | `.task-card` 系列样式 |
| `tests/test_task_board_snapshot.py` | 测试 | **新增** |
| `tests/test_session_task_cascade.py` | 测试 | **新增** |
| `tests/test_system_injection_contract.py` | 测试 | 删 todo reminder 断言 |

> 引擎层文件（`agent_full_v2.py` / `tools.py` / `system_prompt.py` / `session_manage.py` / `task_manager.py`）本次改动超出「thin-layer 仅改 ws_bridge」的默认约束，**需你明确授权**（见决策点 0）。

---

## 9. 测试计划

**新增（全部离线，用内置 `unittest`，不装包）**

`tests/test_task_board_snapshot.py`
- 多组：连续建 3 组（每组建完再完成），断言 `current_board` 只返回最后一组
- 全完成 → 返回 `None`
- `blocked` 派生：A 依赖 B（未完成）→ A 的 `derived_status == 'blocked'`
- 层级不变量：`depth == parent.depth+1`、`path` 拼接、`depth > MAX_DEPTH` 被拒
- 旧格式兼容：手写一份**缺全部新字段**的 task json，断言能加载且默认值正确（这是 4.1 设计的守门测试）
- revision 单调

`tests/test_session_task_cascade.py`
- 建会话 → 写 2 个 scoped task 文件 → `delete_session_permanent` → 断言文件全部消失
- `clear_session` → 断言 task 文件清空但会话文件保留
- `trash`/`restore` → 断言 task 文件不动

**修改**

- `tests/test_system_injection_contract.py`：移除 `test_todo_reminder_is_wrapped` 与 L184 调用（可改为断言 `_inject_todo_reminder` 已不存在，防回退）

**回归命令**

```bash
.venv/bin/python -m py_compile agents/task_manager.py agents/paths.py agents/session_manage.py \
  agents/tools.py agents/system_prompt.py agents/agent_full_v2.py agents/session_runtime.py agents/ws_bridge.py
.venv/bin/python -c "import ws_bridge, session_runtime, task_manager"
.venv/bin/python -m unittest discover -s tests -v      # 当前 87 例须全绿
cd frontend && npm run typecheck
```

---

## 10. 文档同步（强制）

| 文档 | 改动 |
|---|---|
| `docs/frontend/00-README.md` | 清单加入本篇 10 |
| `docs/frontend/02-界面功能设计.md` | 新增 Task 面板形态（挂载位置/高度/折叠/关闭语义） |
| `docs/frontend/03-前后端通信协议.md` | 新增 `task_board` UiEvent 与字段说明 |
| `docs/frontend/07-会话管理与回收站.md` | 会话删除连带 `.tasks/` 清理；trash 不动 |
| `docs/system-prompt.snapshot.md` | prompt 改动后重新生成快照 |
| `AGENTS.md` | 若含「todo/task 两套」表述需同步 |

---

## 11. 验收清单

- [ ] 模型工具列表里不再出现 `todo`
- [ ] system prompt 中不再出现 TodoWrite / 两套并存表述
- [ ] `agent_loop` 不再注入 `Update your tasks.` 提醒
- [ ] 派活后输入框上方出现面板：固定高度、可滚动、可收起/展开
- [ ] `running` 态 DOM 中无关闭按钮
- [ ] 子智能体 `claim_task`/`complete_task` → 面板 1s 内更新（后台线程路径实测）
- [ ] 全部完成 → 自动收起 + 显示 `全部完成 N/N`，出现「关闭」
- [ ] 连续派 3 组活 → 面板只显示最后一组
- [ ] 切到别会话再切回 → 已完成的组不显示
- [ ] 删除会话 → `.tasks/<scope>.json` 消失
- [ ] 旧会话（含旧 task 文件、旧 todo 文件）打开无报错
- [ ] 回归测试全绿 + `npm run typecheck` 通过

---

## 12. 需要你拍板的决策点

| # | 决策 | 我的建议 |
|---|---|---|
| 0 | 改动是否授权到引擎层（`agent_full_v2.py` 等，超出 thin-layer） | **需要授权**——下线 todo 必然要动 agent_full_v2（reminder + nag） |
| 1 | 面板挂载位置：输入框上方 / 消息区顶部 | **输入框上方**（贴合需求原文"对话框上方"） |
| 2 | 完成后行为：自动收起但保留可查 / 立即消失 | **自动收起 + 可手动关闭**（用户可能想回看本轮做了什么） |
| 3 | `_gc_scoped_tasks` 全完成即删文件 | **关闭**（文件留到会话删除，展示层控制显隐） |
| 4 | 是否加 `path` 物化路径 | **先不加**（本需求层级 ≤2 层、无折叠树；`path` 字段先留空占位，需要时再补） |
| 5 | `tasks` ControlKind（现返回 todo 文本） | **重定向为 task 看板文本**（前端已无 todo 调用点，保留接口可复用于调试） |
| 6 | 是否需要"同会话间隔过久自动开新组" | **不需要**（"未完成即归组"规则已足够，且语义更确定） |

---

## 13. 实施顺序与回滚

**顺序（每步独立可验证，不建议合并提交）**

1. 阶段一（下线 todo）→ 跑回归 → 确认 87 例全绿
2. 阶段二（数据结构）→ 新增快照单测 → 全绿
3. 阶段三（后端推送）→ 用 `python -c` 起后端，日志确认 `task_board` 事件
4. 阶段四（前端 UI）→ `npm run typecheck` + 手动验收
5. 阶段五（生命周期）→ 级联删除测试
6. 文档同步 + prompt 快照重生成

**回滚策略**

- 1-2 步：改动集中在 `task_manager.py`（新字段带默认值），回滚 = `git checkout` 单文件，存量数据天然兼容，**无数据迁移风险**。
- 3 步：`set_emitter(None)` 即静默（事件停发，任务功能不受影响）→ 可先只上后端、灰度前端。
- 4 步：UI 是纯增量组件，从 `ChatPanel` 摘掉挂载即回退。

**推荐灰度**：先落 1-3（后端能力完整、前端无变化），确认任务写入/清理正常，再上 UI。这样任一步出问题都不会同时影响"模型行为"和"用户界面"两个面。

---

## 14. 中断续跑（审核阶段追加，已实施）

> 审核时用户追问：「task 在本会话内，执行中断后，可以继续执行吧？」
> 取证结论：**数据层能，语义层原方案有三个断层**。本节记录补齐设计。

### 14.1 取证结论（可复现）

| 事实 | 证据 |
| --- | --- |
| 用户点停止 = **协作式干净收尾**；工具执行段**不检查** `stop_evt` → 已发出的工具会跑完并把结果落盘，因此正常停止**不产生孤儿 tool_calls** | `agent_full_v2.py` agent_loop 的两个检查点（迭代边界 / 流式途中 `should_stop`）；工具三阶段执行段无 stop 判断 |
| 硬中断（关窗 → `SIGTERM`，后端**无** signal handler）→ `assistant(tool_calls)` 已落盘、tool 结果未落盘 → jsonl 出现孤儿 | 落盘顺序：assistant 先于 tool |
| task 文件每次状态变更**立即落盘**（仅 3 个写入点）；唯一删除路径要求"本会话全部 completed" → **残留 pending/in_progress 必定保留** | `task_manager._save_task` 及其调用点 |
| 孤儿自愈**不删整轮**，只给缺失的 tool 响应补占位 | `session_manage._sanitize_orphan_tool_calls`（docstring 明写"绝不静默删除整轮对话"） |
| 压缩只硬保护 `messages[0]`；tool 消息**换占位不删除** | `context_compact.py` |
| 原提醒机制**只读 TodoManager**，且只在 `init_session` / `switch_session` 触发 → **同会话续轮根本不注入** | 原 `_inject_todo_reminder`（已下线） |
| `_claim_task` 前置要求 `status == "pending"`，且全仓**无任何** reset / release / 超时通道 | `task_manager._claim_task` |

### 14.2 三处断层 → 对应设计

**断层一：提醒断层** → `Agent._sync_task_board()`

替代原 `_inject_todo_reminder`（尾注：原实现有两个硬伤 —— 只认 todo、
且同会话续轮不经过它的触发点）。采用与 `<memory_index>` / `<env>` 相同的
「尾部注入 + 指纹去重」模式：

- 注入条件：**存在未完成组** 且 **历史里找不到该 `group_id` 的 `<task_board>` 注入**
- 调用点：`init_session` 末尾、`switch_session` 末尾、**`agent_loop` 每轮开头**
- 效果：进会话 / 重启 resume → 注入一次；同会话中断后再发消息 → 旧注入还在 → 不重复；
  压缩把注入段裁掉 → 自动补注
- 必须 `<system-reminder>` 包裹（前端按前缀过滤）

> **判据是"是否存在未完成组"，不是"是否发生过中断"** —— 前端切换会话并不会中断会话
> （每会话独立运行时），而"模型自己收尾时留了尾巴"同样需要续跑。

**断层二：可见性断层** → 同一注入 + 提示词规范

任务板原本不进 system prompt、不自动注入，模型必须主动 `list_tasks` 才看得到。
现由 `_sync_task_board()` 兜住，并在提示词里写明"看到 `<task_board>` 提醒就接着把
未完成项做完，不要另起一套新计划"。

**断层三：状态机断层** → `TaskManager.release_stale_in_progress()`

中断会留下**无人持有**的 `in_progress`：既不能重新 `claim`（要求 pending），
也没有释放通道。现在在 **`Agent.run_turn()` 开头**做一次归一（→ pending）：

- 放在本轮任何工具调用之前 —— 此处看到的 `in_progress` 必然属于上一轮
- 守卫 1：`background_manager.has_running()` 为真时不动（后台子智能体可能正合法持有）
- 守卫 2：只归一 `owner` 为空或 `agent` 的任务；队友（`owner=<队友名>`）可能真还在跑

> 遗留（不在本期）：队友崩溃留下的 `owner=<队友名>` 孤儿 `in_progress` 无回收通道，
> 属 teammate 生命周期问题，单独立项。

### 14.3 「继续执行」按钮 —— 已删除（2026-09-16）

原设计：按钮判据为

```
board.status === 'running' && !runningSessions.includes(sid) && !bgSessions.includes(sid)
```

即"**有活 + 现在没人在跑**"，**不检测"是否中断"**（理由见 02 篇 3.4），
点击复用既有 `send()` 通路发一条「继续完成未完成的任务」，零协议增量。

**该按钮已从前端删除**，两条理由：

1. **冗余**：本节 14.2 的两个后端机制（`_sync_task_board()` 尾部注入、
   `release_stale_in_progress()` 归一）都在 `Agent.run_turn()` 里，**与触发者无关**；
   用户手打「请继续」与点按钮走同一条 `send()` → `run_turn()` 通路，行为完全一致。
   故 UI 上不再提供按钮，也**不新增任何协议字段**（`runningSessions` / `bgSessions` 前端不再被本组件订阅）。
2. **渲染故障**：按钮误用了侧边栏的 `.mini-btn`（固定 `24×24` 纯图标样式），
   4 个汉字被压成竖向单列并溢出面板。

> 14.3 的判据思路（不检测"是否中断"）在恢复任何形式的续跑入口时仍然适用。

---

## 15. 实施记录（2026-09-16）

### 15.1 落地清单

| 文件 | 改动 |
| --- | --- |
| `agents/todo_manager.py` | 文件头加「已下线，禁止新增引用」并写明两条下线理由；代码保留 |
| `agents/tools.py` | 注释 `todo` 工具定义与 handler；`create_task` 加 `parent_id`、`complete_task` 加 `result` |
| `agents/system_prompt.py` | 尾部整段改写为单套 Task 说明，并写入"中断后继续"的规范 |
| `agents/agent_full_v2.py` | 注释 todo 绑定（3 处）与 `_inject_todo_reminder`；**整段删除 `rounds_since_todo` nag**；`clear_session`/`show_tasks` 改 task；新增 `_sync_task_board` / `_history_has_task_board`；`run_turn` 开头调 `release_stale_in_progress` |
| `agents/task_manager.py` | 扩 `Task` 字段（全带默认值）；`group_id` 分组语义；`current_board` / `latest_board` / `build_board` / `load_scope_tasks` / `scope_prefix_for`；`set_emitter` + `_emit_board`；`release_stale_in_progress`；`clear_scope`；**停用 `_gc_scoped_tasks`** |
| `agents/paths.py` | 新增 `task_files_for_session`；`todo_file_for_session` 标注已下线（**第二轮已改写**，见 §16.4：口径收敛到 `task_scope_file`，`scope_prefix_for` 删除） |
| `agents/session_manage.py` | `delete_session_permanent` / `clear_session` 连带删除本会话 task 文件 |
| `agents/session_runtime.py` | 新增 `_bind_task_board`（在 `build_agent` 内 `switch_session` 之后接线） |
| `agents/ws_bridge.py` | 新增 `_reply_task_board`，会话切换两个分支各补发一次；`tasks` kind 改读 task 看板 |
| 前端 `protocols/agentProtocol.ts` | 新增 `TaskItemStatus` / `TaskItem` / `TaskBoardSnapshot` + `UiEvent.task_board` |
| 前端 `store/agentStore.ts` | 新增 `taskBoardBySession`；`task_board` 分支（按 revision 丢弃乱序）；`session_history` 先置 `null` |
| 前端 `components/Chat/TaskBoard.tsx` | **新增**（固定高度 168px + 滚动 + 折叠 + 不可关闭 + 继续执行〔按钮已于 2026-09-16 删除〕） |
| 前端 `components/Chat/ChatPanel.tsx` / `styles/chat.css` | 挂载 + `.task-card*` 样式 |

### 15.2 相对原方案的偏差（3 处）

1. **快照信封改为嵌套**：`payload = { session_id, board }`，`board` 可为 `null`。
   原方案把字段平铺在 payload 里，但 `session_id` 由 `SessionRuntime` 注入、
   `board` 可能整体为 `null`，嵌套更干净。
2. **拆成两个快照入口**：新增 `latest_board`（实时推送用，含最后一版 `done`）与
   `current_board`（重放 / 提醒用，只看未完成组）。原方案只有一个
   `current_board` —— 会导致"最后一笔完成时前端收不到终态快照"。
3. **`clear_scope` 不再由 `Agent.clear_session` 调用**：下沉到 `SessionManager.clear_session`，
   因为 `ws_bridge` 的 `session_clear` 分支直接调它、不经过 Agent。

### 15.3 验证结果

- **回归**：`156 tests OK`（改造前基线 88 例；新增 `test_task_board_snapshot.py` 34 例、
  `test_session_task_cascade.py` 8 例、注入契约 4 例）
- **编译/导入**：`py_compile` 8 个后端文件通过；`import ws_bridge, session_runtime,
  task_manager, session_manage` 通过
- **前端**：`npm run typecheck` 通过

### 15.4 顺手修掉的两个既有测试故障（与本次改造无关）

跑回归时发现基线**本来就是红的**（16 个 ERROR/FAILURE），逐条取证后确认与本次改动无关，
一并修复以恢复回归信号：

1. **凭据环境缺失（13 例）**：`SessionManager → ContextCompact → LLMClient()` 要求
   `OPENAI_API_KEY` 与 `OPENAI_BASE_URL` 同时存在，否则抛"未配置 LLM 密钥/地址"。
   跑测试前需注入这两个环境变量。**建议后续在测试夹具里显式提供 dummy 值**，
   不要依赖外部环境（本次未改动，留作跟进项）。
2. **测试桩过期（2 个文件加载失败 + 3 例失败）**：
   - `test_subagent_sidecar.py` / `test_system_injection_contract.py` 用「截取
     `ws_bridge.py` 源码片段 exec」的手法取 `_history_to_ui`，而该片段随 ws_bridge
     演进引入了 `Optional[...]` 类型标注 → 注解在 `def` 处即求值 → `NameError`。
     已在两处**预置 `typing` 命名空间**修掉（恢复 22 例）。
   - `test_promise_guard.py` 的离线 Agent 桩缺 `_turn_usage` / `usage_totals` /
     `_in_turn` / `_turn_model_id` / `_turn_switches`（随引擎新增用量统计段而过期）
     → `agent_loop` 抛 `AttributeError` 后提前收尾。已补齐（恢复 3 例）。

> 教训：**桩直连 `agent_loop` 时必须手工补齐 `Agent.__init__` 的记账字段** ——
> 新增引擎字段时同步检查 `tests/test_promise_guard.py::_make_offline_agent`。

### 15.5 后续修订：删除「继续执行」按钮（2026-09-16）

| 文件 | 改动 |
| --- | --- |
| 前端 `components/Chat/TaskBoard.tsx` | 删除 `showResume` 判据与其按钮；顺带摘掉本组件对 `runningSessions` / `bgSessions` / `send` 的订阅（仅它用过）；文件头写明删除理由与"别复用 `.mini-btn`"的坑 |
| 前端 `styles/chat.css` | 删除 `.task-card__resume`；在 `.task-card*` 区块注释与 `.mini-btn` 使用处补注"该按钮类固定 24×24，只能放图标" |
| 文档 | 02 篇 3.4 / 10 篇 14.3 / 10 篇 15.1 / 00 篇清单 / 03 篇交叉引用同步改写 |

**动机**：① 用户实测在手打「请继续」与点按钮效果一致 → 按钮冗余；
② 按钮复用了 `.mini-btn`（固定 `24×24` 纯图标）导致中文被压成竖排、溢出面板。

**协议面零变化**：`task_board` 事件、`runningSessions` / `bgSessions` 状态本身都保留
（侧边栏脉冲点、输入框发送/停止按钮仍在用）。

---

## 16. 第二轮改造：存储布局改为「每会话一文件 + 组为 key」（2026-09-16）

### 16.1 动机

第一轮是**每任务一文件**（`.tasks/task_<scope>_<ts>_<rand>.json`）。任务一多，
`.tasks/` 迅速膨胀成几百个小文件，且：

- **分组只能从文件名"猜"**：组 id 写在每个任务体内，列表要全量读盘后按 `group_id` 归并
- **一次快照要开 N 个文件**：`current_board` / `latest_board` 是面板与中断提醒的
  数据源，每次推快照都 glob 一整轮
- 任务 id 里还得编码 scope（`task_session_Kx7mQ2vT8p_...`），每次 claim/complete
  都把这个长串送进模型上下文

### 16.2 新布局

```
~/.aigent/projects/default/.tasks/<scope>.json
{
  "version": 1,
  "scope": "session_Kx7mQ2vT8p",
  "updated_at": 1758000000.123,
  "groups": {
    "g_1758000000_0001": [ {任务}, {任务} ],   ← 一次"派活"= 一个 key
    "g_1758000123_0002": [ {任务} ]
  }
}
```

`scope` 为空（旧全局看板）落到 `_global.json`。**文件数 = 会话数**，
组id → 任务列表的映射就是 JSON 本身，不再需要"读全部再归并"。

### 16.3 三条必须守住的硬约束

1. **`group_id` 不落进任务体**：组归属由文件里的 key 承载，读盘时回填
   （`load_scope_tasks` → `task.group_id = gid`），写盘时剥离（`task_payload` 内
   `data.pop("group_id")`）。磁盘上只有一份组信息 → 不存在"体内值 ≠ 组 key"的静默不一致。
   任务体内若残留 `group_id`（手工改坏），一律**以 key 为准**。
2. **读-改-写必须持路径级可重入锁 `file_lock(path)` + 原子写**
   （同目录临时文件 + `os.replace`）：一份文件承载整会话任务，非原子写被并发读者
   撞见半截 → `read_doc` 降级为空文档 → **面板与任务板整块消失**（原方案最多丢一条，
   量级完全不同）。锁按**路径**共享，因此同一会话的多个 `TaskManager` 实例也互斥。
3. **`_save_task` 只改组内那一条**（组内按 id 替换/追加），不整份重建 →
   并发修改**不同**任务不会互相覆盖，隔离度与原「每任务一文件」等价。
   唯一的例外是 `_create_task`：整个「算组 id → 算序号 → 落盘」被放进同一把锁，
   否则主智能体与后台子智能体并发派活会开出**两个未完成组**，
   直接破坏 `current_board` 的确定性。

### 16.4 附带的两处简化

| 项 | 改动 |
| --- | --- |
| 任务 id | `task_<scope>_<ts>_<rand>` → **`t_<ts>_<rand>`**（scope 已由文件承载）。同秒内连续创建会重掷随机后缀，保证文件内唯一（上限 100 次后退化为 8 位随机数） |
| 路径口径 | 删除 `task_manager.scope_prefix_for`；新增 `paths.task_scope_file(scope, tasks_dir)` 作为**唯一**出处，`task_manager` 直接 import —— 原先两处各持一份规则，任一边漂移都会导致"清理静默失效" |
| 野 task_id | `_claim_task` / `_complete_task` 对不存在的 id 返回 `Task xxx not found`（原为抛 `FileNotFoundError`）。id 由模型给出，野 id 应是它看得懂的反馈，而不是一次工具异常 |

### 16.5 兼容与迁移

**不做迁移脚本**。第一轮方案的存量文件（`.tasks/task_<scope>_<ts>_<rand>.json`）
**不再被读取** —— 因为新代码只按 `task_scope_file` 精确读一个路径，不做 glob。
属可删的历史数据（用户在改造时确认会自行清理）。

被守护的"零迁移"约定**依然成立且含义收窄**：`Task` 新增字段一律带默认值，
缺字段的**条目**仍能直接加载（`Task(**条目)`）—— 这是结构内字段演进的兼容底线。

### 16.6 落地清单

| 文件 | 改动 |
| --- | --- |
| `agents/paths.py` | 新增 `GLOBAL_TASK_SCOPE_KEY` / `task_scope_key` / `task_scope_file` / `task_file_for_session`；`task_files_for_session` 改为返回 0..1 个路径 |
| `agents/task_manager.py` | 新增存储层 `file_lock` / `empty_doc` / `read_doc` / `write_doc` / `iter_group_tasks` / `task_payload`；`load_scope_tasks` 改读单文件；删除 `scope_prefix_for`；`_task_path` → `_find_task`（按 id 在文件内查）；`_save_task` 改为组内替换 + 原子写；`_create_task` 全程持锁 + 新 id 规则；`clear_scope` 返回任务条数；`_gc_scoped_tasks` 适配单文件（仍停用） |
| `tests/test_task_board_snapshot.py` | 新增 `TaskFileLayoutTests`（单文件/组 key/组 id 不落盘/原子写/id 唯一）、`UnknownTaskIdTests`、`ConcurrencyTests`（20 线程并发改任务不丢更新）；历史兼容用例改为「缺字段条目」与「损坏文件/损坏条目」 |
| `tests/test_session_task_cascade.py` | `seed_tasks` 播新布局；`NamingContractTests` 改为**用真实 TaskManager 写入**来验证口径一致（不再复述字符串） |
| `docs/frontend/07` | §2.3.3 改写为单文件布局 + 三条硬约束 |
| 前端 | **零改动**（`task_board` 事件与快照结构未变） |

### 16.7 验证

- **回归**：`169 tests OK`（第一轮落地后为 156 例，本次新增 13 例）
- **编译/导入**：`py_compile` + `import ws_bridge / session_runtime / session_manage / task_manager / tools / teammate_manager` 通过
- **手工验证**：单文件内三组共存；`group_id` 不在任务体；无临时文件残留；
  20 线程 × (claim+complete) 后 20 条任务全为 `completed`（无丢更新）

---

## 17. 第三轮修复：悬空依赖把整组锁死，面板假「执行中」（2026-09-18）

### 17.1 现象与用户归因

会话 `session_F2xNqhpm0t`（标题「测试任务列表并生成PDF综述大纲」）：**会话早已完成**，
任务面板却一直显示「执行中」（`2/3 · 执行中 · 等待依赖 1`），且**没有关闭入口**；
切走再切回依旧如此。用户自己的归因是"频繁点停止 + 发'请继续'导致的"。

### 17.2 取证：日志与磁盘数据（结论与归因不符）

| 时刻 | 证据（`~/.aigent/logs/agent_2026-09-16.log`） |
| --- | --- |
| 19:19:11 | `create_task` 第二个任务 `blockedBy: ["1"]` ← **祸根** |
| 19:19:25 | 模型自己排了 `bash: sleep 60 && echo waited` |
| 19:19:49 | 用户点停止 → 但工具已在跑，19:20:25 才返回（60.03s） |
| 19:20:25 | turn 以 `stopped` 结束，`t_…_7306` 留在 `in_progress` |
| 19:20:40 | 用户发「请继续」→ `[resume] 归一 1 个 in_progress → pending` → 重新 claim → complete **成功** |
| 19:20:49 | `claim_task(t_…_3474)` → `Blocked by: ['1']`；19:20:52 只好建"修正依赖版"新任务 |

**停止/请继续那条链路是健康的**：中断残留的 `in_progress` 在下一轮开头由
`release_stale_in_progress()` 归一，脚本重跑正常。它不产生"永久卡死"。

磁盘数据（`.tasks/session_F2xNqhpm0t.json`）里 `t_1789557551_3474` 的
`blockedBy: ["1"]`，而文件内根本没有 `id="1"` 的任务 → 永久 `blocked`。

### 17.3 根因（四层叠加，缺一不成）

1. **运行期语义**：`_can_start` / `build_board.derived` 把"依赖缺失"当阻塞
   （防悬空引用误执行，本意没错）→ 写错一次 = 该任务**永久**无法认领；
2. **工具面缺失**：只有 create/list/get/claim/complete，**没有 update/delete** →
   模型无法修复，只能另建"修正版"，残留项再也清不掉（且它当时在 `pending`，
   `complete_task` 只认 `in_progress`，够不到）；
3. **看板状态语义**：`build_board.status = running if any(非 completed)` ——
   "有待办"被当成"正在执行"，一条没人认领的残留也让徽标说「执行中」；
4. **交互无出口**：面板非 `done` 不渲染关闭入口 → 用户被永久钉在假「执行中」上。

回放通道把问题放大成"每次切回都复现"：`_reply_task_board` → `current_board`
按"存在未完成组"回放，该组永远未完成（2026-09-18 15:57:58 切回即复现）。

### 17.4 修复（三条出口，缺一不可）

| 层 | 改动 | 文件 |
| --- | --- | --- |
| 入口拒绝 | `_validate_blocked_by`：`blockedBy` 每个 id 必须是**本会话文件内真实存在**的 task id，否则拒绝创建/修改，报错里列出可用 id 并点明"同批并行创建的多个 `create_task` 互相拿不到 id，要先建后建" | `task_manager.py` |
| 防环 | `_reject_cycle`：改依赖时沿 `blockedBy` 遍历，命中自身即拒绝（A↔B 互等 = 双方永久死锁） | `task_manager.py` |
| 就地自愈 | 新增 `update_task`（subject / description / blockedBy / result，`blockedBy=[]` 清空依赖）+ `delete_task`（删除并**剥离别处对它的引用**；有子任务时拒绝；删空即移除整组 key）。**status 与 parentId 不可改**（不绕状态机 / 不移动子树） | `task_manager.py` / `tools.py` |
| 阻塞反馈 | `claim_task` 被拒时若命中**悬空**依赖，追加 `⚠️ 依赖不存在（悬空引用）… 请用 update_task / delete_task 收尾`（模型看不到出口只会继续另建新任务） | `task_manager.py` |
| 面板说实话 | `build_board` 增派生字段 `has_in_progress`；徽标三态（执行中／待继续／全部完成）；**停滞时开放关闭入口**（逃生阀） | `task_manager.py` / `TaskBoard.tsx` / `chat.css` |
| 提示词 | 任务看板规范补"`blockedBy` 必须是真实 id""禁止另建修正版""收工不留 pending/blocked"；`<task_board>` 尾部注入里那句自相矛盾的"用 `complete_task` 说明原因收尾"（`complete_task` 只认 `in_progress`，够不到 pending）改为 update/delete | `system_prompt.py` / `agent_full_v2.py` |

> `blockedBy` 的 id 校验**只在入口**做；运行期"依赖缺失视作阻塞"的兜底**保留不动**
> —— 存量数据里可能已有悬空引用，兜底至少不会让它误执行。

### 17.5 存量数据收尾

`session_F2xNqhpm0t.json` 的 `t_1789557551_3474` 手工收尾（备份后原地改）：
`status=completed`、`blockedBy` 修正为真实前序 id、`result` 说明"无效依赖，
已由 t_1787652_9216 修正依赖版取代"。收尾后该组闭合，`current_board` 回 `None`，
面板不再出现（已结束的组不回放）。

### 17.6 验证

- **回归**：`181 tests OK`（原 169 例；新增 `DependencyHygieneTests` 12 例 +
  改写 3 例旧断言，把"入口拒绝悬空依赖 / update 自愈 / delete 剥离引用 /
  `has_in_progress` 区分待办与执行"全部固化为契约）
- **编译/导入**：`py_compile`（task_manager / tools / system_prompt / agent_full_v2）+ `import ws_bridge` 通过
- **前端**：`npm run typecheck` 通过
- **快照**：`docs/system-prompt.snapshot.md` 已重新生成（3782 → 4250 字符）

