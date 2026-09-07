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
from streaming_client import WSSink

# 启动即自举配置（Electron spawn 的 cwd 为仓库根，config.py 按 cwd 解析项目级配置）
load_config()

PORT = int(os.environ.get("AGENT_WS_PORT", "8765"))

# 全局单 Agent（桌面端同一后端实例），静音避免打印干扰 UI
agent = Agent(silent=True)
agent.init_session(resume=False)


def _envelope(kind: str, payload: dict) -> str:
    return json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)


def _session_meta(item):
    """session_manager.list_sessions() 的条目：tuple[int, Path, int]"""
    num, path, msg_count = item
    return {"num": num, "message_count": msg_count, "file": path.name}


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
                # run_turn 是阻塞的（agent_loop 同步 LLM 调用）→ 丢到线程池，
                # 让事件循环继续运行，writer 才能把流式事件发出去。
                await asyncio.to_thread(agent.run_turn, payload.get("text", ""))

            elif kind == "session_new":
                await asyncio.to_thread(agent.new_session)
                await _reply_sessions(ws)

            elif kind == "session_switch":
                try:
                    await asyncio.to_thread(agent.switch_session, int(payload.get("num", 0)))
                except FileNotFoundError:
                    await ws.send(_envelope("error", {"msg": f"session {payload.get('num')} not found"}))
                else:
                    await ws.send(_envelope("session", {
                        "num": agent.session_num,
                        "message_count": len(agent.history_messages),
                    }))
                    await _reply_sessions(ws)

            elif kind == "session_clear":
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
                text = await asyncio.to_thread(agent.show_tasks)
                if not text.strip():
                    text = "(当前会话暂无待办)"
                await ws.send(_envelope("tasks", {"text": text}))

            elif kind == "skills":
                text = await asyncio.to_thread(agent.skills.list_skills)
                await ws.send(_envelope("skills", {"text": text}))

            else:
                await ws.send(_envelope("error", {"msg": f"unknown kind: {kind}"}))
    finally:
        writer_task.cancel()


async def _reply_sessions(ws):
    items = await asyncio.to_thread(agent.session_manager.list_sessions)
    sessions = [_session_meta(i) for i in items]
    await ws.send(_envelope("sessions", {"sessions": sessions}))


async def main():
    async with websockets.serve(handle, "127.0.0.1", PORT):
        print(f"[ws_bridge] WS server listening on 127.0.0.1:{PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())