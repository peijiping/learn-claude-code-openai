#!/usr/bin/env python3
"""`ask_user` 回放配对守护测试 —— 2026-09-21。

`ask_user` 是唯一"问题与答案分居两条 jsonl 行"的工具：
- **问题**在 assistant 行的 `tool_calls[].function.arguments`（`questions`）；
- **答案**在紧接着的 `role=tool` 行的 `content`（= broker 生成的 result_text）。

因此 `_history_to_ui` 必须做三件在别处不存在的事：

1. 把 `ask_user` **从 `toolCalls` 里剥离** —— 否则回放时它会长成一条普通工具条
   （与实时路径的"只读小结块"不一致）。
2. 按 `tool_call_id` 把 tool 行 content **配对**回去，挂成 `askUsers[]`
   （实时/回放展示同一份文本，前端不解析）。
3. `_tc_ids` **保留** ask_user 的 id —— 唯一锚点规则与前端锚定都依赖它。
   另外：无提问的 assistant 消息**连字段都不多一个**（逐字节等价）。

`import ws_bridge` 会执行模块顶层自举，故这里沿用
`test_subagent_sidecar._load_history_to_ui` 的**源码切片**手法，只取
`_text_of` → `_attach_subagent` → `_history_to_ui` 之间的代码执行。

入口：`.venv/bin/python -m unittest discover -s tests`
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from session_manage import SessionManager  # noqa: E402


def _load_history_to_ui():
    """从 ws_bridge 抽取 `_text_of` / `_attach_subagent` / `_history_to_ui`。

    切片里的模块级引用必须在此显式预置（ws_bridge 演进时在本函数里补名）：
    - 类型标注在 def 处即求值 → 预置整个 typing 命名空间；
    - SessionManager / WorkspacePaths：附件辅助函数的参数注解；
    - harvest_attachments / harvest_refs / is_tool_images_message：
      `_history_to_ui` 会实际调用。
    - status_of_result（interaction 模块）：`_history_to_ui` 用它给 askUsers[]
      反推结局徽标。**只预置、不在这里重写判定** —— 文案真相只有
      interaction 模块一处（见 status_of_result 的 docstring）。
    """
    import typing
    from attachments import harvest_attachments, is_tool_images_message
    from interaction import status_of_result
    from paths import WorkspacePaths
    from refs import harvest_refs

    src = (AGENTS_DIR / "ws_bridge.py").read_text(encoding="utf-8")
    seg = src[src.index("def _text_of("):src.index("async def handle(ws):")]
    ns: dict = {n: getattr(typing, n) for n in dir(typing) if not n.startswith("_")}
    ns.update({
        "SessionManager": SessionManager,
        "WorkspacePaths": WorkspacePaths,
        "harvest_attachments": harvest_attachments,
        "harvest_refs": harvest_refs,
        "is_tool_images_message": is_tool_images_message,
        "status_of_result": status_of_result,
    })
    exec(compile(seg, "ws_bridge_hist", "exec"), ns)  # noqa: S102 - 测试内自用
    return ns["_history_to_ui"]


history_to_ui = _load_history_to_ui()

ASK_ARGS = json.dumps({"questions": [{
    "id": "color", "header": "主题色", "question": "你希望主题色是？",
    "options": [{"label": "蓝色"}, {"label": "绿色"}],
}]}, ensure_ascii=False)


def _assistant(tool_calls, content="", **extra):
    row = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    row.update(extra)
    return row


def _call(name, args, tcid):
    return {"id": tcid, "type": "function",
            "function": {"name": name, "arguments": args}}


def _tool_row(tcid, content):
    return {"role": "tool", "tool_call_id": tcid, "content": content}


class TestReplayPairing(unittest.TestCase):
    def test_ask_user_moved_to_ask_users_with_result(self):
        result = "用户已完成选择：\n- [主题色] 你希望主题色是？ → 绿色"
        msgs = [
            _assistant([_call("ask_user", ASK_ARGS, "toolu_ask")]),
            _tool_row("toolu_ask", result),
        ]
        ui = history_to_ui(msgs)
        msg = ui[0]
        self.assertEqual(msg["toolCalls"], [], "ask_user 不该以普通工具条出现")
        self.assertEqual(len(msg["askUsers"]), 1)
        ask = msg["askUsers"][0]
        self.assertEqual(ask["tool_call_id"], "toolu_ask")
        self.assertEqual(json.loads(ask["args"])["questions"][0]["id"], "color")
        self.assertEqual(ask["result"], result)
        # 结局徽标由 interaction.status_of_result 反推（jsonl 不存 outcome）。
        # 前端只搬运、不猜文案 —— 这条断言同时守住"桥层确实把它带出去了"。
        self.assertEqual(ask["status"], "answered")

    def test_tc_ids_keep_ask_user_id_for_anchoring(self):
        """唯一锚点规则依赖 _tc_ids 含全部 id（含 ask_user）。"""
        msgs = [
            _assistant([_call("ask_user", ASK_ARGS, "toolu_ask")]),
            _tool_row("toolu_ask", "x"),
        ]
        self.assertEqual(history_to_ui(msgs)[0]["_tc_ids"], ["toolu_ask"])

    def test_unanswered_ask_has_empty_result(self):
        """进程被杀等场景：只有 assistant 行、tool 行缺失 → result 为空串。"""
        ui = history_to_ui([_assistant([_call("ask_user", ASK_ARGS, "toolu_ask")])])
        self.assertEqual(ui[0]["askUsers"][0]["result"], "")
        # 未完成要能被识别（前端据此渲染「提问未完成」，而不是假装已回答）
        self.assertEqual(ui[0]["askUsers"][0]["status"], "incomplete")

    def test_cancelled_and_stopped_statuses_survive_roundtrip(self):
        """取消/被停止：文案不同、结局也不同 —— 只读小结的颜色/文案靠它区分。"""
        from interaction import CANCELLED_TEXT, STOPPED_TEXT
        for text, expect in ((CANCELLED_TEXT, "cancelled"), (STOPPED_TEXT, "stopped")):
            msgs = [
                _assistant([_call("ask_user", ASK_ARGS, "toolu_ask")]),
                _tool_row("toolu_ask", text),
            ]
            self.assertEqual(history_to_ui(msgs)[0]["askUsers"][0]["status"], expect)

    def test_normal_tools_unaffected(self):
        msgs = [
            _assistant([_call("bash", '{"command": "ls"}', "toolu_b")]),
            _tool_row("toolu_b", "file1\nfile2"),
        ]
        ui = history_to_ui(msgs)
        self.assertEqual([t["name"] for t in ui[0]["toolCalls"]], ["bash"])
        self.assertNotIn("askUsers", ui[0])

    def test_mixed_batch_splits_correctly(self):
        msgs = [
            _assistant([
                _call("bash", '{"command": "ls"}', "toolu_b"),
                _call("ask_user", ASK_ARGS, "toolu_ask"),
            ]),
            _tool_row("toolu_b", "out"),
            _tool_row("toolu_ask", "答案"),
        ]
        ui = history_to_ui(msgs)
        self.assertEqual([t["name"] for t in ui[0]["toolCalls"]], ["bash"])
        self.assertEqual([a["tool_call_id"] for a in ui[0]["askUsers"]], ["toolu_ask"])
        self.assertEqual(ui[0]["askUsers"][0]["result"], "答案")
        self.assertEqual(ui[0]["_tc_ids"], ["toolu_b", "toolu_ask"])

    def test_assistant_without_tools_has_no_ask_users_key(self):
        """逐字节等价证明：无提问的消息不该多出任何字段。"""
        ui = history_to_ui([{"role": "assistant", "content": "普通回复"}])
        self.assertNotIn("askUsers", ui[0])
        self.assertEqual(ui[0]["toolCalls"], [])

    def test_tool_row_content_can_be_multimodal_list(self):
        msgs = [
            _assistant([_call("ask_user", ASK_ARGS, "toolu_ask")]),
            _tool_row("toolu_ask", [{"type": "text", "text": "分块答案"}]),
        ]
        self.assertEqual(history_to_ui(msgs)[0]["askUsers"][0]["result"], "分块答案")

    def test_tool_rows_are_still_not_shown_as_messages(self):
        """回归：tool 行本身永远不上屏（历史上靠"跳过 role=tool"）。"""
        msgs = [
            _assistant([_call("ask_user", ASK_ARGS, "toolu_ask")]),
            _tool_row("toolu_ask", "答案"),
        ]
        self.assertEqual([m["role"] for m in history_to_ui(msgs)], ["assistant"])

    def test_multiple_asks_all_paired(self):
        msgs = [
            _assistant([_call("ask_user", ASK_ARGS, "t1")]),
            _tool_row("t1", "答案一"),
            _assistant([_call("ask_user", ASK_ARGS, "t2")]),
            _tool_row("t2", "答案二"),
        ]
        ui = history_to_ui(msgs)
        self.assertEqual(ui[0]["askUsers"][0]["result"], "答案一")
        self.assertEqual(ui[1]["askUsers"][0]["result"], "答案二")


if __name__ == "__main__":
    unittest.main(verbosity=2)
