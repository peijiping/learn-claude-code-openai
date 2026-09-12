#!/usr/bin/env python3
"""
logger.py - 统一日志模块

全项目唯一的日志入口：按日期落盘到 ~/.aigent/logs/agent_YYYY-MM-DD.log，
跨天自动切换新文件（长驻进程无需重启）。

用法（任何模块两行接入）：

    from logger import get_logger
    log = get_logger("agent")          # 名字用于区分来源（ws_bridge / runtime / agent...）
    log.info("session_%s created", num)

设计要点：
- 文件命名固定 agent_ 前缀（后端单文件流，方便按天排查）；
- 级别经 LOG_LEVEL 控制（~/.aigent/config.json / 环境变量，默认 INFO）；
- 不打印到 stdout/stderr：CLI/桌面端 stdout 是交互输出通道，
  print 已承担控制台职责，日志走文件互不干扰；
- logging 模块自带 handler 锁，多线程（run_turn 工作线程 / 后台子智能体 /
  标题线程）并发写安全；
- install_excepthooks() 兜底捕获未处理异常（含线程内异常），
  进程崩溃时日志里有完整 traceback，不再依赖 stdout 捞 print。
"""

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from paths import LOG_DIR

# 日志级别（可调参数：~/.aigent/config.json 或环境变量 LOG_LEVEL）
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# 日志文件前缀（后端统一 agent_；前端 Electron 侧另有 frontend_ 前缀）
LOG_PREFIX = "agent"

_FORMATTER = logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class DailyFileHandler(logging.Handler):
    """按日期写文件的 handler：文件名 {prefix}_YYYY-MM-DD.log，跨天自动切换。

    每次 emit 时检查当前日期（本地时区），与已打开文件不一致则切换新文件；
    append 模式打开，进程重启当天续写同一文件。emit 由 logging 框架持锁调用，
    这里的日期检查/文件切换是线程安全的。
    """

    def __init__(self, log_dir: Path, prefix: str = LOG_PREFIX):
        super().__init__()
        self._dir = Path(log_dir)
        self._prefix = prefix
        self._date = ""
        self._stream = None

    def _ensure_stream(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._stream is not None and today == self._date:
            return
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        self._dir.mkdir(parents=True, exist_ok=True)
        self._stream = open(
            self._dir / f"{self._prefix}_{today}.log", "a", encoding="utf-8"
        )
        self._date = today

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            self._stream.write(self.format(record) + "\n")
            # 必须显式 flush：进程被 SIGTERM/SIGKILL 时解释器不走退出清理，
            # Python 文件缓冲不落盘日志就丢了（历史 bug：曾误调基类
            # Handler.flush()，它访问不存在的 self.stream 属性抛
            # AttributeError，被 except 吞掉后表现为"日志随机丢失"）。
            self._stream.flush()
        except Exception:  # noqa: BLE001 - 日志绝不能反向打断业务
            self.handleError(record)

    def flush(self) -> None:
        """覆写基类：流在 self._stream（基类 flush 访问 self.stream 会 AttributeError）。"""
        if self._stream is not None:
            try:
                self._stream.flush()
            except OSError:
                pass

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        super().close()


_root_configured = False


class _NameStripFilter(logging.Filter):
    """把记录名里的 "aigent." 层级前缀去掉，日志行只显示模块短名。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("aigent."):
            record.name = record.name[len("aigent."):]
        return True


def _ensure_root() -> None:
    """初始化 "aigent" 根 logger（全项目共享同一个 handler，进程内只配一次）。"""
    global _root_configured
    if _root_configured:
        return
    root = logging.getLogger("aigent")
    root.setLevel(LOG_LEVEL)
    handler = DailyFileHandler(LOG_DIR)
    handler.setFormatter(_FORMATTER)
    handler.addFilter(_NameStripFilter())
    root.addHandler(handler)
    root.propagate = False  # 不向 root logger 冒泡（避免与第三方库的 handler 重复输出）
    _root_configured = True


def get_logger(name: str = "agent") -> logging.Logger:
    """获取命名子 logger：aigent.<name>，输出到 ~/.aigent/logs/agent_日期.log。"""
    _ensure_root()
    return logging.getLogger(f"aigent.{name}")


def install_excepthooks() -> None:
    """兜底捕获未处理异常（主线程 + 子线程），写入日志文件。

    在进程入口（ws_bridge / agent_cli 的 __main__）调用一次。
    """
    log = get_logger("crash")

    def _sys_hook(exc_type, exc_value, exc_tb):
        log.error(
            "主线程未捕获异常:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args: threading.ExceptHookArgs):
        thread_name = args.thread.name if args.thread else "?"
        log.error(
            "线程 %s 未捕获异常:\n%s",
            thread_name,
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
