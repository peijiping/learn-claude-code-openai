#!/usr/bin/env python3
"""
task_manager.py - 任务管理模块

任务管理模块负责任务的创建、查询、更新、删除等操作。
支持任务依赖关系管理，任务状态包括：pending（待处理）、in_progress（进行中）、completed（已完成）。

## 存储布局（2026-09-16 第二次改造，替代原「每任务一文件」）

**一个会话一个 JSON，文件内以「组」为 key**（路径口径见 `paths.task_scope_file`）：

    ~/.aigent/projects/default/.tasks/session_<id>.json
    {
      "version": 1,
      "scope": "session_<id>",
      "updated_at": 1758000000.123,
      "groups": { "g_<ts>_<rand>": [ {任务}, ... ], ... }
    }

三条硬约束（改动本模块必须守住）：

1. **`group_id` 不落进任务体**：组归属由「文件里的 key」承载，读写时回填/剥离
   （`_task_payload` / `load_scope_tasks`）。单一事实来源，杜绝"体内值 ≠ 组 key"。
2. **读-改-写必须持路径级可重入锁**（`file_lock`）并**原子写**（临时文件 +
   `os.replace`）：一份文件承载整会话任务，非原子写被读到半截会让面板整块消失。
   锁按路径共享 → 同一会话的多个 TaskManager 实例也互斥。
3. **`_save_task` 只改组内那一条**，不整份重建 → 并发改不同任务不会互相覆盖
   （语义等价于原「每任务一文件」的隔离度）。

> 原方案的存量文件（`.tasks/task_<scope>_<ts>_<rand>.json`）**不再被读取**，
> 属历史数据，需人工清理（本期不做迁移脚本）。

## 2026-09-16 第一轮改造（todo 下线 + 会话内任务面板）

1. **新增字段**：层级（parentId/depth/orderIndex/path）、分组（group_id）、
   时间戳（created_at/updated_at/started_at/completed_at）、完成摘要（result）。
   ⚠️ **所有新增字段一律带默认值** —— 读盘走 `Task(**条目)`，
   带默认值才能直接读缺字段的旧条目，实现零迁移脚本。改本类时务必守住这条。
2. **新增能力**：
   - `current_board()` / `latest_board()`：面板与中断提醒的数据源（纯函数，只读磁盘）
   - `set_emitter()` + `_emit_board()`：任务状态变化时向桌面端推送**幂等快照**
   - `release_stale_in_progress()`：把中断残留的 in_progress 归一为 pending
   - `clear_scope()`：会话清空/删除时连带清理任务文件
3. **停用** `_gc_scoped_tasks` 的自动清理（原因见其 docstring）。

## 组（group）语义 —— 面板"只显示最后一组"的依据

一次"派活" = 一组。规则：**只在「不存在未完成组」时才新开组**（`_ensure_group_id`）。
由此得到不变量：**同一会话同时最多只有一个未完成组**。
于是"多组 task 只显示最后正在执行的那组"与"会话切换/回放时不显示已结束的组"
用同一条判据解决（见 `current_board`）。

## 2026-09-18 修复：悬空依赖把整组锁死（真实事故）

事故现场：会话 `session_F2xNqhpm0t`，模型 `create_task(blockedBy=["1"])`
把"第 1 条任务"当 id 写了（真实 id 是 `t_1789557551_7306`）。三处叠加把面板永久卡住：

1. 运行期把"依赖缺失"当阻塞（`_can_start` / `build_board.derived`，防悬空引用误执行）
   —— 本意没错，但**一次写错 = 该任务永久无法认领**；
2. 工具集只有 create/list/get/claim/complete，**没有 update/delete**，
   模型无法修复，只能另建"修正依赖版"补偿 → 残留项再也清不掉；
3. `build_board.status` 只看"有没有非 completed 任务"，一条没人认领的 pending
   残留也被渲染成「执行中」；前端非 done 不渲染关闭入口 → 会话已完成、面板假执行中且关不掉。

本次修复（三个出口缺一不可）：
- **入口拒绝**：`create_task` / `update_task` 校验 `blockedBy` 里的 id 必须真实存在
  （`_validate_blocked_by`）+ 拒绝成环（`_reject_cycle`）→ 悬空依赖不再产生；
- **就地自愈**：新增 `update_task`（改 subject/description/blockedBy/result）与
  `delete_task`（删除并把其它任务对它的引用一并剥离）→ 已有残留可清理；
- **面板不再说假话**：`build_board` 增派生字段 `has_in_progress`，
  前端据此把徽标分成「执行中（有 in_progress）／待继续（有活但没人跑）／全部完成」，
  且停滞时开放关闭入口 → 用户不再被锁死。

> 运行期"依赖缺失视作阻塞"的兜底**保留不动**（存量数据里可能已经有悬空引用）；
> `_claim_task` 的阻塞反馈会额外点明"悬空引用"并给出 update/delete 出口。
"""

import json
import os
import threading
import time, random
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterator

from paths import TASKS_DIR, task_scope_file
from logger import get_logger

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("task")

# 层级上限：超过则拒绝创建。防模型把任务嵌得过深导致面板缩进失控。
MAX_TASK_DEPTH = 3

# 主智能体认领任务时使用的 owner（见 run_claim_task）。
# release_stale_in_progress 只归一"空 owner 或本值"的任务 —— 队友（owner=<队友名>）
# 可能真的还在执行，不能动。
AGENT_OWNER = "agent"

# 改造前落盘的旧任务没有 group_id（空串）。读取时统一归到这个哨兵组，
# 保证"老会话没做完的活"仍算作一个未完成组，新任务会接着归进同一组，
# 从而维持"同时最多一个未完成组"的不变量。
LEGACY_GROUP_ID = "g_legacy"

# 分组 id 前缀
GROUP_PREFIX = "g_"

# 任务 id 前缀。**不再是** "task_<scope>_<ts>_<rand>"：
# 「文件即会话」之后 scope 已由文件承载，id 里再编码一遍纯属冗余
#（每次 claim/complete 都要把这个长串送进模型上下文）。
TASK_ID_PREFIX = "t_"

# 任务文件顶层版本号（未来结构升级时的判别位）
FILE_VERSION = 1


def _now() -> float:
    """当前时间戳（秒，float）。集中一处便于测试替换。"""
    return time.time()


@dataclass
class Task:
    # 任务的唯一标识符,格式: {scope前缀}{时间戳}_{4位随机数}
    id: str
    # 任务标题(简短描述,用于列表展示)
    subject: str
    # 任务详细描述(可包含具体执行要求、验收标准等)
    description: str = ""
    # 任务状态机: pending(待处理) | in_progress(进行中) | completed(已完成)
    status: str = "pending"
    # 任务认领者,多 agent 场景下记录是哪个 agent 在负责;None 表示尚未认领
    owner: str | None = None
    # 依赖任务 ID 列表:所有列出的任务必须 completed 后,本任务才能开始
    # 注意:缺失的依赖(即 ID 不存在)也会被当作阻塞,防止悬空引用
    blockedBy: list[str] = field(default_factory=list)

    # ── 层级（2026-09-16 新增）──────────────────────────────────────
    # 父任务 id；None = 根任务
    parentId: str | None = None
    # 层级深度（根为 0）。冗余但必要：免递归算深度 + 卡 MAX_TASK_DEPTH
    depth: int = 0
    # 同级排序序号（同一父任务下按此升序展示）
    orderIndex: int = 0
    # 物化路径 "根id/子id/孙id"，根为自身 id。
    # 本期留空占位：层级 ≤2 层且不做折叠树，子树查询用一次 parentId 匹配即可。
    # 未来需要"某节点的全部后代"时再补填，届时旧数据可回填。
    path: str = ""

    # ── 分组（2026-09-16 新增）──────────────────────────────────────
    # 一次"派活"= 一组。分组规则见 TaskManager._ensure_group_id：
    # 只在「不存在未完成组」时才新开组 → 不变量「同会话同时最多一个未完成组」。
    # 「多组 task 只显示最后执行中那组」的需求依赖该不变量。
    group_id: str = ""

    # ── 时间与产物（2026-09-16 新增）────────────────────────────────
    created_at: float = 0.0
    updated_at: float = 0.0
    # 认领时刻；release_stale_in_progress 用它判断"是否属于上一轮"
    started_at: float | None = None
    completed_at: float | None = None
    # 完成摘要（complete_task 可选传入），面板行内展示"这条做了什么"
    result: str = ""


# ═══════════════════════════════════════════════════════════════════
#  存储层：一个会话一个 JSON，文件内以「组」为 key
#  纯函数，不持有实例状态 —— 桌面端重放（会话切换 / 新连接）无需构造
#  Agent/TaskManager 即可算出同一份快照。
# ═══════════════════════════════════════════════════════════════════

# 同一路径的文件锁缓存：同一个会话即使被多个 TaskManager 实例持有（测试 /
# 多 Agent 场景）也必须互斥，只锁实例内部是挡不住的。
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def file_lock(path: Path) -> threading.RLock:
    """按文件路径取一把**进程内可重入**锁（线程安全，随路径去重）。

    为什么必须重入：`_create_task` 整体持锁（算组 id → 算序号 → 落盘），
    其内部又会调 `_save_task`，后者同样要持锁。非重入锁会自锁死。
    """
    key = str(path)
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


def empty_doc(scope: str | None = None) -> dict:
    """新建一份空的任务文档。"""
    return {"version": FILE_VERSION, "scope": scope or "", "updated_at": 0.0, "groups": {}}


def read_doc(scope: str | None, tasks_dir: Path = TASKS_DIR) -> dict:
    """读取该作用域的任务文件；不存在或整份损坏 → 空文档（不抛异常）。

    只认新布局（`groups` 映射）。任一环节不合法都降级为空文档并记 ERROR，
    绝不让"读盘"把上层（面板推送 / 中断提醒 / 工具调用）炸掉。
    """
    path = task_scope_file(scope, tasks_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_doc(scope)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        log.error("任务文件无法解析，按空处理 %s: %s", path.name, e)
        return empty_doc(scope)
    if not isinstance(raw, dict):
        log.error("任务文件顶层不是对象，按空处理 %s", path.name)
        return empty_doc(scope)
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        log.error("任务文件缺少合法的 groups 映射，按空处理 %s", path.name)
        groups = {}
    return {
        "version": raw.get("version", FILE_VERSION),
        "scope": raw.get("scope", scope or ""),
        "updated_at": raw.get("updated_at", 0.0),
        "groups": groups,
    }


def write_doc(scope: str | None, doc: dict, tasks_dir: Path = TASKS_DIR) -> None:
    """整份**原子**写回任务文件（同目录临时文件 + `os.replace`）。

    原子性是硬要求：单文件承载整个会话的任务，若被并发读者撞见"写了一半"，
    `read_doc` 会降级为空文档 → 面板与任务板整块消失（原「每任务一文件」
    时最多丢一条，量级完全不同）。写入失败向上抛，由调用方决定语义。
    """
    path = task_scope_file(scope, tasks_dir)
    doc["version"] = FILE_VERSION
    doc["updated_at"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.error("写入任务文件失败 %s: %s", path.name, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def iter_group_tasks(doc: dict) -> Iterator[tuple[str, dict]]:
    """遍历文档里的 (组 id, 任务原始条目 dict)，结构非法者跳过并记日志。"""
    groups = doc.get("groups")
    if not isinstance(groups, dict):
        return
    for gid, items in groups.items():
        if not isinstance(items, list):
            log.error("任务组 %s 不是数组，跳过", gid)
            continue
        for item in items:
            if not isinstance(item, dict):
                log.error("任务组 %s 内存在非对象条目，跳过", gid)
                continue
            yield gid, item


def task_payload(task: Task) -> dict:
    """任务落盘形态：`asdict` 后**剥离 group_id**。

    组归属由文件里的 key 承载（见模块头硬约束 1）。任务体内的 group_id 只在
    内存中作为便利字段存在，读盘时按 key 回填 —— 这样磁盘上不存在两份
    可能互相矛盾的组信息。
    """
    data = asdict(task)
    data.pop("group_id", None)
    return data


def load_scope_tasks(scope: str | None, tasks_dir: Path = TASKS_DIR) -> list[Task]:
    """读取某作用域下的全部任务（跨全部组，顺序 = 文件内书写顺序）。

    - 组归属以**文件结构**为准（key 即组），回填到 `Task.group_id`
    - 单条条目损坏只跳过并记日志；整份文件损坏返回空列表 —— 都不抛异常
    - 条目缺字段依赖 dataclass 默认值兜底（零迁移的前提）
    """
    out: list[Task] = []
    for gid, item in iter_group_tasks(read_doc(scope, tasks_dir)):
        try:
            task = Task(**item)
        except (TypeError, ValueError) as e:
            log.error("跳过无法解析的任务条目（组 %s）: %s", gid, e)
            continue
        task.group_id = gid
        out.append(task)
    return out


# ═══════════════════════════════════════════════════════════════════
#  模块级纯函数：只读磁盘算快照
# ═══════════════════════════════════════════════════════════════════


def group_key_of(task: Task) -> str:
    """任务的组键：兜底处理 group_id 为空的历史情况，统一归到 LEGACY_GROUP_ID。"""
    return task.group_id or LEGACY_GROUP_ID


def build_board(tasks: list[Task], group: str) -> dict:
    """把"某一组的任务"整理成前端可直接渲染的快照。

    关键设计：`blocked` 是**派生态**，不落盘 —— 由 blockedBy 实时算出，
    避免"依赖状态"与"任务状态"双写不一致。
    """
    completed_ids = {t.id for t in tasks if t.status == "completed"}
    members = [t for t in tasks if group_key_of(t) == group]

    def derived(t: Task) -> str:
        # 完成后恒为 completed；否则任一依赖缺失/未完成都算 blocked
        #（依赖缺失视作阻塞，防悬空引用 —— 与 _can_start 的判定一致）
        if t.status == "completed":
            return "completed"
        for dep in t.blockedBy:
            if dep not in completed_ids:
                return "blocked"
        return t.status

    rows: list[dict] = []
    counts = {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "blocked": 0}
    for t in sorted(members, key=lambda x: (x.depth, x.orderIndex, x.created_at, x.id)):
        ds = derived(t)
        counts["total"] += 1
        if ds in counts:
            counts[ds] += 1
        children = [c for c in members if c.parentId == t.id]
        rows.append({
            "id": t.id,
            "subject": t.subject,
            "status": t.status,
            "derived_status": ds,
            "owner": t.owner,
            "parentId": t.parentId,
            "depth": t.depth,
            "orderIndex": t.orderIndex,
            "blockedBy": list(t.blockedBy),
            "result": t.result,
            "started_at": t.started_at,
            "updated_at": t.updated_at,
            # 父任务进度（由子项派生，不改落盘的 status）
            "child_total": len(children),
            "child_completed": sum(1 for c in children if c.status == "completed"),
        })

    status = "running" if any(t.status != "completed" for t in members) else "done"
    return {
        "group_id": group,
        "revision": 0,  # 由 TaskManager._emit_board 填进程内单调序号
        "status": status,
        # 派生字段（不落盘）：组内**是否真有一条 in_progress**。
        # `status` 只回答"这组活干完没有"，回答不了"现在有人跑吗" ——
        # 一条没人认领的 pending/blocked 残留会让 status 长期停在 running，
        # 前端若直接拿它当"执行中"，就会出现"会话已完成但面板显示执行中"
        # （2026-09-18 事故）。面板徽标必须以本字段为准。
        "has_in_progress": any(t.status == "in_progress" for t in members),
        "counts": counts,
        "tasks": rows,
    }


def current_board(scope: str | None, tasks_dir: Path = TASKS_DIR) -> dict | None:
    """**仅当存在未完成组时**返回该组快照，否则 None。

    用于两处（二者都是"只看活着的那组"的语义）：
    1. 会话切换 / 新连接重放（需求：只显示正在运行的组，已结束的不显示）；
    2. `Agent._sync_task_board()` 的中断提醒注入。

    与 latest_board 的分工：本函数看不到已完成的组，所以回放时不会把
    上一轮做完的组又画出来。
    """
    tasks = load_scope_tasks(scope, tasks_dir)
    unfinished = [t for t in tasks if t.status != "completed"]
    if not unfinished:
        return None
    newest = max(unfinished, key=lambda t: (t.created_at or 0.0, t.id))
    return build_board(tasks, group_key_of(newest))


def latest_board(scope: str | None, tasks_dir: Path = TASKS_DIR) -> dict | None:
    """**最近一组**的快照（不论是否已完成）；一条任务都没有时 None。

    用于实时推送：最后一件事完成的那一刻也要推一版 `status="done"`，
    前端据此自动收起面板并显示「关闭」入口。若改用 current_board，
    最后一笔完成时只会推 None，前端就看不到"全部完成 N/N"了。
    """
    tasks = load_scope_tasks(scope, tasks_dir)
    if not tasks:
        return None
    newest = max(tasks, key=lambda t: (t.created_at or 0.0, t.id))
    return build_board(tasks, group_key_of(newest))


# -- TaskManager: 支持依赖关系图的CRUD操作，数据持久化为JSON文件 --
class TaskManager:
    """
    任务管理器类

    提供任务的增删改查功能，支持任务之间的依赖关系管理。

    **存储**：一个会话一个 JSON（`paths.task_scope_file`），文件内以组为 key，
    多组任务共存于同一文件。详见模块头「存储布局」。

    作用域（scope）把任务限定在某个会话内（如 "session_Kx7mQ2vT8p" / "cron_1"）；
    scope 为 None 时是旧全局看板（向后兼容，落到 `_global.json`）。
    """

    def __init__(self, tasks_dir: Path | None = None):
        """
        初始化任务管理器

        Args:
            tasks_dir: 任务数据存储目录的路径,不传则默认使用 TASKS_DIR
        """
        self.task_dir = tasks_dir if tasks_dir else TASKS_DIR  # 任务文件存储目录
        # 先判断再创建：目录已存在时跳过 mkdir，避免运行环境的文件代理对
        # exist_ok=True 的 mkdir 也误报 EEXIST 导致启动崩溃
        if not self.task_dir.exists():
            self.task_dir.mkdir(parents=True, exist_ok=True)
        # 作用域：会话内任务板用它把任务限定在某个会话（如 "session_3" / "cron_1"）。
        # None 表示旧的全局看板（无会话上下文，向后兼容）。
        self.scope: str | None = None
        # 实时推送回调（由桌面端 SessionRuntime 注入；CLI 场景为 None → 不推送）
        self._emitter: Callable[[dict], None] | None = None
        # 快照 revision 计数：进程内单调，仅用于让前端丢弃乱序快照。
        # 后台子智能体会在多线程里改任务，没有它就无法排除乱序。
        self._revision = 0
        self._emit_lock = threading.Lock()

    def set_scope(self, scope: str | None) -> None:
        """
        设置作用域。作用域为 None 时是全局看板；非空时任务只在本会话内可见/可操作。

        由 Agent 在切换会话时调用（会话初始化 / 新建 / 切换三处），
        scope 值取 f"{session_prefix}{session_id}"，如 "session_Kx7mQ2vT8p"。
        """
        self.scope = scope

    # ── 实时推送（桌面端任务面板）──────────────────────────────────

    def set_emitter(self, emit: Callable[[dict], None] | None) -> None:
        """注入快照推送回调；传 None 关闭推送（CLI / 测试场景）。

        回调收到的是 `{"board": <快照或 None>}`。会话 id 由调用方
        （SessionRuntime）补进信封，TaskManager 不感知传输层。
        """
        self._emitter = emit

    def _emit_board(self) -> None:
        """推送一次当前快照（幂等整份替换，不做增量）。

        用锁把「生成快照 → 取 revision → 投递」串行化：
        主智能体（工具调用）与后台子智能体（daemon 线程）会并发触发本函数，
        不加锁会出现同一 revision 对应两份不同快照。
        """
        emit = self._emitter
        if emit is None:
            return
        with self._emit_lock:
            board = latest_board(self.scope, self.task_dir)
            self._revision += 1
            if board is not None:
                board["revision"] = self._revision
            try:
                emit({"board": board})
            except Exception as e:
                # 推送失败绝不能影响任务本身（任务已落盘）
                log.error("任务快照推送失败: %s: %s", type(e).__name__, e)

    def _scope_file(self) -> Path:
        """当前作用域对应的任务文件（唯一那个）。口径来自 paths，不在此重写规则。"""
        return task_scope_file(self.scope, self.task_dir)

    @contextmanager
    def _locked(self):
        """「本会话任务文件」的读-改-写临界区（可重入，同路径实例间也互斥）。

        加锁粒度选在**文件**而非单条任务：一份文件承载整个会话，任何一次
        落盘都是"读整份 → 改一条 → 写整份"，不串行化就会丢更新
        （主智能体与后台子智能体各在一条线程改任务是最常见场景）。
        """
        with file_lock(self._scope_file()):
            yield

    def _read_doc(self) -> dict:
        """读整份文档（不存在/损坏 → 空文档）。"""
        return read_doc(self.scope, self.task_dir)

    def _write_doc(self, doc: dict) -> None:
        """原子写回整份文档。"""
        write_doc(self.scope, doc, self.task_dir)

    # ── 分组 ────────────────────────────────────────────────────────

    def _unfinished_group(self) -> str | None:
        """返回当前未完成组的组键；没有未完成任务时 None。

        理论上最多只有一个（`_ensure_group_id` 保证），这里取"创建时间最新"
        的未完成任务所属组，即使不变量被外部破坏也能给出确定结果。
        """
        unfinished = [t for t in self._list_tasks() if t.status != "completed"]
        if not unfinished:
            return None
        newest = max(unfinished, key=lambda t: (t.created_at or 0.0, t.id))
        return group_key_of(newest)

    def _ensure_group_id(self) -> str:
        """本次创建应归属的组 id：存在未完成组则复用，否则新开一组。

        ⚠️ 这是"同时最多一个未完成组"不变量的唯一守卫点。若改成"每次创建都新开组"，
        「多组 task 只显示最后执行中那组」与「回放不显示已结束组」两条需求
        会立刻失去确定性（无法判断哪组是"当前"的）。
        """
        gid = self._unfinished_group()
        if gid:
            return gid
        return f"{GROUP_PREFIX}{int(_now())}_{random.randint(0, 9999):04d}"

    # ── 校验 ────────────────────────────────────────────────────────

    def _validate_parent(self, parent_id: str) -> Task:
        """校验父任务存在、深度未超限；返回父任务对象（失败抛 ValueError）。

        本期没有"移动任务"的工具，所以无需查环；将来加移动能力时，
        必须在此处补"parentId 不得指向自己的后代"的校验。
        """
        parent = self._find_task(parent_id)
        if parent is None:
            raise ValueError(f"parent task not found: {parent_id}")
        if parent.depth + 1 > MAX_TASK_DEPTH:
            raise ValueError(
                f"max task depth {MAX_TASK_DEPTH} exceeded "
                f"(parent {parent_id} depth={parent.depth})"
            )
        return parent

    def _validate_blocked_by(self, blocked_by: list[str] | None,
                             exclude_id: str | None = None) -> list[str]:
        """校验 `blockedBy` 里每个 id 都是**本会话文件内真实存在**的任务 id，返回去重后的列表。

        为什么必须在入口拒绝（2026-09-18 事故后新增）：运行期把"依赖缺失"当阻塞
        （`_can_start` / `derived`）是**兜底**，而一次写错 id 会让该任务**永久**
        无法认领 —— 组永远完不成 → 面板永久停在「执行中」。
        真实事故：模型写了 `blockedBy=["1"]`（把"第 1 条任务"当 id），
        于是 t_1789557551_3474 永久 pending，用户看到"会话已完成但任务还在执行中"。

        错误信息要能给模型**可执行的下一步**：列出可用 id、并点明"同批并行创建的
        任务之间无法互相引用 id"（模型当时正是因为拿不到真实 id 才写了 "1"）。
        """
        deps: list[str] = []
        for dep in blocked_by or []:
            if not isinstance(dep, str) or not dep.strip():
                raise ValueError(f"依赖 id 必须是非空字符串，收到 {dep!r}")
            dep = dep.strip()
            if dep == exclude_id:
                raise ValueError(f"任务不能依赖自己: {dep}")
            if dep not in deps:
                deps.append(dep)
        if not deps:
            return deps

        known = {t.id: t.subject for t in self._list_tasks()}
        missing = [d for d in deps if d not in known]
        if missing:
            preview = "; ".join(
                f"{tid}（{subj}）" for tid, subj in list(known.items())[:10]
            ) or "（本会话暂无其它任务）"
            raise ValueError(
                f"依赖 id 不存在: {missing}。blockedBy 必须填**真实 task id**"
                f"（形如 t_<时间戳>_<随机数>），不能写序号、'任务1' 这类自然语言。"
                f"现有任务: {preview}。"
                f"若要依赖同批新建的前序任务：先 create_task 建好它、拿到 id 后再建本任务"
                f"（同一次响应里并行发出的多个 create_task 互相拿不到 id）"
            )
        return deps

    def _reject_cycle(self, task_id: str, deps: list[str]) -> None:
        """拒绝对依赖成环的修改（A 依赖 B、B 又依赖 A → 双方永久死锁）。

        创建路径天然不需要（新 id 不可能已在环上）；只有 `update_task` 改依赖时才可能
        把两条任务连成一个环，故由本函数守。遍历沿 blockedBy 走，命中 task_id 即成环。
        """
        by_id = {t.id: t for t in self._list_tasks()}
        stack = list(deps)
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur == task_id:
                raise ValueError(
                    f"依赖成环: {task_id} 依赖 {deps} 会绕回自身，双方都永远无法认领"
                )
            if cur in seen:
                continue
            seen.add(cur)
            node = by_id.get(cur)
            if node is not None:
                stack.extend(node.blockedBy)

    def _next_order_index(self, parent_id: str | None) -> int:
        """同级追加序号：取同父任务里最大的 orderIndex + 1（无则 0）。"""
        siblings = [t.orderIndex for t in self._list_tasks() if t.parentId == parent_id]
        return max(siblings, default=-1) + 1

    # ── 落盘与读取 ──────────────────────────────────────────────────

    def _new_task_id(self, taken: set[str]) -> str:
        """生成**本会话文件内**唯一的任务 id：`t_<秒级时间戳>_<4位随机数>`。

        id 不再编码 scope（文件即会话）。同秒内连续创建多个任务时随机后缀可能
        撞车，故重掷（撞车探测基于本次已读到的全部 id）；极端情况退化为
        8 位随机后缀，保证一定不重复。
        """
        for _ in range(100):
            tid = f"{TASK_ID_PREFIX}{int(_now())}_{random.randint(0, 9999):04d}"
            if tid not in taken:
                return tid
        return f"{TASK_ID_PREFIX}{int(_now())}_{random.randint(0, 99999999):08d}"

    def _save_task(self, task: Task):
        """把一条任务**写回它所属的组**（整份文件原子覆盖，每次状态变更都要调用）。

        关键点（对应模块头硬约束 2/3）：
        - 持 `_locked()` 完成「读整份 → 组内按 id 替换/追加 → 原子写」；
        - **只改组内那一条**，不动其它组 —— 于是并发修改不同任务不会互相覆盖，
          语义等价于原「每任务一文件」的隔离度。
        """
        task.updated_at = _now()
        with self._locked():
            doc = self._read_doc()
            bucket = doc["groups"].setdefault(group_key_of(task), [])
            payload = task_payload(task)
            for i, item in enumerate(bucket):
                if isinstance(item, dict) and item.get("id") == task.id:
                    bucket[i] = payload
                    break
            else:
                bucket.append(payload)
            self._write_doc(doc)

    def _load_task(self, task_id: str) -> Task:
        """在当前会话文件内按 id 找任务；找不到抛 FileNotFoundError。

        依赖"新增字段全部带默认值"来兼容缺字段的条目（缺字段即取默认值）。
        """
        for task in self._list_tasks():
            if task.id == task_id:
                return task
        raise FileNotFoundError(f"task not found: {task_id}")

    def _find_task(self, task_id: str) -> Task | None:
        """`_load_task` 的不抛异常版本（用于存在性判定：父任务、依赖项）。"""
        try:
            return self._load_task(task_id)
        except FileNotFoundError:
            return None

    def _list_tasks(self) -> list[Task]:
        """
        列出当前作用域下所有任务（跨全部组，文件内书写顺序）。

        作用域过滤：scope 非空时只读该会话的文件；
        scope 为 None 时读全局哨兵文件（旧全局看板，向后兼容）。
        """
        return load_scope_tasks(self.scope, self.task_dir)

    def _get_task(self, task_id: str) -> str:
        """返回任务的完整 JSON 详情字符串,供模型查看完整上下文。"""
        task = self._load_task(task_id)
        return json.dumps(asdict(task), indent=2, ensure_ascii=False)

    def _can_start(self, task_id: str) -> bool:
        """
        判断指定任务是否可以开始。

        判定规则:
        1) 遍历 task.blockedBy 列表中的每个依赖 ID
        2) 若依赖在本会话文件内不存在(被删除/拼写错误) → 视为阻塞(返回 False)
            这样可以防止 agent 引用悬空 ID 时误执行
        3) 若依赖存在但 status != "completed" → 阻塞
        4) 所有依赖都 completed 才返回 True
        """
        task = self._load_task(task_id)
        for dep_id in task.blockedBy:
            dep = self._find_task(dep_id)
            if dep is None:
                return False
            if dep.status != "completed":
                return False
        return True

    # ── 状态流转 ────────────────────────────────────────────────────

    def _create_task(self, subject: str, description: str = "",
                    blockedBy: list[str] | None = None,
                    parent_id: str | None = None) -> Task:
        """
        创建一个新任务。

        - 自动生成**本会话内唯一**的 ID(`t_<时间戳>_<随机数>`)，同秒撞车自动重掷
        - 初始状态为 pending,owner 为 None(尚未被认领)
        - 立即落盘到本会话的任务文件,确保创建即持久
        - 可选 blockedBy 用于声明对其他任务的依赖(实现 DAG 编排)
        - 可选 parent_id 声明从属关系（拆子树）；depth/orderIndex 自动推导，
          超 MAX_TASK_DEPTH 直接拒绝

        整个「算组 id → 算序号 → 落盘」在文件锁内完成：组不变量的守卫点
        （`_ensure_group_id`）若与落盘之间存在窗口，主智能体与后台子智能体
        并发派活会开出两个"未完成组"，直接破坏 `current_board` 的确定性。

        `blockedBy` 在锁内先过 `_validate_blocked_by`（id 必须真实存在）——
        非法依赖直接拒绝创建，不落盘，从源头消灭"悬空依赖永久卡死"。
        """
        with self._locked():
            deps = self._validate_blocked_by(blockedBy)
            taken = {t.id for t in self._list_tasks()}
            depth = 0
            if parent_id:
                parent = self._validate_parent(parent_id)
                depth = parent.depth + 1

            now = _now()
            task = Task(
                id=self._new_task_id(taken),
                subject=subject,
                description=description,
                status="pending",
                owner=None,
                blockedBy=deps,
                parentId=parent_id or None,
                depth=depth,
                orderIndex=self._next_order_index(parent_id or None),
                group_id=self._ensure_group_id(),
                created_at=now,
                updated_at=now,
            )
            self._save_task(task)
        # 推送放在锁外：emit 内部还要读一遍文件算快照，持锁做推送会无谓拉长临界区
        self._emit_board()
        return task

    def _claim_task(self, task_id: str, owner: str = AGENT_OWNER) -> str:
        """
        认领一个 pending 任务:把任务从 pending 推进到 in_progress。

        流程:
        1) 从磁盘重新读取任务,获取最新状态(避免基于内存里的陈旧副本决策)
        2) 状态必须是 pending,否则拒绝(已认领或已完成的任务不能再认领)
           ⚠️ 中断会留下"无人持有"的 in_progress —— 那条路径由
           release_stale_in_progress() 在下一轮开头归一为 pending 来解决，
           本函数**不放宽**状态前置条件（否则无法区分"正在跑"与"被遗弃"）。
        3) 调用 can_start 检查依赖;若仍被阻塞,返回具体阻塞原因(哪些依赖未完成)
        4) 通过校验后:设置 owner 字段,状态置为 in_progress,立即落盘
        5) 推送快照 + 打印日志,方便前端实时进度与终端观察

        id 不存在时返回可读字符串而不是抛 FileNotFoundError —— task_id 由模型
        给出，野 id 应该变成它看得懂的反馈，而不是一次工具异常。
        """
        task = self._find_task(task_id)
        if task is None:
            return f"Task {task_id} not found"
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if not self._can_start(task_id):
            # 收集所有未满足的依赖 ID,精确告知调用方卡在哪里
            deps = []
            for d in task.blockedBy:
                dep = self._find_task(d)
                if dep is None or dep.status != "completed":
                    deps.append(d)
            msg = f"Blocked by: {deps}"
            # 悬空依赖（id 根本不存在）是**永久**阻塞：普通"等依赖完成"能自愈，
            # 这种不能。必须点明出口，否则模型只会另建"修正版"新任务，
            # 把旧任务永久留在板上（2026-09-18 事故的直接成因）。
            dangling = [d for d in task.blockedBy if self._find_task(d) is None]
            if dangling:
                msg += (f"\n⚠️ 依赖不存在（悬空引用）: {dangling}"
                        f" —— 请用 update_task 把 blockedBy 改成真实 id 或清空，"
                        f"或用 delete_task 删掉本任务；否则本任务永久无法认领。")
            return msg
        task.owner = owner
        task.status = "in_progress"
        task.started_at = _now()
        self._save_task(task)
        self._emit_board()
        log.info("[claim] %s → in_progress (owner: %s)", task.subject, owner)
        return f"Claimed {task.id} ({task.subject})"

    def _complete_task(self, task_id: str, result: str = "") -> str:
        """
        将 in_progress 任务标记为 completed。

        关键副作用(重要!):
        - 完成后会扫描所有 pending 任务,找出"因为本次完成而新解锁"的下游任务
        - 即:该任务的 ID 出现在它们的 blockedBy 列表中、且其他依赖也已完成的任务
        - 打印黄色 [unblocked] 日志,提醒 agent 优先调度这些可执行任务
        - 这种"完成即触发依赖检查"的模式是 DAG 调度器的核心机制

        result: 可选完成摘要，写入任务记录供面板行内展示。
        """
        task = self._find_task(task_id)
        if task is None:
            return f"Task {task_id} not found"
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        task.status = "completed"
        task.completed_at = _now()
        if result:
            task.result = result
        self._save_task(task)
        # 找出所有因为本次完成而新解锁的待办任务
        unblocked = [t.subject for t in self._list_tasks()
                    if t.status == "pending" and t.blockedBy and self._can_start(t.id)]
        log.info("[complete] %s ✓", task.subject)
        msg = f"Completed {task.id} ({task.subject})"
        if unblocked:
            msg += f"\nUnblocked: {', '.join(unblocked)}"
            log.warning("[unblocked] %s", ", ".join(unblocked))
        # 快照要在状态写盘之后推：最后一笔完成时也要推 status="done" 的那一版，
        # 前端据此自动收起并显示「关闭」。
        self._emit_board()
        return msg

    def _update_task(self, task_id: str, subject: str | None = None,
                     description: str | None = None,
                     blockedBy: list[str] | None = None,
                     result: str | None = None) -> Task:
        """就地修正一条任务（subject / description / blockedBy / result）。

        **存在的理由**（2026-09-18 新增）：模型把 `blockedBy` 写成不存在的 id 时，
        旧工具集只能另建"修正依赖版"新任务收尾，旧任务永久留在板上
        （`claim_task` 被拒、`complete_task` 只认 in_progress 够不到），
        面板再也回不到「全部完成」。本方法提供**就地自愈**出口：
        `update_task(blockedBy=[<真实 id>])` 或 `update_task(blockedBy=[])` 清空依赖，
        再正常 claim → complete 即可，不需要新增任何任务。

        刻意不提供的能力：
        - **status 不可改**：状态流转只走 claim/complete，避免绕开状态机；
        - **parentId 不可改**：移动子树要连带重算 depth/orderIndex（与
          `_validate_parent` 的"本期不做移动"保持一致）。
        """
        with self._locked():
            task = self._find_task(task_id)
            if task is None:
                raise ValueError(f"task not found: {task_id}")
            deps: list[str] | None = None
            if blockedBy is not None:
                deps = self._validate_blocked_by(blockedBy, exclude_id=task_id)
                self._reject_cycle(task_id, deps)
            if subject is not None:
                if not subject.strip():
                    raise ValueError("subject 不能为空")
                task.subject = subject.strip()
            if description is not None:
                task.description = description
            if result is not None:
                task.result = result
            if deps is not None:
                task.blockedBy = deps
            self._save_task(task)
        # 推送放锁外（与 _create_task 同款理由：emit 内部还要再读一次文件）
        self._emit_board()
        return task

    def _delete_task(self, task_id: str) -> tuple[Task, list[str]]:
        """删除一条任务，并**剥离**其它任务对它的依赖引用。返回 (被删任务, 被解除引用的任务标题)。

        收尾出口之二（与 `_update_task` 并列）：不再需要的残留项直接删掉，
        别靠另建新任务"绕过"。

        三条硬规则：
        1. **有子任务时拒绝**（先删子任务）—— 否则留下指向不存在父任务的孤儿条目；
        2. **删除后必须剥掉别处 blockedBy 里的该 id** —— 不剥的话引用方立刻变成
           "悬空依赖"永久阻塞，等于把死锁换个地方复现；
        3. **组内最后一条被删光时整组 key 一并移除** —— 不留空组（空组会让
           `latest_board` 拿到一条任务都没有的组、面板闪现又消失）。
        """
        with self._locked():
            task = self._find_task(task_id)
            if task is None:
                raise ValueError(f"task not found: {task_id}")
            children = [t.subject for t in self._list_tasks() if t.parentId == task_id]
            if children:
                raise ValueError(
                    f"任务 {task_id} 还有 {len(children)} 个子任务"
                    f"（{', '.join(children[:5])}），请先删子任务再删父任务"
                )
            doc = self._read_doc()
            groups = doc.get("groups") or {}
            gid = group_key_of(task)
            bucket = groups.get(gid)
            removed = False
            if isinstance(bucket, list):
                kept = [it for it in bucket
                        if not (isinstance(it, dict) and it.get("id") == task_id)]
                removed = len(kept) != len(bucket)
                if removed:
                    if kept:
                        groups[gid] = kept
                    else:
                        del groups[gid]      # 不留空组
            if not removed:
                raise ValueError(f"task not found: {task_id}")

            released: list[str] = []
            for items in groups.values():
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    deps = item.get("blockedBy") or []
                    if task_id in deps:
                        item["blockedBy"] = [d for d in deps if d != task_id]
                        released.append(str(item.get("subject") or item.get("id")))
            self._write_doc(doc)

        log.warning("[delete] %s (%s)%s", task.subject, task_id,
                    f"，解除 {len(released)} 处引用" if released else "")
        self._emit_board()
        return task, released

    # ── 中断恢复 ────────────────────────────────────────────────────

    def release_stale_in_progress(self, stale_before: float | None = None) -> int:
        """把「上一轮遗留」的 in_progress 归一为 pending，返回被归一的数量。

        为什么必须做：`_claim_task` 要求 pending，而中断（用户点停止 / 进程被杀 /
        模型自己收尾时留尾巴）会留下**无人持有**的 in_progress —— 不归一的话
        模型既不能重新认领（被拒："is in_progress, cannot claim"），只剩
        "直接 complete" 或"人工删文件"两条路，任务板语义就废了。

        两条守卫（缺一不可）：
        1) **调用方**须确保本会话当前没有后台任务在跑 —— 后台子智能体可能正
           合法持有某个 in_progress。生产路径在 `Agent.run_turn` 开头做该判断。
        2) 只归一 owner 为空或 `AGENT_OWNER` 的任务：队友（owner=<队友名>）
           可能真的还在执行，不能动。

        stale_before: 只归一 started_at 早于该时刻的任务（防误伤刚认领的）。
        """
        released = 0
        for t in self._list_tasks():
            if t.status != "in_progress":
                continue
            if t.owner not in (None, "", AGENT_OWNER):
                continue  # 队友持有，跳过
            if stale_before is not None and (t.started_at or 0.0) >= stale_before:
                continue
            t.status = "pending"
            t.owner = None
            t.started_at = None
            self._save_task(t)
            released += 1
        if released:
            log.warning("[resume] 归一 %d 个中断残留的 in_progress → pending", released)
            self._emit_board()
        return released

    # ── 生命周期 ────────────────────────────────────────────────────

    def clear_scope(self) -> int:
        """删除当前作用域的任务文件，返回被清掉的**任务条数**。

        用于会话清空（chat 与任务板同生共死）与会话永久删除。
        scope 为空时**拒绝执行** —— 否则会误删旧全局看板。

        整份文件删除是原子的：不会出现"删了一半"的半残状态。
        """
        if not self.scope:
            return 0
        path = self._scope_file()
        with self._locked():
            existed = path.exists()
            removed = len(self._list_tasks())
            try:
                path.unlink()
            except FileNotFoundError:
                existed = False
            except OSError as e:
                log.error("删除任务文件失败 %s: %s", path.name, e)
                return 0
        if existed:
            log.warning("[clear] session '%s' 清理任务文件 %s（%d 条任务）",
                        self.scope, path.name, removed)
            self._emit_board()  # 推 None → 前端撤掉面板
        return removed

    def _gc_scoped_tasks(self) -> int:
        """
        【已停用 2026-09-16，保留以备回滚】

        原行为：本会话任务全部 completed 时，立即删除本会话的全部任务文件。

        停用原因（三条）：
        1. 与前端渲染竞争：`_complete_task` 里"删文件"发生在推送快照之前，
           最后一笔完成时可能算不出终态快照，面板看不到"全部完成"；
        2. 与"任务文件保留到会话删除"的约定冲突（会话删除才由 clear_scope 清理）；
        3. "已结束的组不显示"是**展示层**问题（见 current_board），不该用删数据实现。

        对应需求现在由展示层承担：`current_board()` 无未完成组时返回 None。
        """
        if not self.scope:
            return 0
        path = self._scope_file()
        with self._locked():
            tasks = self._list_tasks()
            if not tasks or any(t.status != "completed" for t in tasks):
                return 0
            if not path.exists():
                return 0
            path.unlink(missing_ok=True)
        log.warning("[gc] session '%s' 任务已全部完成，清理任务文件 %s（%d 条）",
                    self.scope, path.name, len(tasks))
        return len(tasks)

    # ── Task tools (面向模型工具调用的薄包装层) ──
    # 这些 run_* 函数是核心业务函数(create_task / list_tasks 等)与 LLM 工具调用之间的桥梁。
    # 主要职责:
    #   1. 参数透传到业务函数
    #   2. 记录执行日志,方便观察 agent 行为
    #   3. 决定返回给模型的字符串格式(简洁、便于模型解析)

    def run_create_task(self, subject: str, description: str = "",
                        blockedBy: list[str] | None = None,
                        parent_id: str | None = None) -> str:
        """
        工具入口:创建任务。返回任务 ID 与依赖信息；非法父任务/超深返回可读错误。
        """
        try:
            task = self._create_task(subject, description, blockedBy, parent_id)
        except ValueError as e:
            return f"Error: {e}"
        deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
        sub = f" (parent: {parent_id})" if parent_id else ""
        log.info("[create] %s%s%s", task.subject, deps, sub)
        return f"Created {task.id}: {task.subject}{deps}{sub}"


    def run_list_tasks(self) -> str:
        """
        工具入口:列出当前作用域全部任务，带状态图标、层级缩进、依赖与分组概览。
        图标约定: ○ pending / ● in_progress / ⊘ blocked（派生）/ ✓ completed
        """
        tasks = self._list_tasks()
        if not tasks:
            return "No tasks. Use create_task to add some."
        completed_ids = {t.id for t in tasks if t.status == "completed"}
        lines = []
        for t in sorted(tasks, key=lambda x: (x.group_id, x.depth, x.orderIndex, x.created_at, x.id)):
            if t.status == "completed":
                icon = "✓"
            elif any(d not in completed_ids for d in t.blockedBy):
                icon = "⊘"
            else:
                icon = {"pending": "○", "in_progress": "●"}.get(t.status, "?")
            indent = "  " * t.depth
            deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
            owner = f" [{t.owner}]" if t.owner else ""
            lines.append(f"  {icon} {indent}{t.id}: {t.subject} "
                        f"[{t.status}]{owner}{deps}")
        return "\n".join(lines)

    def run_get_task(self, task_id: str) -> str:
        """工具入口:获取任务完整 JSON 详情;找不到时返回友好错误而非抛异常。"""
        try:
            return self._get_task(task_id)
        except FileNotFoundError:
            return f"Error: Task {task_id} not found"

    def run_claim_task(self, task_id: str) -> str:
        """工具入口:认领任务(默认 owner=agent)。业务逻辑在 claim_task 内。"""
        return self._claim_task(task_id, owner=AGENT_OWNER)

    def run_complete_task(self, task_id: str, result: str = "") -> str:
        """工具入口:完成任务;若解锁了下游任务,会附带 unblocked 提示。"""
        return self._complete_task(task_id, result=result)

    def run_update_task(self, task_id: str, subject: str | None = None,
                        description: str | None = None,
                        blockedBy: list[str] | None = None,
                        result: str | None = None) -> str:
        """工具入口:就地修正任务（改依赖/标题/说明/完成摘要）。

        非法依赖（不存在 / 成环 / 空串）返回可读错误而非抛异常 —— 与
        `run_create_task` 同一约定：野参数要变成模型看得懂的反馈。
        """
        try:
            task = self._update_task(task_id, subject=subject,
                                     description=description,
                                     blockedBy=blockedBy, result=result)
        except ValueError as e:
            return f"Error: {e}"
        fields = [n for n, v in (("subject", subject), ("description", description),
                                 ("blockedBy", blockedBy), ("result", result))
                  if v is not None]
        log.info("[update] %s 改了 %s", task.id, "/".join(fields) or "（无字段）")
        deps = f" (blockedBy: {', '.join(task.blockedBy)})" if task.blockedBy else ""
        return f"Updated {task.id}: {task.subject}{deps}"

    def run_delete_task(self, task_id: str) -> str:
        """工具入口:删除任务（连带剥离别处对它的依赖引用）。"""
        try:
            task, released = self._delete_task(task_id)
        except ValueError as e:
            return f"Error: {e}"
        msg = f"Deleted {task.id} ({task.subject})"
        if released:
            msg += f"\n已从这些任务的 blockedBy 中移除: {', '.join(released)}"
        return msg
