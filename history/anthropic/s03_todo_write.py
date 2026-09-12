#!/usr/bin/env python3
"""
s03_todo_write.py - 待办事项管理智能体

本程序实现了一个具有待办事项追踪功能的 AI 智能体。模型通过 TodoManager 来跟踪自己的任务进度，
当智能体忘记更新待办事项时，系统会注入一个提醒来强制它保持更新。

系统架构:
    +----------+      +-------+      +---------+
    |   用户   | ---> |  LLM  | ---> |  工具   |
    |  输入    |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   工具结果     |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager 状态      |
                    | [ ] 任务 A            |
                    | [>] 任务 B <- 进行中   |
                    | [x] 任务 C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                      注入 <提醒>

核心洞察: "智能体可以跟踪自己的进度 -- 并且我能看到它。"
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载环境变量，override=True 表示覆盖已存在的环境变量
load_dotenv(override=True)

# 如果设置了 ANTHROPIC_BASE_URL，则移除 ANTHROPIC_AUTH_TOKEN
# 这通常用于兼容某些代理服务
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 设置工作目录为当前目录
WORKDIR = Path.cwd()
# 初始化 Anthropic 客户端，使用环境变量中的基础 URL
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量获取模型 ID
MODEL = os.environ["MODEL_ID"]

# 系统提示词，指导 LLM 如何使用工具
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""


# -- TodoManager: LLM 写入的结构化状态管理器 --
class TodoManager:
    """
    待办事项管理器类
    
    负责管理任务列表的状态，包括添加、更新和渲染任务。
    支持三种状态：pending(待处理)、in_progress(进行中)、completed(已完成)
    """
    
    def __init__(self):
        """初始化空的任务列表"""
        self.items = []

    def update(self, items: list) -> str:
        """
        更新待办事项列表
        
        参数:
            items: 任务列表，每个任务是一个包含 id、text、status 的字典
            
        返回:
            渲染后的任务列表字符串
            
        异常:
            ValueError: 当任务数量超过20、任务文本为空、状态无效或同时有多个进行中任务时
        """
        # 限制最大任务数量为20
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        
        validated = []
        in_progress_count = 0
        
        # 遍历并验证每个任务项
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            
            # 验证任务文本不为空
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            
            # 验证状态值有效
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            
            # 统计进行中任务数量
            if status == "in_progress":
                in_progress_count += 1
            
            validated.append({"id": item_id, "text": text, "status": status})
        
        # 确保只有一个进行中的任务
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        
        self.items = validated
        return self.render()

    def render(self) -> str:
        """
        渲染待办事项列表为可读字符串
        
        返回:
            格式化的任务列表字符串，包含进度统计
        """
        if not self.items:
            return "No todos."
        
        lines = []
        # 状态标记映射
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        
        for item in self.items:
            lines.append(f"{marker[item['status']]} #{item['id']}: {item['text']}")
        
        # 统计已完成任务数量
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        
        return "\n".join(lines)


# 创建全局 TodoManager 实例
TODO = TodoManager()


# -- 工具函数实现 --
def safe_path(p: str) -> Path:
    """
    安全路径解析函数
    
    将相对路径解析为绝对路径，并确保路径不会逃逸出工作目录
    （防止目录遍历攻击）
    
    参数:
        p: 输入的路径字符串
        
    返回:
        解析后的 Path 对象
        
    异常:
        ValueError: 当路径尝试逃逸出工作目录时
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """
    执行 Bash 命令
    
    参数:
        command: 要执行的 shell 命令
        
    返回:
        命令输出（stdout + stderr），最多返回 50000 字符
        
    安全特性:
        - 阻止危险命令（如 rm -rf /、sudo 等）
        - 120秒超时限制
    """
    # 危险命令黑名单
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    """
    读取文件内容
    
    参数:
        path: 文件路径
        limit: 可选，限制读取的最大行数
        
    返回:
        文件内容字符串，最多返回 50000 字符
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """
    写入文件内容
    
    参数:
        path: 文件路径
        content: 要写入的内容
        
    返回:
        操作结果信息
        
    特性:
        - 自动创建父目录
        - 覆盖已存在的文件
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    编辑文件内容（文本替换）
    
    参数:
        path: 文件路径
        old_text: 要替换的旧文本
        new_text: 新文本
        
    返回:
        操作结果信息
        
    注意:
        只替换第一个匹配的文本
    """
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 工具处理函数字典，将工具名称映射到对应的处理函数
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}

# 工具定义列表，用于向 LLM 描述可用的工具及其参数
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"}
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "todo",
        "description": "Update task list. Track progress on multi-step tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
                        },
                        "required": ["id", "text", "status"]
                    }
                }
            },
            "required": ["items"]
        }
    },
]


# -- 带有提醒注入的智能体循环 --
def agent_loop(messages: list):
    """
    智能体主循环
    
    与 LLM 进行交互循环，处理工具调用，并在必要时注入待办事项更新提醒
    
    参数:
        messages: 对话历史消息列表
        
    提醒机制:
        如果连续 3 轮对话没有更新待办事项，系统会注入提醒消息
    """
    rounds_since_todo = 0  # 记录距离上次更新待办事项的轮数
    
    while True:
        # 调用 LLM API
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        
        # 将 LLM 响应添加到对话历史
        messages.append({"role": "assistant", "content": response.content})
        
        # 如果 LLM 没有调用工具，则结束循环
        if response.stop_reason != "tool_use":
            return
        
        results = []
        used_todo = False
        
        # 处理所有工具调用
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                
                # 打印工具调用结果（截断显示）
                print(f"> {block.name}: {str(output)[:200]}")
                
                # 添加工具结果到结果列表
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)
                })
                
                # 检查是否使用了 todo 工具
                if block.name == "todo":
                    used_todo = True
        
        # 更新待办事项计数器
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        
        # 如果连续 3 轮没有更新待办事项，注入提醒
        if rounds_since_todo >= 3:
            results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        
        # 将结果添加到对话历史
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 主程序入口
    history = []
    
    while True:
        try:
            # 显示青色提示符 "s03 >> " 并读取用户输入
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # 处理 Ctrl+D 或 Ctrl+C
            break
        
        # 退出命令
        if query.strip().lower() in ("q", "exit", ""):
            break
        
        # 添加用户输入到对话历史
        history.append({"role": "user", "content": query})
        
        # 运行智能体循环
        agent_loop(history)
        
        # 获取并显示 LLM 的最终响应
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
