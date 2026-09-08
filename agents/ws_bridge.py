"""
ws_bridge.py - 桌面端桥层（新增，不修改 agent_full_v2.py）
把 Agent 变成 WS service：命令进、事件出（走 WSSink 的 JSON 行协议）。

协议见 docs/frontend/03-前后端通信协议.md。Electron 主进程拉起本脚本，
连 ws://127.0.0.1:<AGENT_WS_PORT>（默认 8765）。

与后端铁律一致：只新增这一个薄层，agent_full_v2.py 及以下零改动。
"""
import asyncio
import json
import os

import websockets

from agent_full_v2 import Agent
from config import load as load_config
from llm_config import get_config, load_llm_config, save_config
from paths import CHAT_HISTORY_DIR
from session_manage import SessionManager
from streaming_client import WSSink

# 启动即自举配置（Electron spawn 的 cwd 为仓库根，config.py 按 cwd 解析项目级配置）
load_config()
# 存在 llmconfig.json 则加载大模型配置映射进 env（文件缺失时不影响启动）
load_llm_config()

PORT = int(os.environ.get("AGENT_WS_PORT", "8765"))

# 全局单 Agent（桌面端同一后端实例），静音避免打印干扰 UI。
# 惰性会话：启动不建会话（避免每次打开窗口都多一个空 jsonl），
# 首条 chat 消息或用户切换会话时才产生/加载会话。
agent = Agent(silent=True)


def _envelope(kind: str, payload: dict) -> str:
    return json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)


def _session_meta(item):
    """session_manager.list_sessions() 的条目：tuple[int, Path, int]"""
    num, path, msg_count = item
    return {"num": num, "message_count": msg_count, "file": path.name}


def _ensure_session_manager():
    """惰性会话下 session_manager 可能为 None（尚未 init/switch），
    列会话等只读操作前先兜底构建（构建后 init_session 也会复用）。"""
    if agent.session_manager is None:
        agent.session_manager = SessionManager(
            CHAT_HISTORY_DIR, agent.system_prompt.build_system_prompt(),
            session_prefix=agent.session_prefix,
        )
    return agent.session_manager


def _text_of(content) -> str:
    """历史消息 content 兼容转换：str 直接返回，list（多模态 blocks）拼接 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def _history_to_ui(messages: list) -> list[dict]:
    """session 历史 → 前端可渲染消息列表。

    - 跳过 system / tool 消息（前者无展示价值，后者已聚合进 assistant 工具条）
    - 跳过系统注入的 user 消息（<system-reminder> 开头的 todo reminder 等）
    - assistant 保留 reasoning_content → thinking、tool_calls → 工具条
    """
    ui = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = _text_of(m.get("content"))
            if content.startswith("<system-reminder>"):
                continue
            ui.append({"role": "user", "content": content})
        elif role == "assistant":
            ui.append({
                "role": "assistant",
                "content": _text_of(m.get("content")),
                "thinking": m.get("reasoning_content") or "",
                "toolCalls": [
                    {
                        "name": (tc.get("function") or {}).get("name", ""),
                        "args": (tc.get("function") or {}).get("arguments", ""),
                    }
                    for tc in (m.get("tool_calls") or [])
                ],
            })
    return ui


async def handle(ws):
    line_q: asyncio.Queue = asyncio.Queue()

    # WSSink 的 send_func 是同步回调：往 asyncio 队列塞，writer 协程异步发送。
    # 这样 agent.run_turn 跑在别的线程时，事件仍能被事件循环逐条 flush。
    def send_func(line: str):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"type": "unknown", "text": line}
        line_q.put_nowait(_envelope("event", payload))

    agent.stream_sink = WSSink(send_func=send_func)

    async def writer():
        while True:
            line = await line_q.get()
            await ws.send(line)

    writer_task = asyncio.create_task(writer())
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(_envelope("error", {"msg": f"bad json: {raw}"}))
                continue
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            if kind == "chat":
                # 惰性会话：前端"新建任务"只是前端态（activeSession=null），
                # 首条消息带 fresh=true；后端无激活会话时也强制新建。
                # 先建会话并回发 session 信封（前端尽早拿到新会话号），
                # 再跑 run_turn（阻塞调用丢线程池，让 writer 持续吐流式事件）。
                if payload.get("fresh") or agent.session_num is None:
                    await asyncio.to_thread(agent.init_session, resume=False)
                    await ws.send(_envelope("session", {
                        "num": agent.session_num,
                        "message_count": 0,
                    }))
                    await _reply_sessions(ws)
                await asyncio.to_thread(agent.run_turn, payload.get("text", ""))
                # 一轮结束后回发列表，让新会话/计数即时可见
                await _reply_sessions(ws)

            elif kind == "session_switch":
                try:
                    await asyncio.to_thread(agent.switch_session, int(payload.get("num", 0)))
                except FileNotFoundError:
                    await ws.send(_envelope("error", {"msg": f"session {payload.get('num')} not found"}))
                else:
                    # 切换成功：把该会话历史消息回放给前端渲染（会话列表同刷）
                    await ws.send(_envelope("session_history", {
                        "num": agent.session_num,
                        "messages": _history_to_ui(agent.history_messages),
                    }))
                    await _reply_sessions(ws)

            elif kind == "session_clear":
                if agent.session_num is None:
                    await ws.send(_envelope("error", {"msg": "当前无激活会话"}))
                    continue
                deleted = await asyncio.to_thread(agent.clear_session)
                await ws.send(_envelope("session", {
                    "num": agent.session_num,
                    "message_count": len(agent.history_messages),
                }))
                await _reply_sessions(ws)
                await ws.send(_envelope("error", {"msg": f"cleared {deleted} messages"}))

            elif kind == "sessions_list":
                await _reply_sessions(ws)

            elif kind == "goal_status":
                text = await asyncio.to_thread(agent.goal_status)
                await ws.send(_envelope("goal_status", {"text": text}))

            elif kind == "tasks":
                # 惰性会话下可能尚未绑定 todo manager，无激活会话时给占位文本
                if agent.session_num is None:
                    await ws.send(_envelope("tasks", {"text": "(当前会话暂无待办)"}))
                    continue
                text = await asyncio.to_thread(agent.show_tasks)
                if not text.strip():
                    text = "(当前会话暂无待办)"
                await ws.send(_envelope("tasks", {"text": text}))

            elif kind == "skills":
                text = await asyncio.to_thread(agent.skills.list_skills)
                await ws.send(_envelope("skills", {"text": text}))

            elif kind == "llm_config_get":
                await ws.send(_envelope("llm_config", {"config": get_config()}))

            elif kind == "llm_config_save":
                config = payload.get("config") or {}
                try:
                    saved = await asyncio.to_thread(save_config, config)
                    # 重新映射进 env 并就地热切换 LLM 绑定，立即生效（无需重启）
                    await asyncio.to_thread(load_llm_config)
                    result = await asyncio.to_thread(agent.reload_llm_bindings)
                except ValueError as exc:
                    await ws.send(_envelope("error", {"msg": f"保存失败：{exc}"}))
                else:
                    ok = result.get("applied", False)
                    await ws.send(_envelope("llm_config", {
                        "config": get_config(),
                        "applied": ok,
                        "msg": (f"模型配置已生效（{result.get('primary')}）"
                                if ok else result.get("reason", "未生效")),
                    }))

            else:
                await ws.send(_envelope("error", {"msg": f"unknown kind: {kind}"}))
    finally:
        writer_task.cancel()


async def _reply_sessions(ws):
    sm = await asyncio.to_thread(_ensure_session_manager)
    items = await asyncio.to_thread(sm.list_sessions)
    sessions = [_session_meta(i) for i in items]
    await ws.send(_envelope("sessions", {"sessions": sessions}))


async def main():
    async with websockets.serve(handle, "127.0.0.1", PORT):
        print(f"[ws_bridge] WS server listening on 127.0.0.1:{PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())