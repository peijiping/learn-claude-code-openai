#!/usr/bin/env python3
"""
cron_scheduler.py - Cron 定时任务调度器（CronScheduler 类）

从 s14 教程的模块级函数重构为类形式，核心变化：
- 队列处理器不再抢 agent_lock 注入共享 messages，而是为每个触发任务
  创建独立 Agent 实例（session_prefix="cron_"），每个任务拥有独立会话文件。
- 会话文件存储在 .chathistory/cron_{N}.jsonl，与主会话 session_{N}.jsonl 隔离。

架构（保留教程三层设计）：
  1. 调度线程：独立 daemon 线程，每秒轮询，cron_matches 匹配 → 入队
  2. 任务队列：cron_queue 解耦调度与执行
  3. 队列处理器：逐条消费队列，为每个 job 创建 Agent(session_prefix="cron_")
     → init_session(resume=False) → run_turn("[Scheduled] {prompt}")
"""

import json
import random
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Union


# ── CronJob 数据结构（与教程一致）──────────────────────────────────


@dataclass
class CronJob:
    """一条 cron 定时任务。"""
    # 任务唯一 ID，格式如 cron_123456（6 位随机数）
    id: str
    # 5 段 cron 表达式："分 时 日 月 周"，例如 "0 9 * * *" 表示每天 9:00
    cron: str
    # 触发时注入到 agent 上下文的提示词（作为 user message 追加）
    prompt: str
    # True=周期性任务（命中后保留）；False=一次性任务（命中后移除）
    recurring: bool
    # True=持久化到 .scheduled_tasks.json，重启后仍生效
    durable: bool


# ── CronScheduler 类 ──────────────────────────────────────────────


class CronScheduler:
    """Cron 定时任务调度器。

    每个进程只需一个实例（调度线程是全局唯一的）。
    通过工具层（schedule_cron / list_crons / cancel_cron）供大模型调用。
    """

    def __init__(self, chat_history_dir: Path, durable_path: Path):
        self.chat_history_dir = chat_history_dir
        self.durable_path = durable_path

        # ── 任务注册表 ──
        self.scheduled_jobs: dict[str, CronJob] = {}
        # ── 触发队列 ──
        self.cron_queue: list[CronJob] = []
        # ── 线程安全锁 ──
        self._lock = threading.Lock()
        # ── 防重放：job_id → "YYYY-MM-DD HH:MM" ──
        self._last_fired: dict[str, str] = {}
        # ── 运行标志 ──
        self._running = False

    # ═══════════════════════════════════════════════════════════
    #  Cron 匹配与校验（静态方法，与教程完全一致）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _cron_field_matches(field: str, value: int) -> bool:
        """匹配单个 cron 字段（如 "*/5" / "1,3,5" / "9-17" / "30"）与具体数值。"""
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return step > 0 and value % step == 0
        if "," in field:
            return any(CronScheduler._cron_field_matches(f.strip(), value)
                       for f in field.split(","))
        if "-" in field:
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        return value == int(field)

    @staticmethod
    def cron_matches(cron_expr: str, dt: datetime) -> bool:
        """判断 5 段 cron 表达式是否匹配给定时间。
        采用标准 cron 语义：DOM（日）和 DOW（周）同时被约束时取「或」逻辑。"""
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0

        m = CronScheduler._cron_field_matches(minute, dt.minute)
        h = CronScheduler._cron_field_matches(hour, dt.hour)
        dom_ok = CronScheduler._cron_field_matches(dom, dt.day)
        month_ok = CronScheduler._cron_field_matches(month, dt.month)
        dow_ok = CronScheduler._cron_field_matches(dow, dow_val)

        if not (m and h and month_ok):
            return False
        dom_unconstrained = dom == "*"
        dow_unconstrained = dow == "*"
        if dom_unconstrained and dow_unconstrained:
            return True
        if dom_unconstrained:
            return dow_ok
        if dow_unconstrained:
            return dom_ok
        return dom_ok or dow_ok

    @staticmethod
    def _validate_cron_field(field: str, lo: int, hi: int) -> Optional[str]:
        """校验单个 cron 字段值是否落在 [lo, hi] 范围内，返回错误信息或 None。"""
        if field == "*":
            return None
        if field.startswith("*/"):
            step_str = field[2:]
            if not step_str.isdigit():
                return f"Invalid step: {field}"
            step = int(step_str)
            if step <= 0:
                return f"Step must be > 0: {field}"
            return None
        if "," in field:
            for part in field.split(","):
                err = CronScheduler._validate_cron_field(part.strip(), lo, hi)
                if err:
                    return err
            return None
        if "-" in field:
            parts = field.split("-", 1)
            if not parts[0].isdigit() or not parts[1].isdigit():
                return f"Invalid range: {field}"
            a, b = int(parts[0]), int(parts[1])
            if a < lo or a > hi or b < lo or b > hi:
                return f"Range {field} out of bounds [{lo}-{hi}]"
            if a > b:
                return f"Range start > end: {field}"
            return None
        if not field.isdigit():
            return f"Invalid field: {field}"
        val = int(field)
        if val < lo or val > hi:
            return f"Value {val} out of bounds [{lo}-{hi}]"
        return None

    @staticmethod
    def validate_cron(cron_expr: str) -> Optional[str]:
        """校验完整 cron 表达式。各字段合法范围：
            minute: 0-59, hour: 0-23, day-of-month: 1-31, month: 1-12, day-of-week: 0-6（周日=0）"""
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
        for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
            err = CronScheduler._validate_cron_field(field, lo, hi)
            if err:
                return f"{name}: {err}"
        return None

    # ═══════════════════════════════════════════════════════════
    #  持久化
    # ═══════════════════════════════════════════════════════════

    def _save_durable_jobs(self):
        """把所有 durable=True 的任务持久化到 .scheduled_tasks.json。"""
        durable = [asdict(j) for j in self.scheduled_jobs.values() if j.durable]
        self.durable_path.write_text(json.dumps(durable, indent=2, ensure_ascii=False))

    def _load_durable_jobs(self):
        """进程启动时从 .scheduled_tasks.json 恢复任务。"""
        if not self.durable_path.exists():
            return
        try:
            jobs = json.loads(self.durable_path.read_text())
            for j in jobs:
                job = CronJob(**j)
                err = self.validate_cron(job.cron)
                if err:
                    print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                    continue
                self.scheduled_jobs[job.id] = job
            valid = [j for j in jobs if j["id"] in self.scheduled_jobs]
            if valid:
                print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    #  任务管理（供工具层调用）
    # ═══════════════════════════════════════════════════════════

    def schedule_job(self, cron: str, prompt: str, recurring: bool = True,
                     durable: bool = True) -> Union[CronJob, str]:
        """注册一条新的 cron 任务。成功返回 CronJob，失败返回错误字符串。"""
        err = self.validate_cron(cron)
        if err:
            return err
        job = CronJob(
            id=f"cron_{random.randint(0, 999999):06d}",
            cron=cron, prompt=prompt,
            recurring=recurring, durable=durable,
        )
        with self._lock:
            self.scheduled_jobs[job.id] = job
            # 防止刚注册就立即触发：将 last_fired 设为当前分钟，至少等到下一分钟才首次触发
            self._last_fired[job.id] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if durable:
            self._save_durable_jobs()
        print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
        return job

    def cancel_job(self, job_id: str) -> str:
        """取消一条 cron 任务。若该任务是 durable 的，会同步从磁盘删除。"""
        with self._lock:
            job = self.scheduled_jobs.pop(job_id, None)
        if not job:
            return f"Job {job_id} not found"
        if job.durable:
            self._save_durable_jobs()
        print(f"  \033[31m[cron cancel] {job_id}\033[0m")
        return f"Cancelled {job_id}"

    def list_jobs(self) -> list[CronJob]:
        """返回当前所有已注册任务（只读快照）。"""
        with self._lock:
            return list(self.scheduled_jobs.values())

    # ═══════════════════════════════════════════════════════════
    #  工具层薄包装（供 ToolRegistry.handlers 调用）
    # ═══════════════════════════════════════════════════════════

    def run_schedule_cron(self, cron: str, prompt: str,
                          recurring: bool = True, durable: bool = True) -> str:
        """schedule_cron 工具处理器。"""
        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' → {prompt}"

    def run_list_crons(self) -> str:
        """list_crons 工具处理器。"""
        jobs = self.list_jobs()
        if not jobs:
            return "No cron jobs. Use schedule_cron to add one."
        lines = []
        for j in jobs:
            tag = "recurring" if j.recurring else "one-shot"
            dur = "durable" if j.durable else "session"
            lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                         f"[{tag}, {dur}]")
        return "\n".join(lines)

    def run_cancel_cron(self, job_id: str) -> str:
        """cancel_cron 工具处理器。"""
        return self.cancel_job(job_id)

    # ═══════════════════════════════════════════════════════════
    #  调度线程（与教程一致）
    # ═══════════════════════════════════════════════════════════

    def _scheduler_loop(self):
        """调度线程主体（独立 daemon 线程）。
        每 1 秒检查一次：遍历所有任务，时间匹配则入队。"""
        while self._running:
            time.sleep(1)
            now = datetime.now()
            minute_marker = now.strftime("%Y-%m-%d %H:%M")
            with self._lock:
                for job in list(self.scheduled_jobs.values()):
                    try:
                        if self.cron_matches(job.cron, now):
                            if self._last_fired.get(job.id) != minute_marker:
                                self.cron_queue.append(job)
                                self._last_fired[job.id] = minute_marker
                                # print(f"  \033[35m[cron fire] {job.id} → "
                                #       f"{job.prompt[:40]}\033[0m")
                            if not job.recurring:
                                self.scheduled_jobs.pop(job.id, None)
                                if job.durable:
                                    self._save_durable_jobs()
                    except Exception as e:
                        print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")

    # ═══════════════════════════════════════════════════════════
    #  队列处理器（变化点：创建独立 Agent 实例执行）
    # ═══════════════════════════════════════════════════════════

    def _has_queue(self) -> bool:
        """判断队列是否非空。"""
        with self._lock:
            return bool(self.cron_queue)

    def _consume_queue(self) -> list[CronJob]:
        """一次性取出并清空队列。"""
        with self._lock:
            fired = list(self.cron_queue)
            self.cron_queue.clear()
        return fired

    def _execute_job(self, job: CronJob):
        """为单个 cron 任务创建独立 Agent 实例并执行。

        每个 cron 触发都走独立 Agent(session_prefix="cron_") → 独立会话文件，
        与主 REPL 完全隔离，不需要抢 agent_lock。
        """
        # 延迟 import 避免循环依赖：cron_scheduler ← agent_full_v2
        from agent_full_v2 import Agent

        try:
            agent = Agent(session_prefix="cron_", silent=True)
            agent.init_session(resume=False)
            # print(f"  \033[35m[cron execute] {job.id} → session "
            #       f"cron_{agent.session_num}\033[0m")
            result = agent.run_turn(f"[Scheduled] {job.prompt}")
        except Exception as e:
            print(f"  \033[31m[cron execute error] {job.id}: {e}\033[0m")

    def _queue_processor_loop(self):
        """队列处理器（独立 daemon 线程）。

        不再与主 REPL 共享 agent_lock，而是为每个触发任务创建独立 Agent 实例。
        队列为空时每 0.5 秒检查一次。
        """
        while self._running:
            time.sleep(0.5)
            if not self._has_queue():
                continue
            jobs = self._consume_queue()
            for job in jobs:
                self._execute_job(job)

    # ═══════════════════════════════════════════════════════════
    #  生命周期
    # ═══════════════════════════════════════════════════════════

    def start(self):
        """启动调度器：加载持久化任务 → 启动调度线程 + 队列处理器线程。

        调用顺序很关键：必须先恢复任务再启动线程，否则启动瞬间可能错过本应触发的任务。
        """
        self._load_durable_jobs()
        self._running = True
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        threading.Thread(target=self._queue_processor_loop, daemon=True).start()
        print("  \033[35m[cron] scheduler + queue processor started\033[0m")

    def stop(self):
        """停止调度器（线程为 daemon，进程退出时自动结束；此方法仅设标志位）。"""
        self._running = False