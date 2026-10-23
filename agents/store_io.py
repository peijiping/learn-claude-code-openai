#!/usr/bin/env python3
"""
store_io.py - 设置页系 store 的原子读写小工具（mcp / skill / plugin 共用）

为什么要独立成模块：三个 store 的**错误语义必须逐字一致**，而这份语义有两处
反直觉的地方 —— 抄三遍必然漂移，漂移的代价是"把用户手写的配置抹掉"。

错误语义（与 `mcp_store` 完全同源，改之前先读 `mcp_store` 模块 docstring）：

- **读（strict）**：文件不存在 → 返回 `default`（默认 `{}`）；
  **存在但损坏 → 抛 `ValueError`**，绝不吞成空值 —— 否则调用方会拿空值回写，
  把磁盘上还在的内容整份抹掉。UI 侧要据此区分「配置读取失败」与「还没有配置」。
- **读（lenient）**：坏文件降级成 `default` 并只记一条 warning。用于**旁路元数据**
  （来源标记、启停状态这类"丢了也不致命"的信息）—— 不值得为它拦下整个设置页。
- **写**：`tmp + os.replace` 原子写 + `chmod 0o600`。可能含私有市场的访问令牌，
  也可能含用户手写的密钥，一律按最严的权限落盘。

⚠️ 这里**不 import** `mcp_store` / `paths` 之外的任何业务模块：三个 store 都可能
在 Agent 构造之前被设置页加载（同 `mcp_store._prefix_key` 的注释），导入链必须保持
只有 `logger` + 标准库。
"""

import json
import os
import threading
from pathlib import Path
from typing import Any

from logger import get_logger

log = get_logger("store_io")

# 同一进程内并发写同一文件的串行化（多窗口同开设置页时会发生）。
# 跨进程不加锁 —— 与 mcp_store 同策略：原子替换已保证"不会读到半个文件"，
# 而跨进程的读-改-写竞争在本应用的单实例模型下不存在。
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def read_json_strict(path: Path | str, default: Any = None,
                     expect: type | tuple | None = dict) -> Any:
    """严格读：不存在 → `default`；损坏 / 类型不符 → `ValueError`。

    `expect` 传 `None` 表示不校验顶层类型。
    """
    p = Path(path)
    fallback = {} if default is None else default
    if not p.exists():
        return fallback
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"读取失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败（第 {exc.lineno} 行）：{exc.msg}") from exc
    if expect is not None and not isinstance(raw, expect):
        raise ValueError(f"顶层结构异常（期望 {getattr(expect, '__name__', expect)}）")
    return raw


def read_json_lenient(path: Path | str, default: Any = None,
                      label: str = "") -> Any:
    """宽容读（旁路元数据专用）：任何问题都降级成 `default` + 一条 warning。"""
    p = Path(path)
    fallback = {} if default is None else default
    if not p.exists():
        return fallback
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("%s 无法解析，降级为空：%s", label or p.name, exc)
        return fallback
    return raw


def write_json_atomic(path: Path | str, data: Any, mode: int = 0o600) -> None:
    """原子写：临时文件 + `os.replace` + 权限收紧。失败抛 `OSError`。"""
    p = Path(path)
    with _lock_for(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            try:
                os.chmod(tmp, mode)
            except OSError as exc:  # noqa: PERF203 - 平台不支持 chmod 不该拦下落盘
                log.warning("%s chmod 失败（不阻断写入）：%s", p.name, exc)
            os.replace(tmp, p)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            log.error("%s 写入失败: %s", p.name, exc)
            raise


def is_enabled(value) -> bool:
    """`enable` / `enabled` 字段的启用判定。

    与 `mcp_store.is_enabled` **逐字一致**：`load_config` 判定的语义是"禁用 =
    `value in (0, "0", False, "false")`"，这里取反。两处口径必须同源，否则会出现
    「页面显示已启用、实际没生效」。**缺省（字段不存在）= 启用**。
    """
    return value not in (0, "0", False, "false")
