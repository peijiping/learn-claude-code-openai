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
| 存储 | 单文件 `.todo/session_<id>.todo.json` | 每任务一文件 `.tasks/task_<scope>_<ts>_<rand>.json` |
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
| 「会话删除时 json 一起删」 | `delete_session_permanent` 内按 scope glob 删除 | 删除后 `.tasks/` 无该会话残留 |
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
- **不新增** `update_task` / `fail_task`：先不加工具，避免工具膨胀；确有需要再单独立项。
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
    """只读 .tasks/ 下该 scope 的文件，算出当前未完成组快照；无则 None。"""
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
    """返回该会话 scope 下的全部 task 文件（task_<prefix><id>_*.json）。"""
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
- [ ] 删除会话 → `.tasks/task_<scope>_*.json` 全消失
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
