#!/usr/bin/env python3
"""
subagent_store.py - 子智能体执行过程的旁路记录（sidecar）存储

## 为什么需要它

子智能体执行过程（思考 + 工具调用）原先以 `role=subagent` 行**混写**在主会话
文件 `session_N.jsonl` 里，与标准 OpenAI 消息共用同一个 append 流。这带来两个
无法在"就地修补"框架下消除的问题：

1. **写入位置错误会静默删除整轮对话**：同步子智能体在工具执行阶段就落盘，
   磁盘顺序变成 `assistant(tool_calls)` → `subagent` → `tool`，打断加载时
   `_sanitize_orphan_tool_calls` 的 tool 块连续性判定 → assistant 与 tool 双双
   被判为孤儿并**重写文件**（不可逆）。
2. **结构性耦合**：该行与拼行修复、自愈重写、上下文压缩、异常恢复四处机制
   互相踩，任何一处改动都会破坏另外几处。

因此把子智能体记录**物理隔离**到旁路文件：主会话文件从此只写标准消息。

## 文件约定

- 路径：`<chat_history_dir>/session_N.subagents.jsonl`（与主文件同目录同前缀）
- 格式：append-only，每行一条 JSON 记录
- 读取：按 `subagent_id` 分组，**取每组最后一条**（后写覆盖先写）→ "起始占位行
  + 结束完整行"天然合并为最终态；进程被强杀时只剩占位行，状态为 `running`

## 记录字段（v1）

```json
{"v": 1, "subagent_id": "sub_xxx", "tool_call_id": "call_xxx", "session_num": 6,
 "name": "任务名", "status": "running|done|error|aborted", "source": "sync|background",
 "prompt": "……", "started_at": "2026-09-11T09:31:02", "ended_at": null,
 "duration_ms": null, "thinking": "", "text": "", "toolCalls": [], "error": ""}
```

设计依据见 `docs/frontend/08-子智能体执行过程持久化与回放改造方案.md`。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from logger import get_logger

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("subagent")

# 旁路文件后缀：session_6.jsonl → session_6.subagents.jsonl
SIDECAR_SUFFIX = ".subagents.jsonl"


def _now_iso() -> str:
    """本地时间秒级 isoformat（单机桌面产品，无时区换算需求）。"""
    return datetime.now().isoformat(timespec="seconds")


def _read_json_objects(path: Path) -> list[dict]:
    """容错读取 JSONL：一行可能因进程中断而拼了多个 JSON，用 raw_decode 全部解出。

    与 `session_manage.load_session_history` 使用同一算法，保证迁移前后对
    同一份文件的"行"理解一致。无法解析的尾部残片会被丢弃（与原逻辑一致）。
    """
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    idx, n = 0, len(content)
    while idx < n:
        while idx < n and content[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(content, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            objects.append(obj)
        idx = end
    return objects


class SubagentStore:
    """子智能体执行过程的旁路记录存储（一个会话目录对应一个实例）。"""

    def __init__(self, chat_history_dir: Path):
        self.chat_history_dir = Path(chat_history_dir)

    # ── 路径推导 ────────────────────────────────────────────────
    @staticmethod
    def sidecar_path(session_file: Path) -> Path:
        """由主会话文件推出旁路文件路径（session_6.jsonl → session_6.subagents.jsonl）。"""
        session_file = Path(session_file)
        return session_file.with_name(session_file.stem + SIDECAR_SUFFIX)

    @staticmethod
    def sidecar_exists(self_unused, session_file: Path) -> bool:  # pragma: no cover - 兼容占位
        return SubagentStore.sidecar_path(session_file).exists()

    @staticmethod
    def _session_num(session_file: Path) -> Optional[int]:
        try:
            return int(Path(session_file).stem.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            return None

    # ── 写入 ───────────────────────────────────────────────────
    def _append_row(self, session_file: Path, row: dict) -> None:
        path = self.sidecar_path(session_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def begin(self, session_file: Path, subagent_id: str, tool_call_id: str = "",
              name: str = "", prompt: str = "", source: str = "sync") -> None:
        """子智能体启动：写一条 `status=running` 占位行（崩溃留痕 + 实时卡片立即可见）。

        进程被强杀时不会再有终态行，加载即得到 running 状态，卡片显示"已中断"。
        """
        self._append_row(session_file, {
            "v": 1,
            "subagent_id": subagent_id,
            "tool_call_id": tool_call_id,
            "session_num": self._session_num(session_file),
            "name": name,
            "status": "running",
            "source": source,
            "prompt": prompt,
            "started_at": _now_iso(),
            "ended_at": None,
            "duration_ms": None,
            "thinking": "",
            "text": "",
            "toolCalls": [],
            "error": "",
        })

    def append(self, session_file: Path, transcript: dict) -> None:
        """子智能体结束：写完整终态行（同 subagent_id 的后写覆盖先写）。

        transcript 由 `subagent.SubAgent.spawn_subagent` 产出，已含
        subagent_id / name / thinking / toolCalls / error / 可选
        tool_call_id / text / started_at / duration_ms。
        """
        error = transcript.get("error") or ""
        status = transcript.get("status") or ("error" if error else "done")
        self._append_row(session_file, {
            "v": 1,
            "subagent_id": transcript.get("subagent_id", ""),
            "tool_call_id": transcript.get("tool_call_id", ""),
            "session_num": self._session_num(session_file),
            "name": transcript.get("name", ""),
            "status": status,
            "source": transcript.get("source", "sync"),
            "prompt": transcript.get("prompt", ""),
            "started_at": transcript.get("started_at") or _now_iso(),
            "ended_at": _now_iso(),
            "duration_ms": transcript.get("duration_ms"),
            "thinking": transcript.get("thinking", ""),
            "text": transcript.get("text", ""),
            "toolCalls": transcript.get("toolCalls", []),
            "error": error,
        })

    # ── 读取 ───────────────────────────────────────────────────
    def load(self, session_file: Path) -> list[dict]:
        """读取旁路记录：按 subagent_id 分组取最后一条，保持首次出现顺序。"""
        rows = [r for r in _read_json_objects(self.sidecar_path(session_file))
                if r.get("subagent_id")]
        merged: dict[str, dict] = {}
        for row in rows:
            merged[row["subagent_id"]] = row  # 后写覆盖先写
        return list(merged.values())

    # ── 生命周期 ───────────────────────────────────────────────
    def clear(self, session_file: Path) -> None:
        """清空会话时删除旁路文件（与主 jsonl 同生共死）。"""
        self.delete(session_file)

    def delete(self, session_file: Path) -> None:
        path = self.sidecar_path(session_file)
        try:
            if path.exists():
                path.unlink()
        except OSError as e:  # pragma: no cover - 文件占用等极端情况
            log.error("删除子智能体记录文件失败: %s", e)

    # ── 旧数据迁移 ─────────────────────────────────────────────
    def migrate(self, session_file: Path) -> int:
        """把主 jsonl 里遗留的 `role=subagent` 行抽出到旁路文件，并净化主文件。

        幂等：主文件已无 subagent 行时直接返回 0（不会重复迁移，也不会覆盖
        sidecar 里更新的记录）。首次迁移前留一份 `session_N.jsonl.bak` 快照，
        使"迁移误伤"可回滚。

        Returns:
            迁移的 subagent 行数（0 表示无需迁移）。
        """
        session_file = Path(session_file)
        if not session_file.exists():
            return 0
        objects = _read_json_objects(session_file)
        if not objects:
            return 0
        legacy = [o for o in objects if o.get("role") == "subagent"]
        if not legacy:
            return 0

        # 快照：仅首次（存在于最初形态），不覆盖历史备份
        backup = session_file.with_name(session_file.name + ".bak")
        try:
            if not backup.exists():
                backup.write_text(
                    "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objects),
                    encoding="utf-8",
                )
        except OSError as e:  # pragma: no cover
            log.error("写入会话备份失败（继续迁移）: %s", e)

        # 旧行写入旁路（已存在同 id 记录则跳过：sidecar 视为主源，数据更新）
        existing_ids = {r.get("subagent_id") for r in self.load(session_file)}
        moved = 0
        for row in legacy:
            sid = row.get("subagent_id", "")
            if not sid or sid in existing_ids:
                continue
            # 工具状态归一化：旧行是子智能体**结束时的快照**，里面残留的
            # `running` 是旧版配对 bug（按 tool_calls[-1] 闭合）的产物。
            # 直接迁过来会让卡片永久转圈 → 一律修正为 done。
            tools = []
            for t in (row.get("toolCalls") or []):
                item = dict(t) if isinstance(t, dict) else {"name": str(t)}
                item["status"] = "done"
                tools.append(item)
            self._append_row(session_file, {
                "v": 1,
                "subagent_id": sid,
                "tool_call_id": row.get("tool_call_id", ""),
                "session_num": self._session_num(session_file),
                "name": row.get("name", ""),
                "status": "done",
                "source": "migrated",
                "prompt": "",
                "started_at": None,
                "ended_at": None,
                "duration_ms": None,
                "thinking": row.get("thinking", ""),
                "text": "",
                "toolCalls": tools,
                "error": row.get("error", ""),
            })
            existing_ids.add(sid)
            moved += 1

        # 主文件重写为纯标准消息（保持原 dict 逐行、原顺序）
        cleaned = [o for o in objects if o.get("role") != "subagent"]
        tmp = session_file.with_suffix(session_file.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for obj in cleaned:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            tmp.replace(session_file)
        except Exception as e:  # pragma: no cover
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            log.error("迁移子智能体记录失败（主文件保持原样）: %s", e)
            return 0
        log.warning("[子智能体记录迁移] %s: 抽出 %d 行到旁路文件（主文件已净化为纯标准消息）",
                    session_file.name, len(legacy))
        return len(legacy)
