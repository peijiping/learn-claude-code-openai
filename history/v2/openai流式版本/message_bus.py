
import os
import subprocess
import time
from pathlib import Path
import json




# 预定义的有效消息类型集合
# 用于验证消息总线中传输的消息类型是否合法
VALID_MSG_TYPES = {
    "message",               # 普通文本消息，点对点发送
    "broadcast",             # 广播消息，发送给所有团队成员
    "shutdown_request",     # 关闭请求，请求目标团队成员优雅关闭
    "shutdown_response",     # 关闭响应，目标对关闭请求的批准/拒绝回复
    "plan_approval_request", # 计划审批请求，队友向 Lead 提交计划等待审批（s16/s17）
    "plan_approval_response", # 计划审批响应，对计划请求的批准/拒绝回复
    "result",                # 工作结果，队友完成后发给 Lead 的总结（s17）
}


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    """
    消息总线类，负责团队成员之间的消息传递

    设计理念：
    - 每个团队成员拥有独立的 JSONL 收件箱文件（./inbox/{name}.jsonl）
    - 消息以追加模式写入（append-only），保证消息不丢失
    - 读取收件箱后自动清空文件，实现"消费"语义

    消息格式（JSON对象）：
    {
        "type": str,         # 消息类型，取值自 VALID_MSG_TYPES
        "from": str,         # 发送者名称
        "content": str,      # 消息内容
        "timestamp": float,  # 时间戳（从 epoch 开始的秒数）
        ...extra             # 可选的扩展字段
    }
    """

    def __init__(self, inbox_dir: Path):
        """
        初始化消息总线

        参数:
            inbox_dir: 收件箱目录路径，所有成员的收件箱文件将存放于此
        """
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)  # 确保目录存在

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        """
        向指定团队成员发送消息

        参数:
            sender: 发送者名称
            to: 接收者名称（目标团队成员）
            content: 消息内容
            msg_type: 消息类型，默认为 "message"（普通文本消息）
            extra: 可选的扩展字段字典，会合并到消息对象中

        返回:
            str: 操作结果字符串，"Sent {msg_type} to {to}" 或错误信息

        注意:
            - 消息类型必须为 VALID_MSG_TYPES 中的有效值
            - 消息以 JSONL 格式追加到接收者的收件箱文件
        """
        # 验证消息类型是否合法
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"

        # 构造消息对象
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),  # 记录发送时间戳
        }
        # 合并可选的扩展字段
        if extra:
            msg.update(extra)

        # 将消息追加写入接收者的收件箱文件
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """
        读取并清空指定团队成员的收件箱

        参数:
            name: 团队成员名称

        返回:
            list: 消息对象列表，如果收件箱为空或不存在则返回空列表

        注意:
            - 读取后收件箱文件会被清空，实现"消费"语义
            - 这是一种"排他性读取"，消息只会被一个消费者处理
        """
        inbox_path = self.dir / f"{name}.jsonl"

        # 如果收件箱文件不存在，返回空列表
        if not inbox_path.exists():
            return []

        # 读取所有消息行并解析为 JSON 对象
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))

        # 清空收件箱文件
        inbox_path.write_text("")

        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        """
        向所有团队成员广播消息

        参数:
            sender: 发送者名称
            content: 消息内容
            teammates: 团队成员名称列表

        返回:
            str: 广播结果字符串，格式为 "Broadcast to {count} teammates"

        注意:
            - 广播消息使用 "broadcast" 类型
            - 发送者不会收到自己发送的广播消息
        """
        count = 0
        for name in teammates:
            # 排除发送者自己
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"
