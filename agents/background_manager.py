"""
BackgroundManager —— 适配 OpenAI SDK 的后台任务管理器

设计要点（与 Anthropic 教程版的差异）：
1. 不再依赖 Anthropic 的 block 对象（block.name / block.input / block.id）。
   - 改为接收 OpenAI 风格的 tool_call_id + 工具名 + 已解析的 tool_args
2. 不再内置"按 block 路由到 TOOL_HANDLERS"的 execute_tool()。
   - 改为接收一个 executor 闭包，由主循环把同步执行逻辑闭包进来。
   - 这样 sub_agent / 普通工具 / 自定义逻辑全部走同一条后台路径。
3. 占位 result 由调用方构造（教程原话模板），
   background_manager 只负责"启动线程 + 收集通知"两件事。
"""
import threading
import time
from typing import Callable

from logger import get_logger

# 统一日志：后台任务派发/完成打点（排查"后台子智能体期间前端状态断了"的对照源）
log = get_logger("background")


class BackgroundManager:
    """
    后台任务管理器：守护线程执行 + 通知队列。

    三个全局状态 + 一把锁：
       _bg_counter        后台任务自增计数器，用于生成唯一 bg_id
       background_tasks   生命周期字典：bg_id → {tool_call_id, command, status}
       background_results 输出缓存：bg_id → 最终输出字符串
       background_lock    线程锁：后台线程与主线程都会读写上述两个字典，
                          必须加锁避免并发读写导致数据损坏 / 脏读
    """

    def __init__(self):
        self.bg_counter = 0
        self.background_tasks: dict[str, dict] = {}   # bg_id → {tool_call_id, command, status}
        self.background_results: dict[str, str] = {}   # bg_id → output
        self.background_lock = threading.Lock()

    # 兜底启发式：从命令文本里猜它是不是"慢操作"（预计超过 30 秒）。
    # 规则很简单——只对 bash 生效，命令里出现 install / build / test /
    # deploy / compile 等关键词就认为是慢操作。
    # 关键词命中是"可能慢"，宁可多后台化也不阻塞主循环。
    def is_slow_operation(self, tool_name: str, tool_args: dict) -> bool:
        """Fallback heuristic: commands likely to take > 30s."""
        if tool_name != "bash":
            return False
        cmd = tool_args.get("command", "").lower()
        slow_keywords = ["install", "build", "test", "deploy", "compile",
                         "docker build", "pip install", "npm install",
                         "cargo build", "pytest", "make"]
        return any(kw in cmd for kw in slow_keywords)

    # 判断这个工具调用要不要进后台。
    # 优先级：模型显式传了 run_in_background=True → 听模型的；
    # 没传 → 退回启发式判断（is_slow_operation）。
    # 这就是"模型显式意图优先、启发式兜底"的双保险设计。
    def should_run_background(self, tool_name: str, tool_args: dict) -> bool:
        """Model explicit request takes priority; fallback to heuristic."""
        if tool_args.get("run_in_background"):
            return True
        return self.is_slow_operation(tool_name, tool_args)

    # 把工具调用放到守护线程里异步执行，立即返回后台任务 ID。
    # 流程：
    #   1) 计数器 +1，生成 bg_id（如 bg_0001）；
    #   2) 先在 background_tasks 里登记状态为 running（此时拿到锁）；
    #   3) 启动 daemon 线程执行 worker：executor() 跑完后，
    #      加锁把状态改成 completed 并把输出写进 background_results；
    #      executor 抛异常时把异常字符串也当作结果写入，避免通知里丢失信息。
    #   4) 主线程不等待，直接返回 bg_id。
    # daemon=True 的意义：主程序退出时后台线程自动结束，不会残留线程挂住进程。
    def start_background_task(
        self,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        executor: Callable[[], str],
    ) -> str:
        """Run executor in a daemon thread. Returns background task ID."""
        self.bg_counter += 1
        bg_id = f"bg_{self.bg_counter:04d}"
        # 显示文本：bash 取 command；sub_agent 取 prompt 前 80 字；其他用工具名
        cmd = (
            tool_args.get("command")
            or (tool_args.get("prompt", "")[:80] if tool_args.get("prompt") else "")
            or tool_name
        )

        def worker():
            started = time.monotonic()
            try:
                result = executor()
                if not isinstance(result, str):
                    result = str(result)
            except Exception as e:
                result = f"Error: {type(e).__name__}: {e}"
            with self.background_lock:
                self.background_tasks[bg_id]["status"] = "completed"
                self.background_results[bg_id] = result
            # 完成即打点（含耗时）：区分"任务真完成"与"结果尚未注入主循环"
            elapsed = time.monotonic() - started
            print(f"  \033[33m[background] completed {bg_id} ({elapsed:.1f}s): "
                  f"{cmd[:40]}\033[0m")
            log.info("[background] %s completed (%.1fs): %r", bg_id, elapsed, cmd[:60])

        with self.background_lock:
            self.background_tasks[bg_id] = {
                "tool_call_id": tool_call_id,
                "command": cmd,
                "status": "running",
            }
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
        log.info("[background] %s dispatched (tool=%s): %r", bg_id, tool_name, cmd[:60])
        return bg_id

    # 查询后台任务状态（不消费结果，可重复调用）。
    # 与 collect_background_results 的语义区别：
    # - collect_background_results 注入通知后把状态改为 notified（数据保留）
    # - check 是"查询"语义（只读 background_tasks / background_results，
    #   模型可以反复看，状态/结果都不会被弹出或清除）
    # 'notified' 与 'completed' 在 check 视角下等价：都返回完整结果，
    # 避免出现"通知已注入但查不到"的死路。
    # 用法：
    #   - 不传 task_id：列出所有后台任务的当前状态
    #   - 传 task_id：返回该任务的详细状态；已完成则附带结果
    def check(self, task_id: str | None = None) -> str:
        """Inspect background task status without consuming the result."""
        with self.background_lock:
            if task_id is None:
                if not self.background_tasks:
                    return "No background tasks."
                lines = []
                for bid, t in self.background_tasks.items():
                    if t["status"] in ("completed", "notified"):
                        result_preview = (self.background_results.get(bid) or "")[:200]
                        lines.append(
                            f"{bid}: [{t['status']}] {t['command'][:60]}\n"
                            f"  ↳ {result_preview}"
                        )
                    else:
                        lines.append(f"{bid}: [{t['status']}] {t['command'][:60]}")
                return "\n".join(lines)
            t = self.background_tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            if t["status"] in ("completed", "notified"):
                return (
                    f"[{task_id} {t['status']}]\n"
                    f"{self.background_results.get(task_id, '')}"
                )
            return f"[{task_id} still {t['status']}] {t['command'][:60]}"

    # 是否仍有后台任务在运行（goal Stop 钩子的 defer 分支用）：
    # 有任务处于 running 状态时，goal 评估"是否达成"不可靠，应暂缓判定。
    def has_running(self) -> bool:
        """Return True if any background task is still running."""
        with self.background_lock:
            return any(
                t["status"] == "running"
                for t in self.background_tasks.values()
            )

    # 是否有"已完成但尚未注入通知"的后台结果（auto-followup 用途）：
    # 完成后台任务的 watch 结束、但结果还没被主智能体消费过（collect
    # 才会把 completed → notified）时返回 True，供会话运行时决定是否
    # 自动续一轮 turn，让主智能体拿到 task_notification 并给出最终总结。
    def has_completed_pending(self) -> bool:
        """Return True if any finished background result is not yet consumed."""
        with self.background_lock:
            return any(
                t["status"] == "completed"
                for t in self.background_tasks.values()
            )

    # 收集所有已完成的后台任务，生成 task_notification 通知列表。
    # 设计要点（与教程版不同）：
    # - 通知里同时给 <summary>（200 字符预览）+ <full_output>（完整结果）：
    #   模型既能快速预览，也能直接读到全文，无需再查 check_background。
    # - 状态从 completed 改为 notified（不 pop 数据）：同一结果只注入一次，
    #   避免下轮重复出现；但 background_tasks / background_results 里的数据
    #   完整保留，check_background 仍能查到完整结果。
    # - task_notification 是独立消息格式（普通 text 块），而非复用 tool_result——
    #   因为 tool_result 必须对应具体 tool_call_id，而后台任务的结果与原始
    #   tool_use 早已"分离"了。
    # - 整段用 <system-reminder> 包裹：这是"只给模型看、不下发前端"的判定依据
    #   （ws_bridge._history_to_ui 按 startswith 过滤），详见下方实现处注释。
    def collect_background_results(self) -> list[str]:
        """收集已完成的后台任务，产出 <system-reminder> 包裹的 task_notification 消息。"""
        with self.background_lock:
            ready_ids = [bid for bid, task in self.background_tasks.items()
                         if task["status"] == "completed"]
        notifications = []
        for bg_id in ready_ids:
            with self.background_lock:
                task = self.background_tasks[bg_id]
                output = self.background_results.get(bg_id, "")
                # 标记为已通知而不是 pop：同一结果只注入一次，但数据保留
                self.background_tasks[bg_id]["status"] = "notified"
            summary = output[:200] if len(output) > 200 else output
            # 必须用 <system-reminder> 包裹：ws_bridge._history_to_ui 只按
            # startswith("<system-reminder>") 判定"系统注入消息"并跳过，否则这段
            # 通知会以**用户气泡**的形式漏到聊天界面（看着像用户自己说的话）。
            # 后台任务的用户可见性已由子智能体卡片 / 工具条承担，这里只给模型看。
            notifications.append(
                f"<system-reminder>\n"
                f"<task_notification>\n"
                f"  <task_id>{bg_id}</task_id>\n"
                f"  <status>completed</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"  <full_output>{output}</full_output>\n"
                f"</task_notification>\n"
                f"</system-reminder>")
            print(f"  \033[32m[background done] {bg_id}: "
                  f"{task['command'][:40]} ({len(output)} chars)\033[0m")
        return notifications
