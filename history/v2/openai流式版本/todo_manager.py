#!/usr/bin/env python3
"""
todo_manager.py - 单会话待办事项管理

会话级轻量级计划板：数据持久化为单个 JSON 文件。
对标 s05 课程的 todo_write 工具：单列表、3 态、刷新式更新。
"""

import json
from pathlib import Path


# -- TodoManager: 单列表 + 落盘 + 严格状态校验 --
class TodoManager:
    """
    待办事项管理器。

    每个会话对应一个 JSON 文件。模型通过 update(items, fresh_start) 维护列表：
    - 默认模式：整体替换当前列表
    - fresh_start=True：开始新计划，先丢弃当前已完成的项，再整体替换
    """

    MAX_ITEMS = 20
    FILE_VERSION = 1

    def __init__(self, todo_file: Path):
        self.todo_file = todo_file
        self.todo: list[dict] = []
        self.load()

    def load(self) -> list:
        """从磁盘加载当前会话待办列表。"""
        self.todo_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.todo_file.exists():
            self.todo = []
            return self.todo

        payload = json.loads(self.todo_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != self.FILE_VERSION:
            raise ValueError(f"Unsupported todo file format: {self.todo_file}")

        items = payload.get("items", [])
        if not isinstance(items, list):
            raise ValueError(f"Invalid todo file format: {self.todo_file}")

        self.todo = self._validate_items(items)
        return self.todo

    def _save(self) -> None:
        """把当前待办列表落盘。"""
        self.todo_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.FILE_VERSION, "items": self.todo}
        self.todo_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _validate_items(self, items: list) -> list:
        """
        校验并规范化待办列表。

        约束：
          - 总数不超过 MAX_ITEMS
          - text 非空
          - status 必须是 pending / in_progress / completed
          - 同一时刻最多 1 个 in_progress（避免模型贪心并行）
        """
        if len(items) > self.MAX_ITEMS:
            raise ValueError(f"Max {self.MAX_ITEMS} todos allowed")

        validated: list[dict] = []
        in_progress_count = 0
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"Item {i + 1}: must be an object")
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})

        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        return validated

    def update(self, items: list, fresh_start: bool = False) -> str:
        """
        更新待办列表（整体替换语义）。

        参数:
            items: 新的完整待办列表
            fresh_start: True 时表示开始新计划——先清掉当前已完成的项，
                        再用 items 替换整个列表。返回消息会提示清理数量。

        返回:
            渲染后的待办看板。
        """
        cleared = 0
        if fresh_start and self.todo:
            cleared = sum(1 for i in self.todo if i["status"] == "completed")

        self.todo = self._validate_items(items)
        self._save()

        if fresh_start and cleared:
            return f"[新计划开始，已清理 {cleared} 个已完成任务]\n{self.render()}"
        return self.render()

    def has_open_items(self) -> bool:
        """判断当前会话是否存在未完成的待办项。"""
        return any(i["status"] != "completed" for i in self.todo)

    def render(self) -> str:
        """把当前待办列表渲染为可读字符串。"""
        if not self.todo:
            return "No todos."

        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        done = sum(1 for i in self.todo if i["status"] == "completed")
        total = len(self.todo)
        lines = [f"Todos ({done}/{total} completed):"]
        for item in self.todo:
            lines.append(f"  {marker[item['status']]} #{item['id']}: {item['text']}")
        return "\n".join(lines)
