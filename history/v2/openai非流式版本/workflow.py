#!/usr/bin/env python3
"""
workflow.py - 工作流运行时（Workflow Runtime，s16）

核心思想：把「多智能体编排计划」写成确定性 Python 代码（计划即代码，
not a chat turn），模型只需通过一次 run_workflow 工具调用，就能执行整段
已保存的编排脚本。模型只能按 name 调用注册表中的工作流，永远不能直接
提交可执行代码——这是「代码由人保存、模型只按名调用」的安全边界。

三种编排原语（由 ExecutionState 作为 ctx 注入工作流脚本）：
  - agent(prompt, schema) : 派生一个子智能体执行单步任务；传 schema 时强制
                            结构化 JSON 输出并校验（失败自动重试一次）
  - parallel(thunks)      : 屏障式并发（BARRIER）——所有任务全部完成才继续，
                            任一任务抛异常则整个工作流失败
  - pipeline(items, ...)  : 按条目流水线——条目 A 可以先推进到第 3 阶段，
                            而条目 B 还停留在第 1 阶段（阶段之间没有屏障）

可靠性支撑设施：
  - journal  (<runId>.journal.jsonl) : 追加式执行日志；resume 时按「语义键」
    （kind+label+prompt+schema 的稳定哈希）命中缓存，直接重放结果
  - snapshot (<runId>.json)          : 启动参数与任务状态快照，resume 时校验
    工作流名与入参是否与原始运行一致
  - run lock : 双层互斥——线程级 threading.Lock 注册表（实例级）+ 进程级
    fcntl 文件锁，保证同一 runId 不会被并发重复执行
  - Budget / AGENT_CAP / CONCURRENCY : Token 预算、agent() 调用次数上限、
    并发信号量，防止工作流失控

与教程（anthropic_v2.1/s16）的差异：
  - AnthropicAgentRunner → OpenAIAgentRunner（复用宿主的 OpenAI 客户端）
  - WorkflowTool / WORKFLOWS 注册表 / run_workflow / run_workflow_sync
    四者合并为 WorkflowManager 一个类（实例级，无模块级全局单例）
  - STORE 改为构造注入的 store_dir（paths.WORKFLOW_DIR）
  - 可调参数从 .env 读取（WORKFLOW_AGENT_CAP / WORKFLOW_CONCURRENCY /
    WORKFLOW_MAX_TOKENS）
"""

import asyncio          # 异步编排：Semaphore（并发上限）、gather（并发聚合）、to_thread（同步 LLM 调用丢线程池）
import fcntl            # 文件锁（POSIX），实现跨进程的 runId 互斥
import hashlib          # 跨进程稳定哈希（内置 hash() 每次进程启动加盐，不能用于 resume）
import json
import os               # O_CREAT|O_EXCL 原子占位 runId 文件；os.replace 原子写快照
import re               # 工作流名 / runId 的白名单正则校验
import secrets          # 加密安全的随机数，生成 runId 后缀
import threading        # 线程级的 run 锁注册表
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# ── 运行时护栏：白名单正则（格式校验，非可调参数，不走 .env） ──────────
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")          # 工作流名：1-64 位 slug
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$") # runId：wf_<名>_<16位hex>

# journal 缓存未命中的哨兵对象——不能用 None，因为缓存值本身可能是 null
MISS = object()


class WorkflowInputError(Exception):
    """工作流/元数据/Schema 输入不合法时抛出的统一异常。
    在工具层（run_sync）会被捕获并转成 "Error: ..." 文本回给模型。"""


def _stable_hash(s: str) -> int:
    """跨进程稳定哈希。Python 内置 hash() 对字符串每次进程启动随机加盐，
    会让 journal 的 resume 缓存键对不上；这里用 SHA-256 保证 run 与 resume
    两个进程算出同一个键。"""
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def _write_json(path: Path, value) -> None:
    """原子写 JSON：先写 .tmp 临时文件，再用 os.replace 原子替换，
    避免读方（resume/快照校验）读到写了一半的残缺文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _parse_runner_json(text: str):
    """解析模型返回的 JSON：兼容 ```json 代码围栏；围栏剥掉后仍失败，
    则扫描文本里第一个能成功 raw_decode 的 '{'，从中截取 JSON 对象。
    全部失败 => 抛 WorkflowInputError，由上层触发一次重试。"""
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


def _serialize_task(task) -> dict:
    """把 LocalWorkflowTask 序列化为可 JSON 化的任务快照（写进 <runId>.json）。"""
    return {
        "taskId": task.task_id,
        "taskType": "local_workflow",
        "runId": task.run_id,
        "workflowName": task.meta["name"],
        "status": task.status,
        "usage": dict(task.usage),
        "progress": list(task.progress),
    }


# ═══════════════════════════════════════════════════════════
#  极简 JSON Schema 校验器（agent({schema}) 的结构化输出闭环）
# ═══════════════════════════════════════════════════════════
class SimpleJsonSchema:
    """迷你校验器，只覆盖工作流用到的子集：
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
                # 对每个元素递归校验，错误信息带下标方便定位
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


# ═══════════════════════════════════════════════════════════
#  Token 预算
# ═══════════════════════════════════════════════════════════
class Budget:
    """Token 预算：total=None 表示不设限。已消耗达到上限后，
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


# ═══════════════════════════════════════════════════════════
#  子智能体执行器（OpenAI SDK 版）
# ═══════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RunnerOutput:
    """执行器统一返回值：value=结果（文本或已解析的 JSON），tokens=本次消耗的 token 数"""
    value: object
    tokens: int


class OpenAIAgentRunner:
    """真实执行器：复用宿主（Agent 实例）的 OpenAI 客户端与模型配置来跑
    工作流子智能体。有 schema 时把 schema 序列化进 prompt，要求模型只回一个
    符合格式的 JSON 对象（Structured Output 的「穷人版」实现）。
    注意：子智能体是「单步无工具」的 focused LLM 调用，与 s16 教程一致。"""

    def __init__(self, client, model, max_tokens=2000):
        self.client = client        # OpenAI SDK 实例（来自 LLMClient().llm）
        self.model = model          # 模型 ID（OPENAI_MODEL_ID）
        self.max_tokens = max_tokens

    def run(self, prompt, schema=None, label=None) -> RunnerOutput:
        request = prompt
        if schema is not None:
            # 把 schema 以确定性顺序（sort_keys）拼进请求，方便模型对照
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                # 系统提示：专注完成单步，禁止谎称访问了 prompt 之外的文件/结果
                {"role": "system",
                 "content": "You are a focused workflow agent. Complete only the "
                            "supplied step. Do not claim access to files or results "
                            "not included in the prompt."},
                {"role": "user", "content": request},
            ],
            max_tokens=self.max_tokens,
        )
        text = (response.choices[0].message.content or "").strip()
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
        tokens = int(getattr(usage, "prompt_tokens", 0) or 0) + int(
            getattr(usage, "completion_tokens", 0) or 0
        )
        return RunnerOutput(value, tokens)


# ═══════════════════════════════════════════════════════════
#  Journal（执行日志：resume 缓存重放的基石）
# ═══════════════════════════════════════════════════════════
class WorkflowJournal:
    """追加式日志文件 <runId>.journal.jsonl，每行一条 {"key": ..., "value": ...}。
    key 是 agent() 调用的「语义键」（kind+label+prompt+schema 的稳定哈希）——
    与并发顺序无关，因此 parallel/pipeline 里同一调用在 resume 时能算出同一个键。
    resume 打开旧日志时把全部记录装进内存 cache；之后 agent() 命中缓存就直接
    重放结果，不再真实调用模型。新运行则以 "w" 模式打开（截断旧文件）。"""

    def __init__(self, run_id, resume, store_dir: Path):
        store_dir.mkdir(parents=True, exist_ok=True)
        self.path = store_dir / f"{run_id}.journal.jsonl"
        self.resume = resume
        self.cache = {}
        if resume:
            # resume 模式：日志文件必须存在，且每行必须是合法的 key/value 记录
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(
                    self.path.read_text(encoding="utf-8").splitlines(), start=1):
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
            self._f = self.path.open("w", encoding="utf-8")   # 全新运行：截断旧文件

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


# ═══════════════════════════════════════════════════════════
#  工作流任务生命周期（状态机 + 用量 + 进度事件）
# ═══════════════════════════════════════════════════════════
class LocalWorkflowTask:
    """本地工作流任务的运行时载体：保存状态机（running/completed/failed）、
    用量统计（agents/tokens）与进度事件流，并把事件实时打印到终端。"""

    def __init__(self, task_id, run_id, meta, silent=False):
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.status = "running"                        # 状态机：running -> completed | failed
        self.usage = {"agents": 0, "tokens": 0}        # 用量：子智能体调用次数 / token 消耗
        self.progress = []                             # 进度事件列表（可序列化回传给宿主）
        self.silent = silent                           # cron/silent 模式下抑制打印

    def _print(self, *args, **kwargs):
        if not self.silent:
            print(*args, **kwargs)

    def event(self, name, **data):
        # 生命周期事件（async_launched / task_started / task_notification）
        line = " ".join(f"{k}={v}" for k, v in data.items())
        self._print(f"  event      {name:<18} {line}")

    def progress_event(self, ptype, **data):
        # 进度事件（workflow_phase / workflow_log / workflow_agent），同时记内存 + 打印
        self.progress.append({"type": ptype, **data})
        line = " ".join(f"{k}={v}" for k, v in data.items())
        self._print(f"  progress   {ptype:<16} {line}")


# ═══════════════════════════════════════════════════════════
#  执行原语（注入给工作流脚本的编排上下文）
# ═══════════════════════════════════════════════════════════
class ExecutionLimits:
    """整个运行共享的限额（嵌套子工作流也共用同一份）：
    agents 计数器用于 AGENT_CAP 总量限制；semaphore 用于并发上限。"""

    def __init__(self, agent_cap, concurrency):
        self.agent_cap = agent_cap                     # agent() 调用次数硬上限
        self.agents = 0                                # 已派生的 agent() 总数（含嵌套工作流）
        self.semaphore = asyncio.Semaphore(concurrency)  # 并发信号量

    def claim_agent(self):
        # 领取一个「派生名额」：超上限立即失败，防止工作流自我爆炸
        self.agents += 1
        if self.agents > self.agent_cap:
            raise WorkflowInputError(f"agent() cap reached ({self.agent_cap})")


class ExecutionState:
    """执行状态 / 编排上下文：作为 ctx 注入工作流脚本，是工作流代码里
    唯一能接触到的「能力面」——ctx.agent / ctx.parallel / ctx.pipeline /
    ctx.workflow / ctx.phase / ctx.log。所有可靠性机制（缓存、预算、限额、
    结构化校验）都封装在这里，工作流作者无需关心。"""

    def __init__(self, task, journal, runner, budget, args,
                 registry, agent_cap, concurrency, depth=0, limits=None):
        self.task = task          # LocalWorkflowTask：状态/用量/进度
        self.journal = journal    # WorkflowJournal：缓存与落盘
        self.runner = runner      # OpenAIAgentRunner：子智能体执行器
        self.budget = budget      # Budget：token 预算
        self.args = args          # 工作流入参
        self._registry = registry # 工作流注册表（workflow() 嵌套时按名解析）
        self._depth = depth       # 嵌套深度：workflow() 只允许一层子工作流
        self._phase = None        # 当前阶段名（新 agent() 默认归属该阶段）
        self._phases_seen = set() # 已广播过的阶段名（用于 upsert 去重）
        # 与父级共享的运行限额（agent_cap/concurrency 首次创建时生效）
        self._limits = limits or ExecutionLimits(agent_cap, concurrency)

    def phase(self, title):
        """声明/切换当前阶段：之后的 agent() 默认归入该阶段。
        Upsert 语义：同一阶段名重复声明（例如 pipeline 每个条目都调一次
        ctx.phase("Verify")）只会在第一次广播 workflow_phase 事件，不重复刷屏。"""
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """输出一条工作流日志进度事件。"""
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        """派生一个子智能体执行单步任务。核心流程：
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
            # runner.run 是同步阻塞调用，丢进线程池避免卡事件循环
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
        """屏障式并发：所有 thunk（异步闭包）同时开跑并全部等待完成。
        任一 thunk 抛异常 => gather 立即把异常抛出 => 整个工作流失败（fail-fast）。"""
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """按条目流水线：每个条目独立穿过全部阶段，阶段之间【没有屏障】——
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
        """把另一个已保存的工作流作为子工作流内联运行：
        - 只允许一层嵌套（子工作流里不能再 workflow()），防止递归失控
        - 子工作流与父级共享同一份 journal（缓存互通）、budget（预算共用）、
          agent 计数器（AGENT_CAP 共享），表现为「同一次运行」的一部分"""
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in self._registry:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        meta, fn = self._registry[name]
        child = ExecutionState(
            self.task, self.journal, self.runner, self.budget,
            args or {}, self._registry,
            agent_cap=self._limits.agent_cap, concurrency=None,
            depth=self._depth + 1, limits=self._limits,
        )
        return await fn(child, args or {})


# ═══════════════════════════════════════════════════════════
#  WorkflowManager：注册表 + 运行器 + 同步桥（工具层唯一入口）
# ═══════════════════════════════════════════════════════════
class WorkflowManager:
    """工作流管理器（s16 教程 WorkflowTool + WORKFLOWS + run_workflow +
    run_workflow_sync 的合并体），由 Agent 实例持有，实例级无全局单例。

    职责：
    - 注册表：register() 保存 (meta, 脚本函数)，模型只能按 name 调用
    - 运行器：run() 校验元数据 → 原子占坑 runId → 双重锁 → 快照/journal
      → 执行脚本 → 落输出文件与最终快照（resume 能力完整保留）
    - 同步桥：run_sync() 供 ToolRegistry 的同步工具分发器调用
    """

    def __init__(self, store_dir: Path, llm_client, model: str, silent: bool = False):
        # 持久化目录（paths.WORKFLOW_DIR）：快照 + journal + 输出文件
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # 复用宿主的 OpenAI 客户端与模型 ID 跑工作流子智能体
        self.llm_client = llm_client
        self.model = model
        self.silent = silent
        # 可调参数从 .env 惰性读取（int(... or 默认值)，与 llm_manage 风格一致；
        # 不能在模块 import 期读取——agent_full_v2 的 load_dotenv 在 import 块之后才执行）
        self.agent_cap = int(os.environ.get("WORKFLOW_AGENT_CAP") or 1000)
        self.concurrency = int(os.environ.get("WORKFLOW_CONCURRENCY") or 8)
        self.max_tokens = int(os.environ.get("WORKFLOW_MAX_TOKENS") or 2000)
        # 工作流注册表：name -> (meta, async 脚本函数)；构造时注册内置示例
        self._workflows: dict = {}
        # 线程级 run 锁注册表（实例级）：runId -> threading.Lock
        self._run_locks_guard = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}

    # ── 注册表管理 ──────────────────────────────────────────
    def register(self, name: str, meta: dict, script_fn) -> None:
        """注册一个工作流：name 必须与 meta.name 一致，script_fn 为
        async def fn(ctx, args) 形式的编排脚本（计划即代码，由人保存）。"""
        if meta.get("name") != name:
            raise WorkflowInputError(f"meta.name ({meta.get('name')}) != register key ({name})")
        self._workflows[name] = (meta, script_fn)

    # ── 校验 ────────────────────────────────────────────────
    @staticmethod
    def _validate_meta(meta):
        """启动前校验工作流元数据：name / description 必填，
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

    @staticmethod
    def _validate_run_id(run_id):
        """校验 runId 格式（resume 入口尤其重要——它来自外部输入/模型）。"""
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise WorkflowInputError("invalid workflow runId")
        return run_id

    def _create_run_id(self, meta) -> str:
        # 生成新的运行标识：wf_<工作流名>_<16位随机hex>
        return f"wf_{meta['name']}_{secrets.token_hex(8)}"

    def _reserve_run_id(self, meta) -> str:
        """在任何 journal 被截断之前，先「占坑」一个全新的 runId。
        用 os.O_CREAT | os.O_EXCL 以原子方式创建 <runId>.json 空文件——
        若文件已存在（极小概率的随机碰撞）则换一个再试，最多 32 次。"""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(32):
            run_id = self._validate_run_id(self._create_run_id(meta))
            snapshot_path = self.store_dir / f"{run_id}.json"
            try:
                # O_EXCL 保证「创建」是原子的：文件已存在则抛 FileExistsError
                fd = os.open(snapshot_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue   # 随机碰撞，换一个 runId 重试
            os.close(fd)
            return run_id
        raise WorkflowInputError("could not allocate a unique workflow runId")

    # ── 双重锁：线程级（实例注册表）+ 进程级（fcntl 文件锁） ──
    @contextmanager
    def _run_lock(self, run_id: str):
        """让同一个 runId 在「本进程内多个线程」和「跨多个宿主进程」
        两个维度上都互斥，锁持有覆盖整个运行生命周期。
        1) 线程锁：非阻塞获取失败 => 同进程里已有相同 runId 在跑，直接报错；
        2) 文件锁：对 <runId>.lock 做 fcntl.flock（LOCK_NB 非阻塞），跨进程互斥。
        finally 中按相反顺序释放，并清理锁注册表条目避免无限增长。"""
        with self._run_locks_guard:
            local_lock = self._run_locks.setdefault(run_id, threading.Lock())
        if not local_lock.acquire(blocking=False):
            raise WorkflowInputError(f"workflow run {run_id} is already active")

        handle = None
        try:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            handle = (self.store_dir / f"{run_id}.lock").open("a+", encoding="utf-8")
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
            with self._run_locks_guard:
                # 若没有其他线程正在等这把锁，把它从注册表里移除
                if not local_lock.locked() and self._run_locks.get(run_id) is local_lock:
                    self._run_locks.pop(run_id, None)

    # ── 快照读写 + last_run 记录 ────────────────────────────
    def _read_snapshot(self, run_id) -> dict:
        """读取运行快照（resume 的一致性校验依据），文件缺失/损坏/非对象都报可读错误。"""
        path = self.store_dir / f"{run_id}.json"
        if not path.exists():
            raise WorkflowInputError(f"resume snapshot not found for {run_id}")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowInputError(f"invalid resume snapshot for {run_id}") from exc
        if not isinstance(snapshot, dict):
            raise WorkflowInputError(f"invalid resume snapshot for {run_id}")
        return snapshot

    def _save_last_run(self, run_id):
        # 记录最近一次 runId（resume 命令/工具从这里读取）
        temporary = self.store_dir / "last_run.txt.tmp"
        temporary.write_text(run_id, encoding="utf-8")
        os.replace(temporary, self.store_dir / "last_run.txt")

    def read_last_run(self):
        """读取最近一次 runId；不存在返回 None（外部 resume 入口用）。"""
        p = self.store_dir / "last_run.txt"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    # ── 核心运行器（async） ─────────────────────────────────
    async def run(self, name, args=None, resume_from_run_id=None) -> dict:
        """面向模型的适配层：模型只给工作流「名字」，实际执行的代码由
        注册表解析（安全边界：模型永远不能直接提交可执行代码）。
        resume_from_run_id 非空时走 resume 路径（快照校验 + journal 缓存重放）。"""
        if not isinstance(name, str):
            raise WorkflowInputError("workflow name must be a string")
        if name not in self._workflows:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        if args is not None and not isinstance(args, dict):
            raise WorkflowInputError("workflow args must be an object")
        meta, script_fn = self._workflows[name]
        self._validate_meta(meta)

        resuming = resume_from_run_id is not None
        if resuming:
            # resume：沿用旧 runId（先做格式校验）
            run_id = self._validate_run_id(resume_from_run_id)
        else:
            # 新运行：先原子占坑一个唯一 runId，再开始执行
            run_id = self._reserve_run_id(meta)
        with self._run_lock(run_id):
            # 全程持有 run 级锁：同 runId 在任何线程/进程都不允许并发执行
            return await self._run_locked(meta, script_fn, args, run_id, resuming)

    async def _run_locked(self, meta, script_fn, args, run_id, resuming) -> dict:
        if resuming:
            # ---- resume 路径：读快照做一致性校验 ----
            snapshot = self._read_snapshot(run_id)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("resume runId does not match workflow meta")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args              # 未传参 => 沿用原始入参
            elif args != saved_args:
                raise WorkflowInputError("resume args do not match the original run")
            journal = WorkflowJournal(run_id, resume=True, store_dir=self.store_dir)
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False, store_dir=self.store_dir)
        task_id = f"local_workflow_{run_id}"   # 由 runId 派生任务 ID

        task = LocalWorkflowTask(task_id, run_id, meta, silent=self.silent)
        # 记录「启动信封」（返回给调用方的第一段信息），然后广播生命周期事件
        launched = {"status": "async_launched", "taskId": task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task_id)
        task.event("task_started", workflow=meta["name"],
                   phases=",".join(meta.get("phases", [])) or "-",
                   resume=resuming)
        # 写初始快照（runId / 工作流名 / 入参 / 任务状态），供 resume 校验
        _write_json(self.store_dir / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": _serialize_task(task),
        })

        try:
            # 组装执行上下文：Budget 从 args["budget"] 读取（None = 不限）
            ctx = ExecutionState(
                task, journal,
                OpenAIAgentRunner(self.llm_client, self.model, self.max_tokens),
                Budget(args.get("budget")), args,
                registry=self._workflows,
                agent_cap=self.agent_cap, concurrency=self.concurrency,
            )
            result = await script_fn(ctx, args)   # 真正执行工作流脚本
            task.status = "completed"
        except Exception as e:
            # 任何异常（含预算超限/校验失败/用户停止）都把状态收口为 failed，
            # 错误对象作为 result 返回——已完成的步骤仍留在 journal 里可 resume
            task.status = "failed"
            result = {"error": str(e)}
        finally:
            journal.close()

        # 无论成败：落输出文件 + 刷新最终快照 + 记录 last_run（供 resume 取用）
        _write_json(self.store_dir / f"{run_id}.output.json", result)
        _write_json(self.store_dir / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": _serialize_task(task),
        })
        self._save_last_run(run_id)
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f"{self.store_dir.name}/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": _serialize_task(task)}

    # ── 同步桥（ToolRegistry 的工具分发是同步的） ───────────
    def run_sync(self, name, args=None, resume_from_run_id=None) -> str:
        """同步桥：用 asyncio.run 把异步工作流运行时桥接到同步工具分发器。
        业务错误（WorkflowInputError）转成 "Error: ..." 文本回给模型，
        而不是让异常炸掉宿主循环。"""
        try:
            return json.dumps(
                asyncio.run(self.run(name, args, resume_from_run_id)),
                default=str,
            )
        except WorkflowInputError as exc:
            return f"Error: {exc}"


# ═══════════════════════════════════════════════════════════
#  内置示例工作流：代码审查多维度流水线（review-changes）
# ═══════════════════════════════════════════════════════════
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


async def sample_workflow(ctx, args):
    """示例工作流：对每个审查维度跑「审查 -> 逐条对抗验证」的流水线，
    只保留验证器确认真实的发现。编排计划是 Python 代码，不是对话轮次——
    模型只负责调用一次 run_workflow 工具，结构完全由这段代码决定。

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


def register_default_workflows(manager: WorkflowManager) -> None:
    """把内置示例工作流注册进管理器（WorkflowManager 构造后调用一次）。"""
    manager.register(SAMPLE_META["name"], SAMPLE_META, sample_workflow)
