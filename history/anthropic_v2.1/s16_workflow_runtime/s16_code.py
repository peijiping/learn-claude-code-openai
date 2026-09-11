#!/usr/bin/env python3
"""
s16: Workflow Runtime - run a saved orchestration through one tool call.

Run:
  python s16_workflow_runtime/code.py
  python s16_workflow_runtime/code.py demo
  python s16_workflow_runtime/code.py resume

    +-------------+       +--------------------------------+
    | Agent loop  | ----> | Workflow(name, args, run_id)  |
    +-------------+       +---------------+----------------+
                                          |
                           +--------------+--------------+
                           | agent | parallel | pipeline  |
                           +--------------+--------------+
                                          |
                                   journal + result

【中文导读】
s16 = 工作流运行时（Workflow Runtime）。

核心思想：把"多智能体编排计划"写成确定性 Python 代码（而不是让模型在对话
中一步步决策），模型只需通过一次 Workflow 工具调用，就能执行整段已保存的
编排脚本。计划即代码（The plan is code, not a chat turn）。

三种编排原语（由 ExecutionState 提供给工作流脚本）：
  - agent(prompt, schema) : 派生一个子智能体执行单步任务；传 schema 时强制
                            结构化 JSON 输出并校验（失败自动重试一次）
  - parallel(thunks)      : 屏障式并发（BARRIER）——所有任务全部完成才继续，
                            任一任务抛异常则整个工作流失败
  - pipeline(items, ...)  : 按条目流水线——条目 A 可以先推进到第 3 阶段，
                            而条目 B 还停留在第 1 阶段（阶段之间没有屏障）

可靠性支撑设施：
  - journal  (.runtime/<runId>.journal.jsonl) : 追加式执行日志；resume 时按
    "语义键"（kind+label+prompt+schema 的稳定哈希）命中缓存，直接重放结果，
    跳过已执行过的 agent() 调用
  - snapshot (.runtime/<runId>.json)          : 启动参数与任务状态快照，
    resume 时校验工作流名与入参是否与原始运行一致
  - run lock : 双层互斥——线程级 threading.Lock 注册表 + 进程级 fcntl 文件锁，
    保证同一 runId 不会被并发重复执行
  - Budget / AGENT_CAP / CONCURRENCY : Token 预算、agent() 调用次数上限、
    并发信号量，防止工作流失控

运行方式：
  python s16_workflow_runtime/code.py          # 加载 s15 宿主并注入 Workflow 工具，进入 REPL
  python s16_workflow_runtime/code.py demo     # 用 MockAgentRunner 确定性地跑一遍示例工作流
  python s16_workflow_runtime/code.py resume   # 依据 journal 缓存续跑上一次 demo（未变化的步骤直接命中缓存）
"""

import asyncio          # 异步编排：Semaphore（并发上限）、gather（并发聚合）、to_thread（把同步 LLM 调用丢进线程池）
import fcntl            # 文件锁（POSIX），实现跨进程的 runId 互斥
import hashlib          # 提供跨进程稳定的哈希（Python 内置 hash() 每次进程启动加盐，不能用于 resume）
import importlib.util   # 按文件路径动态加载 s15 宿主模块
import json
import os               # O_CREAT|O_EXCL 原子占位 runId 文件；os.replace 原子写快照
import re               # 工作流名 / runId 的白名单正则校验
import secrets          # 加密安全的随机数，生成 runId 后缀
import sys
import threading        # 线程级的 run 锁注册表
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# -- Runtime Guards（运行时护栏）--
AGENT_CAP = 1000                       # 单次运行内 agent() 调用次数的硬上限（防止工作流无限派生子智能体）
CONCURRENCY = 8                        # 并发上限：信号量控制同时真实运行的子智能体数量
STORE = Path(__file__).parent / ".runtime"   # 快照 + 日志的持久化目录（相对本文件，便于 demo/resume）
MISS = object()                        # journal 缓存未命中的哨兵对象——不能用 None，因为缓存值本身可能是 null
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")          # 工作流名：1-64 位 slug（字母/数字/._-）
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$") # runId 格式：wf_<工作流名>_<16位hex>


def _stable_hash(s: str) -> int:
    """Process-stable hash (Python's hash() is salted per process, which would
    break resume keys across `run` and `resume`).
    【中文】跨进程稳定哈希。Python 内置 hash() 对字符串每次进程启动随机加盐，
    同一输入在不同进程会得到不同值，会让 journal 的 resume 缓存键对不上；
    这里改用 SHA-256 保证 run 与 resume 两个进程算出同一个键。"""
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def create_run_id(meta) -> str:
    # 生成新的运行标识：wf_<工作流名>_<16位随机hex>
    return f"wf_{meta['name']}_{secrets.token_hex(8)}"


def reserve_run_id(meta) -> str:
    """Reserve a fresh run identity before any journal can be truncated.
    【中文】在任何 journal 被截断之前，先"占坑"一个全新的 runId。
    实现方式：用 os.O_CREAT | os.O_EXCL 以原子方式创建 <runId>.json 空文件——
    若文件已存在（极小概率的随机碰撞）则换一个再试，最多 32 次。
    这样即使后续流程崩溃，runId 也不会与其他运行冲突。"""
    STORE.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        run_id = validate_run_id(create_run_id(meta))
        snapshot_path = STORE / f"{run_id}.json"
        try:
            # O_EXCL 保证"创建"是原子的：文件已存在则抛 FileExistsError
            fd = os.open(snapshot_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue   # 随机碰撞，换一个 runId 重试
        os.close(fd)
        return run_id
    raise WorkflowInputError("could not allocate a unique workflow runId")


def create_task_id(run_id) -> str:
    # 由 runId 派生任务 ID（宿主任务系统里的 task_id）
    return f"local_workflow_{run_id}"


def validate_run_id(run_id):
    # 校验 runId 格式（resume 入口尤其重要——它来自外部输入）
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("invalid workflow runId")
    return run_id


# -- Errors（统一错误类型）--
class WorkflowInputError(Exception):
    """Bad workflow, metadata, or schema input.
    【中文】工作流/元数据/Schema 输入不合法时抛出的统一异常。
    在工具层会被捕获并转成 "Error: ..." 文本回给模型。"""


_run_locks_guard = threading.Lock()                # 保护下面这个字典本身的锁
_run_locks: dict[str, threading.Lock] = {}         # runId -> 线程锁 的注册表（同进程内查重）


@contextmanager
def workflow_run_lock(run_id: str):
    """Hold one run across threads and host processes for its full lifecycle.
    【中文】双重锁：让同一个 runId 在"本进程内多个线程"和"跨多个宿主进程"
    两个维度上都互斥，锁持有覆盖整个运行生命周期。

    1) 线程锁：从注册表按 runId 取（或建）一把 threading.Lock，尝试非阻塞
       获取；拿不到说明同进程里已有相同 runId 在跑，直接报错。
    2) 文件锁：对 <runId>.lock 文件做 fcntl.flock（LOCK_NB 非阻塞），实现
       跨进程互斥（不同进程跑同一 runId 的 resume 也会被挡住）。
    finally 中按相反顺序释放两把锁，并顺手从注册表清理本线程的锁条目。"""
    with _run_locks_guard:
        local_lock = _run_locks.setdefault(run_id, threading.Lock())
    if not local_lock.acquire(blocking=False):
        # 非阻塞获取失败 => 同进程内该 runId 已在运行
        raise WorkflowInputError(f"workflow run {run_id} is already active")

    handle = None
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        handle = (STORE / f"{run_id}.lock").open("a+", encoding="utf-8")
        try:
            # LOCK_EX 独占锁 + LOCK_NB 非阻塞：被占用立刻抛 BlockingIOError
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowInputError(
                f"workflow run {run_id} is already active"
            ) from exc
        yield   # ---- 临界区：调用方在 with 体内执行整个工作流 ----
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)   # 释放文件锁
            finally:
                handle.close()
        local_lock.release()   # 释放线程锁
        with _run_locks_guard:
            # 若没有其他线程正在等这把锁，把它从注册表里移除，避免字典无限增长
            if not local_lock.locked() and _run_locks.get(run_id) is local_lock:
                _run_locks.pop(run_id, None)


# -- Metadata Validation（工作流元数据校验）--
def validate_meta(meta):
    """Validate name, description, and optional phases before launch.
    【中文】启动前校验工作流元数据：name / description 必填，
    可选的 phases 必须是非空字符串列表（用于进度展示的阶段划分）。"""
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta must be an object literal")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta requires `name` and `description`")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError(
            "meta.name must be a 1-64 character slug using letters, numbers, '.', '_', or '-'"
        )
    if not isinstance(meta["description"], str):
        raise WorkflowInputError("meta.description must be a string")
    if "phases" in meta:
        if not isinstance(meta["phases"], list) or not all(
            isinstance(phase, str) and phase for phase in meta["phases"]
        ):
            raise WorkflowInputError("meta.phases must be a list of non-empty strings")
    return meta


def check_permission(meta, settings=None):
    """Apply the s03 allow/deny gate before launching a workflow.
    【中文】启动前套用 s03 课的 allow/deny 权限门控：
    名字在 settings.deny 黑名单里直接拒绝；否则视为放行。
    （本演示只有黑名单语义，未命中 deny 即 allow。）"""
    settings = settings or {}
    if meta["name"] in settings.get("deny", []):
        raise WorkflowInputError(f"workflow '{meta['name']}' denied by settings")
    return "allow"


# -- Minimal JSON Schema（极简 JSON Schema 校验器）--
class SimpleJsonSchema:
    """Tiny validator backing agent({schema}):
    object/array/string/boolean/number + required keys.
    【中文】为 agent({schema}) 提供的迷你校验器，只覆盖教程用到的子集：
    object / array / string / boolean / number(integer) + required + enum。
    返回 (是否通过, 错误信息) 而不是抛异常，方便上层决定重试。"""

    def __init__(self, schema):
        self.schema = schema

    def validate(self, value, schema=None):
        schema = self.schema if schema is None else schema
        # enum：值必须在枚举列表内
        if "enum" in schema and value not in schema["enum"]:
            return False, f"expected one of {schema['enum']}"
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False, "expected object"
            # required：必填键缺一不可
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"missing required key '{key}'"
            # properties：对出现过的键递归校验子 Schema
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, err = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {err}"
            return True, None
        if t == "array":
            if not isinstance(value, list):
                return False, "expected array"
            items = schema.get("items")
            if items:
                # 对每个元素递归校验，错误信息带上下标方便定位
                for i, el in enumerate(value):
                    ok, err = self.validate(el, items)
                    if not ok:
                        return False, f"[{i}]: {err}"
            return True, None
        if t == "string":
            return (isinstance(value, str), None if isinstance(value, str) else "expected string")
        if t == "boolean":
            # 注意 bool 是 int 的子类，必须先于 number 判断
            return (isinstance(value, bool), None if isinstance(value, bool) else "expected boolean")
        if t in ("number", "integer"):
            # 数字：int/float 都接受，但排除 bool（True 是 int 的实例）
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return (ok, None if ok else "expected number")
        return True, None   # 未知类型不拦（宽松处理）


def _fill_schema(schema, seed):
    """Deterministic generic filler used for schemas the mock doesn't special-case.
    【中文】确定性的通用 Schema 填充器：MockAgentRunner 遇到没有特判的
    Schema 时，按结构递归生成一份"看起来合理"的假数据。
    所有取值都由 _stable_hash(seed) 决定，因此同样输入永远得到同样输出。"""
    t = schema.get("type")
    if t == "object":
        # 优先填 required 键；没写 required 就填全部 properties
        keys = schema.get("required") or list(schema.get("properties", {}))
        return {k: _fill_schema(schema["properties"][k], f"{seed}/{k}") for k in keys}
    if t == "array":
        # 只填一个示例元素（seed 带下标 0）
        return [_fill_schema(schema["items"], f"{seed}/0")]
    if t == "boolean":
        # 哈希 % 4 != 0 => 约 3/4 概率为 True
        return _stable_hash(seed) % 4 != 0
    if t in ("number", "integer"):
        # 0~4 之间的确定性数字
        return _stable_hash(seed) % 5
    # 字符串：取 seed 的最后一段作为占位内容
    return seed.rsplit("/", 1)[-1]


# -- Agent Runners（子智能体执行器：Mock 与真实 API 两个实现）--


@dataclass(frozen=True)
class RunnerOutput:
    # 执行器统一返回值：value=结果（文本或已解析的 JSON），tokens=本次消耗的 token 数
    value: object
    tokens: int


class MockAgentRunner:
    """Deterministic runner used by demo mode and unit tests.
    【中文】确定性 Mock 执行器：demo 模式和单元测试使用，不调 API、零成本、
    输出完全由输入哈希决定——同一 prompt 永远得到同一结果，
    这正是 journal 缓存重放（resume）能被验证的前提。"""

    def run(self, prompt, schema=None, label=None):
        if schema is None:
            # 无 schema：直接返回一段确定性文本（截取前 60 字符避免刷屏）
            value = f"[mock] {(label or prompt)[:60]}"
            return RunnerOutput(value, self._tokens(prompt, value))
        props = schema.get("properties", {})
        if "findings" in props:
            # 特判"审查发现"结构：产出 1~2 条 finding，severity 由哈希决定
            n = 1 + (_stable_hash(prompt) % 2)
            sev = ["high", "medium", "low"]
            value = {"findings": [
                {"title": f"{label or 'audit'} #{i + 1}",
                 "severity": sev[_stable_hash(prompt + str(i)) % 3]}
                for i in range(n)
            ]}
        elif "isReal" in props:
            # 特判"裁决"结构：约 1/4 概率判定为误报（不真实）
            real = _stable_hash(prompt) % 4 != 0
            value = {"isReal": real,
                     "reason": "reproduced" if real else "could not reproduce"}
        else:
            # 其他 Schema：走通用填充器
            value = _fill_schema(schema, prompt)
        return RunnerOutput(value, self._tokens(prompt, value))

    @staticmethod
    def _tokens(prompt, result):
        # 估算 token：约等于字符数 / 4（英文经验值），输入 + 输出各算一份
        return len(prompt) // 4 + len(json.dumps(result, default=str)) // 4


def _response_text(response) -> str:
    # 从 Anthropic API 响应中拼接所有 text block 的文本
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_runner_json(text: str) -> object:
    # 解析模型返回的 JSON：兼容 ```json 代码围栏；围栏剥掉后仍失败，
    # 则扫描文本里第一个能成功 raw_decode 的 '{'，从中截取 JSON 对象。
    # 全部失败 => 抛 WorkflowInputError，由上层触发一次重试。
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines                 # 去掉 ``` 开头行
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]                                # 去掉 ``` 结尾行
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # 兜底：逐字符扫描第一个 '{'，尝试从该位置解码一个完整 JSON 对象
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        raise WorkflowInputError("workflow agent returned invalid JSON")


class AnthropicAgentRunner:
    """Run workflow agents through the same API client as the host.
    【中文】真实执行器：复用宿主（s15）的 Anthropic 客户端与模型配置来跑
    工作流子智能体。有 schema 时把 schema 序列化进 prompt，要求模型只回一个
    符合格式的 JSON 对象（Structured Output 的"穷人版"实现）。"""

    def __init__(self, client, model):
        self.client = client
        self.model = model

    def run(self, prompt, schema=None, label=None):
        request = prompt
        if schema is not None:
            # 把 schema 以确定性顺序（sort_keys）拼进请求，方便模型对照
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        response = self.client.messages.create(
            model=self.model,
            system=(
                # 系统提示：专注完成单步，禁止谎称访问了 prompt 之外的文件/结果
                "You are a focused workflow agent. Complete only the supplied "
                "step. Do not claim access to files or results not included in "
                "the prompt."
            ),
            messages=[{"role": "user", "content": request}],
            max_tokens=2000,
        )
        text = _response_text(response)
        if schema is None:
            value = text   # 无 schema：原始文本就是结果
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # 解析失败时先把原始文本当结果塞回去，
                # 让 ExecutionState 的 schema 校验触发它那一次重试。
                value = text
        # 统计输入 + 输出 token（usage 缺失时按 0 算）
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        return RunnerOutput(value, tokens)


RUNNER_FACTORY = MockAgentRunner   # 执行器工厂：默认 Mock；接入 s15 宿主后被替换为真实 API 执行器


# -- Journal（执行日志：resume 缓存重放的基石）--
class WorkflowJournal:
    """Append-only <runId>.journal.jsonl. On resume, agent() calls whose
    semantic key is already present are replayed from cache instead of re-run.
    【中文】追加式日志文件 <runId>.journal.jsonl，每行一条 {"key": ..., "value": ...}。
    key 是 agent() 调用的"语义键"（kind+label+prompt+schema 的稳定哈希）——
    与并发顺序无关，因此 parallel/pipeline 里同一调用在 resume 时能算出同一个键。
    resume 打开旧日志时把全部记录装进内存 cache；之后 agent() 命中缓存就直接
    重放结果，不再真实调用模型。新运行则以 "w" 模式打开（截断旧文件）。"""

    def __init__(self, run_id, resume, store=None):
        store = STORE if store is None else store
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self.resume = resume
        self.cache = {}
        if resume:
            # resume 模式：日志文件必须存在，且每行必须是合法的 key/value 记录
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    rec = json.loads(line)
                    if (
                        not isinstance(rec, dict)
                        or not isinstance(rec.get("key"), str)
                        or "value" not in rec
                    ):
                        raise ValueError("expected key/value record")
                except (json.JSONDecodeError, ValueError) as exc:
                    # 坏行精确报行号，避免静默丢失部分缓存
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    ) from exc
                self.cache[rec["key"]] = rec["value"]
            self._f = self.path.open("a", encoding="utf-8")   # 追加模式续写
        else:
            self._f = self.path.open("w", encoding="utf-8")             # fresh run truncates（全新运行：截断旧文件）

    def key(self, kind, label, prompt, schema):
        # 确定性语义键：不依赖并发完成顺序，因此 parallel/pipeline 的调用
        # 在 resume 时也能得到相同的键（这是缓存能命中的关键）。
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        # 查缓存：命中返回值；未命中返回 MISS 哨兵（缓存值可能是 None，不能用 None 判断）
        return self.cache.get(key, MISS)

    def record(self, key, value):
        # 落一条记录并立即 flush（崩溃后最多丢最后一条，不影响已完成步骤）
        self._f.write(json.dumps({"key": key, "value": value}) + "\n")
        self._f.flush()
        self.cache[key] = value

    def close(self):
        self._f.close()


# -- Token Budget（Token 预算）--
class Budget:
    """budget.total / spent() / remaining(). Once spent reaches total, agent()
    calls raise instead of silently overspending.
    【中文】Token 预算：total=None 表示不设限。已消耗达到上限后，
    agent() 会直接抛异常而不是悄悄超支。"""

    def __init__(self, total=None):
        self.total = total
        self._spent = 0

    def add(self, n):
        # 预扣校验：加上本次消耗若会超过上限，直接拒绝（不部分记账）
        if self.total is not None and self._spent + n > self.total:
            raise WorkflowInputError(
                f"token budget exceeded ({self._spent + n} > {self.total})"
            )
        self._spent += n

    def spent(self):
        return self._spent

    def remaining(self):
        # 未设上限时返回无穷大
        return float("inf") if self.total is None else max(0, self.total - self._spent)


# -- Workflow Task Lifecycle（工作流任务生命周期）--
class LocalWorkflowTask:
    """Hold workflow status, usage, and progress events.
    【中文】本地工作流任务的运行时载体：保存状态机（running/completed/failed）、
    用量统计（agents/tokens）与进度事件流，并把事件实时打印到终端。"""

    def __init__(self, task_id, run_id, meta):
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.status = "running"                        # 状态机：running -> completed | failed
        self.usage = {"agents": 0, "tokens": 0}        # 用量：子智能体调用次数 / token 消耗
        self.progress = []                             # 进度事件列表（可序列化回传给宿主）

    def event(self, name, **data):
        # 生命周期事件（async_launched / task_started / task_notification）
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  event      {name:<18} {line}")

    def progress_event(self, ptype, **data):
        # 进度事件（workflow_phase / workflow_log / workflow_agent），同时记内存 + 打印
        self.progress.append({"type": ptype, **data})
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  progress   {ptype:<16} {line}")


# -- Workflow Primitives（工作流原语：注入给工作流脚本的执行环境）--
class ExecutionLimits:
    """Shared run-wide limits, including nested workflows.
    【中文】整个运行共享的限额（嵌套子工作流也共用同一份）：
    agents 计数器用于 AGENT_CAP 总量限制；semaphore 用于并发上限。"""

    def __init__(self):
        self.agents = 0                                # 已派生的 agent() 总数（含嵌套工作流）
        self.semaphore = asyncio.Semaphore(CONCURRENCY)  # 并发信号量，限制同时在跑的子智能体数

    def claim_agent(self):
        # 领取一个"派生名额"：超上限立即失败，防止工作流自我爆炸
        self.agents += 1
        if self.agents > AGENT_CAP:
            raise WorkflowInputError(f"agent() cap reached ({AGENT_CAP})")


class ExecutionState:
    """Injected into the workflow script with the orchestration primitives.
    【中文】执行状态 / 编排上下文：作为 ctx 注入工作流脚本，是工作流代码里
    唯一能接触到的"能力面"——ctx.agent / ctx.parallel / ctx.pipeline /
    ctx.workflow / ctx.phase / ctx.log。所有可靠性机制（缓存、预算、限额、
    结构化校验）都封装在这里，工作流作者无需关心。"""

    def __init__(self, task, journal, runner, budget, args, depth=0, limits=None):
        self.task = task          # LocalWorkflowTask：状态/用量/进度
        self.journal = journal    # WorkflowJournal：缓存与落盘
        self.runner = runner      # MockAgentRunner 或 AnthropicAgentRunner
        self.budget = budget      # Budget：token 预算
        self.args = args          # 工作流入参
        self._depth = depth       # 嵌套深度：workflow() 只允许一层子工作流
        self._phase = None        # 当前阶段名（新 agent() 默认归属该阶段）
        self._phases_seen = set() # 已广播过的阶段名（用于 upsert 去重）
        self._limits = limits or ExecutionLimits()  # 与父级共享的运行限额

    def phase(self, title):
        """Start a phase; subsequent agent()s group under it. Upsert: emitting the
        same phase again (e.g. from each pipeline item) does not re-announce it.
        【中文】声明/切换当前阶段：之后的 agent() 默认归入该阶段。
        Upsert 语义：同一阶段名重复声明（例如 pipeline 每个条目都调一次
        ctx.phase("Verify")）只会在第一次广播 workflow_phase 事件，不重复刷屏。"""
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """Emit a workflow_log progress line.
        【中文】输出一条工作流日志进度事件。"""
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        """Spawn one subagent. With a schema, force StructuredOutput + validate
        (retry once). On resume, a cached key short-circuits the run.
        【中文】派生一个子智能体执行单步任务。核心流程：
        1) 计数与预算护栏（claim_agent / 预算余量检查）
        2) 算语义键查 journal 缓存——resume 时命中直接重放（cached 状态）
        3) 未命中：在并发信号量保护下把同步 runner 丢进线程池执行
        4) 有 schema 时做结构化校验，失败则带着 "Return valid JSON." 重试一次，
           再失败才报错（Structured Output 的强制闭环）
        5) 记账（预算/用量）+ journal 落盘 + 进度事件（done 状态）"""
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        if self.budget.remaining() <= 0:
            raise WorkflowInputError("token budget exceeded")

        key = self.journal.key("agent", label, prompt, schema)
        cached = self.journal.cached(key)
        if cached is not MISS:
            # ---- 缓存命中（resume 路径）：校验后直接重放，不再调用模型 ----
            if schema is not None:
                ok, err = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(
                        f"cached agent output failed schema validation: {err}"
                    )
            self.task.progress_event("workflow_agent", label=label,
                                     phase=phase or self._phase, status="cached")
            return cached

        # ---- 缓存未命中：真实执行 ----
        async with self._limits.semaphore:
            # runner.run 是同步阻塞调用（requests 层面），丢进线程池避免卡事件循环
            run = await asyncio.to_thread(
                self.runner.run, prompt, schema, label
            )
            result = run.value
            tokens = run.tokens

        if schema is not None:
            # 结构化输出校验：失败自动重试一次（提示词追加 "Return valid JSON."）
            ok, err = SimpleJsonSchema(schema).validate(result)
            if not ok:
                retry = await asyncio.to_thread(
                    self.runner.run,
                    prompt + "\n\nReturn valid JSON.",
                    schema,
                    label,
                )
                result = retry.value
                tokens += retry.tokens
                ok, err = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(f"agent({{schema}}) invalid output: {err}")

        self.budget.add(tokens)                        # 记入 token 预算
        self.task.usage["agents"] += 1                 # 用量统计
        self.task.usage["tokens"] += tokens
        self.journal.record(key, result)               # 落 journal：resume 时可重放
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    async def parallel(self, thunks):
        """BARRIER: run all thunks concurrently and fail if any thunk fails.
        【中文】屏障式并发：所有 thunk（异步闭包）同时开跑并全部等待完成。
        任一 thunk 抛异常 => gather 立即把异常抛出 => 整个工作流失败（fail-fast）。"""
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """Per-item staged flow, NO barrier between stages: item A can be in
        stage 3 while item B is still in stage 1. Each stage gets
        (prev_result, original_item, index). A throwing stage fails the workflow.
        【中文】按条目流水线：每个条目独立穿过全部阶段，阶段之间【没有屏障】——
        条目 A 可以已经走到第 3 阶段，而条目 B 还停在第 1 阶段（相比
        parallel 的全局屏障，流水线能更早让慢条目开工、快条目收尾）。
        每个阶段函数的签名是 (上一阶段结果, 原始条目, 条目下标)；
        任一阶段抛异常 => gather 抛出 => 整个工作流失败。"""
        async def run_item(item, idx):
            value = item
            for stage in stages:
                value = await stage(value, item, idx)   # 逐阶段串联，结果传给下一阶段
            return value
        # 所有条目并发执行各自的阶段链
        return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])

    async def workflow(self, name, args=None):
        """Run a saved workflow inline as a child (one level), sharing this run's
        journal + budget + agent counter.
        【中文】把另一个已保存的工作流作为子工作流内联运行：
        - 只允许一层嵌套（子工作流里不能再 workflow()），防止递归失控
        - 子工作流与父级共享同一份 journal（缓存互通）、budget（预算共用）、
          agent 计数器（AGENT_CAP 共享），表现为"同一次运行"的一部分"""
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in WORKFLOWS:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        meta, fn = WORKFLOWS[name]
        child = ExecutionState(self.task, self.journal, self.runner, self.budget,
                               args or {}, depth=self._depth + 1,
                               limits=self._limits)
        return await fn(child, args or {})


# -- Workflow Tool（Workflow 工具：模型调用工作流的入口）--
class WorkflowTool:
    """The Workflow tool. .call() validates meta, runs the permission check,
    creates runId/taskId, registers a LocalWorkflowTask, and emits lifecycle
    events while executing the script. It returns the result and task state and
    supports resume.
    【中文】Workflow 工具本体。call() 的完整编排：
    1) 校验 meta + 权限门控
    2) 确定运行标识：resume 模式沿用外部传入的 runId（校验格式）；
       新运行用 reserve_run_id 原子占坑
    3) 拿 run 级双重锁（线程锁 + 文件锁）后进入 _call_locked 执行
    4) resume 时读快照校验（工作流名一致、入参一致）并加载 journal 缓存
    5) 记录启动信封、写快照 -> 执行脚本 -> 无论成败都关闭 journal、
       写输出文件/最终快照、记 last_run、发 task_notification 事件"""

    async def call(self, meta, script_fn, args=None, resume_from_run_id=None):
        validate_meta(meta)
        check_permission(meta)
        resuming = resume_from_run_id is not None
        if resuming:
            # resume：沿用旧 runId（先做格式校验）
            run_id = validate_run_id(resume_from_run_id)
        else:
            # 新运行：先原子占坑一个唯一 runId，再开始执行
            run_id = reserve_run_id(meta)
        with workflow_run_lock(run_id):
            # 全程持有 run 级锁：同 runId 在任何线程/进程都不允许并发执行
            return await self._call_locked(
                meta, script_fn, args, run_id, resuming
            )

    async def _call_locked(self, meta, script_fn, args, run_id, resuming):
        if resuming:
            # ---- resume 路径：读快照做一致性校验 ----
            snapshot = _read_snapshot(run_id)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("resume runId does not match workflow meta")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args              # 未传参 => 沿用原始入参
            elif args != saved_args:
                raise WorkflowInputError("resume args do not match the original run")
            journal = WorkflowJournal(run_id, resume=True)   # 加载历史缓存
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False)  # 新日志（截断）
        task_id = create_task_id(run_id)

        task = LocalWorkflowTask(task_id, run_id, meta)
        # Record the launch envelope before workflow execution starts.
        # 记录"启动信封"（返回给调用方的第一段信息），然后广播生命周期事件
        launched = {"status": "async_launched", "taskId": task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task_id)
        task.event("task_started", workflow=meta["name"],
                   phases=",".join(meta.get("phases", [])) or "-",
                   resume=resuming)
        # 写初始快照（runId / 工作流名 / 入参 / 任务状态），供 resume 校验
        _write_json(STORE / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })

        try:
            # 组装执行上下文：Budget 从 args["budget"] 读取（None = 不限）
            ctx = ExecutionState(
                task, journal, RUNNER_FACTORY(), Budget(args.get("budget")), args
            )
            result = await script_fn(ctx, args)   # 真正执行工作流脚本
            task.status = "completed"
        except Exception as e:                          # failed / stopped close the loop too
            # 任何异常（含预算超限/校验失败/用户停止）都把状态收口为 failed，
            # 错误对象作为 result 返回——已完成的步骤仍留在 journal 里可 resume
            task.status = "failed"
            result = {"error": str(e)}
        finally:
            journal.close()

        # 无论成败：落输出文件 + 刷新最终快照 + 记录 last_run（供 resume 命令取用）
        _write_json(STORE / f"{run_id}.output.json", result)
        _write_json(STORE / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })
        _save_last_run(run_id)
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f".runtime/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": task}


def _write_json(path, value):
    # 原子写 JSON：先写 .tmp 临时文件，再用 os.replace 原子替换，
    # 避免读方（resume/快照校验）读到写了一半的残缺文件
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _read_snapshot(run_id):
    # 读取运行快照（resume 的一致性校验依据），文件缺失/损坏/非对象都报可读错误
    path = STORE / f"{run_id}.json"
    if not path.exists():
        raise WorkflowInputError(f"resume snapshot not found for {run_id}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}") from exc
    if not isinstance(snapshot, dict):
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}")
    return snapshot


def _save_last_run(run_id):
    # 记录最近一次 runId，demo 的 resume 子命令从这里读取
    (STORE / "last_run.txt").write_text(run_id, encoding="utf-8")


def _read_last_run():
    p = STORE / "last_run.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


# -- Sample Workflow（示例工作流：代码审查多维度流水线）--
# 审查发现 Schema：{findings: [{title, severity(high|medium|low)}]}
FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "severity"],
        "properties": {
            "title": {"type": "string"},
            "severity": {
                "type": "string", "enum": ["high", "medium", "low"]
            },
        }}}},
}
# 裁决 Schema：{isReal: bool, reason: str}——对抗式验证该发现是否真实成立
VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}

SAMPLE_META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": ["Review", "Verify"],   # 两个阶段：审查 -> 逐条验证
}

DIMENSIONS = ["correctness", "security", "performance", "style"]   # 四个审查维度
DEMO_CHANGES = (
    # demo 用的"代码变更"样例：一个肉眼可见的 SQL 注入
    "def load_user(user_id):\n"
    "    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n"
    "    return db.execute(query).fetchone()\n"
)


async def sample_workflow(ctx, args):
    """pipeline over review dimensions (audit -> verify-each), then keep only the
    findings a verifier confirms. The plan is code, not a chat turn.
    【中文】示例工作流：对每个审查维度跑"审查 -> 逐条对抗验证"的流水线，
    只保留验证器确认真实的发现。编排计划是 Python 代码，不是对话轮次——
    模型只负责调用一次 Workflow 工具，结构完全由这段代码决定。

    流程：
      Review 阶段：4 个维度并发 audit（pipeline 按条目流转，无全局屏障）
      Verify 阶段：每个维度产出的每条 finding 各派一个对抗验证子智能体（parallel）
      收尾：汇总确认的发现，按 high > medium > low 排序"""
    ctx.phase("Review")
    changes = args.get("changes", "")
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes must be a string")
    review_input = changes.strip() or "No change context was supplied."

    async def audit(_value, dimension, _idx):
        # 阶段 1（流水线签名：上一阶段结果, 原始条目, 下标）：审查单个维度
        out = await ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _idx):
        # 阶段 2：对阶段 1 的每条 finding 做对抗式验证
        ctx.phase("Verify")
        # Each finding is verified by its own adversarial subagent, concurrently.
        # 每条 finding 一个独立验证子智能体，全部并发执行（parallel 屏障）。
        # 注意 lambda f=f 的默认参数技巧：立即绑定循环变量 f，避免闭包晚绑定
        # 导致所有 lambda 都引用最后一条 finding 的经典 Python 坑。
        verdicts = await ctx.parallel([
            (lambda f=f: ctx.agent(
                f"Adversarially verify this {dimension} finding against the "
                "supplied change context.\n\n"
                f"Change context:\n{review_input}\n\n"
                f"Finding:\n{json.dumps(f, ensure_ascii=True)}",
                schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}", phase="Verify"))
            for f in audited["findings"]])
        # 只保留验证器判定为真实的发现
        confirmed = [f for f, v in zip(audited["findings"], verdicts)
                     if v and v.get("isReal")]
        return {"dimension": dimension, "confirmed": confirmed}

    # 条目 = 4 个维度，阶段链 = [audit, verify]：维度 A 可能在 verify 时，
    # 维度 B 还在 audit——这正是 pipeline 与 parallel 的差别
    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    # 汇总所有维度确认的发现，拍平成 [{dimension, title, severity}, ...]
    confirmed = [{"dimension": r["dimension"], **f}
                 for r in results if r for f in r["confirmed"]]
    # 按严重度排序：high(0) < medium(1) < low(2)，未知严重度垫底(3)
    confirmed.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))
    ctx.log(f"confirmed {len(confirmed)} real finding(s)")
    return {"confirmed": confirmed}


# Saved workflow registry（已保存工作流注册表：名字 -> (元数据, 脚本函数)）
WORKFLOWS = {SAMPLE_META["name"]: (SAMPLE_META, sample_workflow)}

# 暴露给模型的工具定义（模型侧只看到 name/args/resume_from_run_id 三个参数）
WORKFLOW_TOOL = {
    "name": "Workflow",
    "description": "Run a saved workflow by name. Pass input in args.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "object"},
            "resume_from_run_id": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def serialize_task(task):
    # 把 LocalWorkflowTask 序列化为可 JSON 化的任务快照（写进 .runtime/<runId>.json）
    return {
        "taskId": task.task_id,
        "taskType": "local_workflow",
        "runId": task.run_id,
        "workflowName": task.meta["name"],
        "status": task.status,
        "usage": dict(task.usage),
        "progress": list(task.progress),
    }


async def run_workflow(name, args=None, resume_from_run_id=None):
    """Model-facing adapter: resolve trusted code from the host registry.
    【中文】面向模型的适配层：模型只给工作流"名字"，实际执行的代码由宿主
    注册表（WORKFLOWS）解析——模型永远不能直接提交可执行代码，这是
    "代码由人保存、模型只按名调用"的安全边界。"""
    if not isinstance(name, str):
        raise WorkflowInputError("workflow name must be a string")
    if name not in WORKFLOWS:
        raise WorkflowInputError(f"unknown workflow '{name}'")
    if args is not None and not isinstance(args, dict):
        raise WorkflowInputError("workflow args must be an object")
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool().call(
        meta,
        script_fn,
        args=args,
        resume_from_run_id=resume_from_run_id,
    )
    return {
        "launched": out["launched"],
        "result": out["result"],
        "task": serialize_task(out["task"]),
    }


WORKFLOW_HANDLERS = {"Workflow": run_workflow}   # 工具名 -> 异步处理器（demo 直接用）
INHERITS_TOOLS_FROM = "s15"                      # 标注：本课工具池继承自 s15


def run_workflow_sync(**tool_input):
    """Bridge the synchronous host dispatcher to the async workflow runtime.
    【中文】同步桥：s15 宿主的工具分发是同步的，这里用 asyncio.run 把
    异步工作流运行时桥接过去。业务错误（WorkflowInputError）转成
    "Error: ..." 文本回给模型，而不是让异常炸掉宿主循环。"""
    try:
        return json.dumps(asyncio.run(run_workflow(**tool_input)), default=str)
    except WorkflowInputError as exc:
        return f"Error: {exc}"


def install_workflow_tool(host):
    """Extend the s15 host tool pool without changing its dispatch loop.
    【中文】把 Workflow 工具"外挂"进 s15 宿主，不改动宿主的分发循环：
    1) 替换 RUNNER_FACTORY：工作流子智能体改用真实 API（复用宿主的
       client 和 MODEL），而非 Mock
    2) 猴子补丁 host.assemble_tool_pool：在原工具池基础上追加 Workflow
       工具定义与同步处理器（幂等——用 _workflow_tool_installed 标记防重复安装）"""
    global RUNNER_FACTORY
    RUNNER_FACTORY = lambda: AnthropicAgentRunner(host.client, host.MODEL)
    if getattr(host, "_workflow_tool_installed", False):
        return
    base_assemble = host.assemble_tool_pool

    def assemble_with_workflow():
        tools, handlers = base_assemble()
        if not any(tool.get("name") == "Workflow" for tool in tools):
            tools.append(WORKFLOW_TOOL)
        handlers["Workflow"] = run_workflow_sync
        return tools, handlers

    host.assemble_tool_pool = assemble_with_workflow
    host._workflow_tool_installed = True


def load_integrated_host():
    """Load s15 lazily so deterministic workflow tests need no API key.
    【中文】按文件路径懒加载 s15 集成宿主模块（不用包导入，避免循环依赖）。
    只有 REPL 模式才加载它；demo/resume 用 Mock 执行器，无需 API key。"""
    path = Path(__file__).resolve().parents[1] / "s15_integrated_harness" / "code.py"
    spec = importlib.util.spec_from_file_location("integrated_host", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load integrated host from {path}")
    host = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = host   # 注册进 sys.modules，供宿主内部 import 自查
    spec.loader.exec_module(host)
    return host


# -- CLI --
async def run_demo(argv):
    """【中文】demo 入口：确定性跑一遍示例工作流。
    - `demo`   ：全新运行（Mock 执行器，无 API 依赖）
    - `resume` ：读取 last_run.txt 找到上次 runId 续跑；
      prompt/label/schema 未变化的 agent() 调用直接命中 journal 缓存
      （进度事件显示 status=cached），只有新增/变更的步骤会真实执行。"""
    resume_id = None
    if argv and argv[0] == "resume":
        resume_id = _read_last_run()
        if not resume_id:
            print("nothing to resume; run `python code.py demo` first.")
            return
        print(f"resuming {resume_id}; unchanged agent() calls use the journal cache\n")
    else:
        print("launching workflow `review-changes`\n")

    out = await WORKFLOW_HANDLERS["Workflow"](
        name="review-changes",
        args={"budget": None, "changes": DEMO_CHANGES},   # budget=None：不设 token 上限
        resume_from_run_id=resume_id,
    )

    print("\nresult:")
    # 打印确认后的发现：[严重度] 维度: 标题
    for f in out["result"].get("confirmed", []):
        print(f"  [{f['severity']:<6}] {f['dimension']}: {f['title']}")
    task = out["task"]
    usage = task["usage"]
    print(f"\nstatus={task['status']}  agents={usage['agents']}  "
          f"tokens={usage['tokens']}  journal=.runtime/{task['runId']}.journal.jsonl")


PROMPT = "\033[36ms16 >> \033[0m"
# \001/\002 tell Readline the ANSI escapes have zero display width.
# 【中文】\001/\002 告诉 readline：ANSI 转义序列占 0 显示宽度，
# 否则带颜色提示符会导致行编辑时光标错位。
READLINE_PROMPT = "\001\033[36m\002s16 >> \001\033[0m\002"


def run_cli():
    """Run the cumulative s15 host with Workflow added to its tool pool.
    【中文】REPL 入口：加载累计到 s15 的完整宿主，注入 Workflow 工具，
    然后复用宿主的控制台/钩子/agent 循环跑交互会话。
    主线程负责读输入 + 提交用户消息；后台线程跑宿主的异步事件循环。"""
    host = load_integrated_host()
    install_workflow_tool(host)
    host.CONSOLE.set_prompt(PROMPT, READLINE_PROMPT)
    host.CLI_ACTIVE = True
    host.start_runtime_services()
    print("s16: workflow runtime")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = host.update_context({}, history)
    session_state = {"active_user_request": "(no active user request)"}
    threading.Thread(
        target=host.async_event_loop,
        args=(history, context, session_state),
        daemon=True,
    ).start()
    while True:
        try:
            query = host.CONSOLE.ask()
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        with host.agent_lock:
            # 与 s15 一致的单轮流程：触发钩子 -> 记录用户消息 -> agent 循环 -> 刷新上下文
            host.trigger_hooks("UserPromptSubmit", query)
            turn_start = len(history)
            session_state["active_user_request"] = query
            history.append({"role": "user", "content": query})
            host.agent_loop(history, context, query)
            context = host.update_context(context, history)
            host.print_turn_assistants(history, turn_start)
        print()


if __name__ == "__main__":
    # 命令行分发：demo / resume 走确定性演示；否则进入完整 REPL
    if sys.argv[1:] and sys.argv[1] in {"demo", "resume"}:
        asyncio.run(run_demo(sys.argv[1:]))
    else:
        run_cli()
