#!/usr/bin/env python3
"""`ask_user` 桥层接线守护测试 —— 2026-09-21。

覆盖两块：

1. **在途提问重放**（`_ask_snapshot_lines`）：用户关窗 / 刷新 / 断线重连时，
   turn 可能仍阻塞在"等作答"上。不重放 → 前端面板永远不再出现而后端还在等，
   表现为"卡住不动"。这里验函数本身（含 `registry is None` 的兜底），
   以及它被接进了连接建立重放与 `status_query` 两个入口。
2. **结构性不变量**（源码级断言，这些次序问题无法用行为测试表达）：
   - `chat` 的自由作答拦截必须在 `if rt.busy` **之前** —— 提问期间 turn 正阻塞、
     `busy=True`，落到守卫之后就只会回一句"正在执行，请先停止"，而用户的真实
     意图恰恰是作答；
   - `ask_answer` / `ask_cancel` 两条入站命令必须注册；
   - `_ask_snapshot_lines` 必须定义在**源码切片区间之外**（`_text_of` 之前）：
     切片区间内只能放 def，且 def 的注解在 def 处即求值，新增模块级函数是
     守卫测试（test_subagent_sidecar / test_system_injection_contract）失效的
     最常见原因 —— 症状是整个测试模块 import 失败、`Ran N` 总数掉一截。

`import ws_bridge` 会执行模块顶层自举（与 test_workspace_commands 同样做法），
故本文件用隔离 HOME 跑（见 README / 回归命令）。

入口：`.venv/bin/python -m unittest discover -s tests`
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import ws_bridge  # noqa: E402

WS_SRC = (AGENTS_DIR / "ws_bridge.py").read_text(encoding="utf-8")


def _payload(sid: str, rid: str) -> dict:
    return {
        "session_id": sid,
        "request_id": rid,
        "tool_call_id": f"toolu_{rid}",
        "questions": [{"id": "q", "header": "h", "question": "q?",
                       "multi_select": False, "allow_custom": True,
                       "custom_label": "其他", "options": [{"label": "a"}]}],
        "created_at": 1.0,
    }


class _FakeRuntime:
    def __init__(self, sid, pending):
        self.sid = sid
        self._pending = pending

    def pending_interactions(self):
        return self._pending


class _FakeRegistry:
    def __init__(self, runtimes):
        self._runtimes = runtimes

    def all_runtimes(self):
        return list(self._runtimes)


class TestAskSnapshotLines(unittest.TestCase):
    def setUp(self):
        self._orig = ws_bridge.registry

        def restore():
            ws_bridge.registry = self._orig

        self.addCleanup(restore)

    def test_registry_none_returns_empty(self):
        ws_bridge.registry = None
        self.assertEqual(ws_bridge._ask_snapshot_lines(), [])

    def test_empty_registry_returns_empty(self):
        ws_bridge.registry = _FakeRegistry([])
        self.assertEqual(ws_bridge._ask_snapshot_lines(), [])

    def test_runtime_without_pending_contributes_nothing(self):
        ws_bridge.registry = _FakeRegistry([_FakeRuntime("s1", [])])
        self.assertEqual(ws_bridge._ask_snapshot_lines(), [])

    def test_collects_pending_across_sessions(self):
        ws_bridge.registry = _FakeRegistry([
            _FakeRuntime("s1", [_payload("s1", "ask_1")]),
            _FakeRuntime("s2", [_payload("s2", "ask_2")]),
        ])
        envs = [json.loads(line) for line in ws_bridge._ask_snapshot_lines()]
        self.assertEqual([e["kind"] for e in envs], ["ask_request", "ask_request"])
        self.assertEqual([e["payload"]["session_id"] for e in envs], ["s1", "s2"])
        self.assertEqual([e["payload"]["request_id"] for e in envs], ["ask_1", "ask_2"])


class TestStructuralInvariants(unittest.TestCase):
    """次序/接线类不变量 —— 行为测试表达不了，用源码定位固化成断言。"""

    def test_free_text_interception_precedes_busy_guard(self):
        intercept = WS_SRC.index("resolve_ask_free_text, text)")
        busy = WS_SRC.index("if rt.busy:")
        self.assertLess(
            intercept, busy,
            "自由作答拦截必须在 busy 守卫之前：提问期间 busy=True，"
            "落到守卫后就只会回「正在执行，请先停止」，而用户意图是作答",
        )

    def test_inbound_kinds_registered(self):
        for kind in ("ask_answer", "ask_cancel"):
            self.assertIn(f'elif kind == "{kind}":', WS_SRC, f"{kind} 分支未注册")

    def test_outbound_kinds_emitted(self):
        # 出站信封由 interaction.py 的 broker 投递；这里守住"事件名一字不差"
        interaction_src = (AGENTS_DIR / "interaction.py").read_text(encoding="utf-8")
        for kind in ("ask_request", "ask_resolved"):
            self.assertIn(f'"{kind}"', interaction_src, f"{kind} 事件未投递")

    def test_replay_wired_into_both_entrypoints(self):
        """连接建立重放 + status_query 各一处（外加函数定义本身）。"""
        self.assertGreaterEqual(
            WS_SRC.count("_ask_snapshot_lines()"), 3,
            "在途提问重放未同时接进「连接建立」与「status_query」",
        )

    def test_ask_snapshot_defined_outside_exec_slice(self):
        fn = WS_SRC.index("def _ask_snapshot_lines()")
        slice_start = WS_SRC.index("def _text_of(")
        self.assertLess(
            fn, slice_start,
            "_ask_snapshot_lines 落在源码切片区间内 → 守卫测试的 exec 会炸",
        )

    def test_slice_region_module_level_statements_are_safe_to_exec(self):
        """切片区间的**模块级语句**必须能在裸命名空间里安全执行。

        `_text_of` → `handle` 这段源码会被两个守卫测试 exec 到只有 def/注解
        所需名字的裸 dict 里。区里出现模块级语句本身是允许的（已有
        `_ATTACH_GC_LAST = 0.0` 这类字面量常量），但**语句一旦引用裸命名空间
        里没有的名字就会 NameError** —— 症状是整个测试模块 import 失败、
        `Ran N` 总数掉一截（2026-09-20 附件那次踩过：模块级 `threading.Lock()`）。

        因此这里只放行：def / async def / 装饰器 / 注释 / 空行 / 纯字面量赋值。
        """
        start = WS_SRC.index("def _text_of(")
        end = WS_SRC.index("async def handle(ws):")
        literal_assign = re.compile(
            r"^[A-Za-z_]\w*\s*=\s*(\d+\.?\d*|None|True|False|\[\]|\{\}|\(\)|''|\"\")\s*$"
        )
        offenders = [
            line
            for line in WS_SRC[start:end].splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and line[:1] not in (" ", "\t")           # 缩进 = 函数体内部
            and not line.strip().startswith(("def ", "async def ", "@"))
            and not literal_assign.match(line.strip())
        ]
        self.assertEqual(
            offenders, [],
            "源码切片区间出现无法在裸命名空间安全执行的模块级语句；"
            "请把它移到 `def _text_of(` 之前（或放在 handle 体内）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
