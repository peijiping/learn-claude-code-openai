#!/usr/bin/env python3
"""上下文压缩四层管线的回归测试 —— 2026-09-20。

背景（本次改造顺带修掉的既存缺陷）：`context_compact.py` 从 LangChain 迁到
**纯 dict 消息**时只迁了一半 —— L1/L2/L3/L4 里散着 `msg.content`、`msg.tool_calls`、
`isinstance(m, AIMessage)`、`HumanMessage(...)` 这类对象式访问，而历史消息实际是
`json.JSONDecoder().raw_decode` 产出的 plain dict。

后果被一个阈值掩盖了很久：`maybe_compact_context` 只在上下文用到 95% 才调
`compact_if_needed`，所以这些代码平时根本不执行 —— 一旦真的触发（长会话、或
手动 `/compact`），L3 的 `_find_last_ai_index` 先抛 NameError，整轮被打断，
**压缩从来没成功过**。

本文件因此有两个目的：
  1. 锁住"四层在 dict 消息上都不抛"这条底线（`CompactPipelineTests`）；
  2. 锁住 L1 的**附件保护** —— 图片在 token 估算里只算约 5 个字符，是最廉价、
     也最不该被裁掉的东西。

运行：`.venv/bin/python -m unittest discover -s tests -v`（pytest 未安装，用内置 unittest）
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import context_compact as C  # noqa: E402


def _compact(root: Path, max_context_tokens: int = 1_000_000) -> C.ContextCompact:
    """只填测试用得到的字段（不触发构造流程 / 网络）。

    `summarize_history` 的真 LLM 调用一律用注入的 `summarizer` 绕过。
    """
    cc = C.ContextCompact.__new__(C.ContextCompact)
    cc.max_context_tokens = max_context_tokens
    cc.transcript_dir = root / "transcripts"
    cc.tool_results_dir = root / "tool_results"
    cc.llm_client = None
    return cc


def user(text, **kw):
    return {"role": "user", "content": text, **kw}


def assistant(text="", **kw):
    return {"role": "assistant", "content": text, **kw}


def tool_result(text, call_id="c1"):
    return {"role": "tool", "content": text, "tool_call_id": call_id}


def attachment_message(name="a.png", text=""):
    return {"role": "user", "content": [
        *([{"type": "text", "text": text}] if text else []),
        {"type": "attachment",
         "attachment": {"id": "att_x1", "kind": "image", "name": name}},
    ]}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cc = _compact(self.root)


class SnipCompactTests(_Base):
    """L1：把中间消息换成占位，但**附件消息不裁**。"""

    def _history(self, middle: list) -> list:
        """头 3 + 指定中段 + 尾 47 —— 总数 > SNIP_MAX_MESSAGES(50)。"""
        return ([user(f"h{i}") for i in range(C.SNIP_KEEP_HEAD)]
                + middle
                + [user(f"t{i}") for i in range(C.SNIP_KEEP_TAIL)])

    def test_noop_below_threshold(self):
        msgs = [user("a"), user("b")]
        self.assertIs(self.cc.snip_compact(msgs), msgs)

    def test_snips_middle_and_counts_honestly(self):
        msgs = self._history([user(f"m{i}") for i in range(4)])
        out = self.cc.snip_compact(msgs)
        self.assertEqual(len(out), len(msgs) - 4 + 1)
        placeholder = [m for m in out if isinstance(m.get("content"), str)
                       and "snipped" in m["content"]]
        self.assertEqual(len(placeholder), 1)
        self.assertIn("snipped 4 messages", placeholder[0]["content"])
        # 头尾原文保留
        self.assertEqual(out[:C.SNIP_KEEP_HEAD], msgs[:C.SNIP_KEEP_HEAD])
        self.assertEqual(out[-C.SNIP_KEEP_TAIL:], msgs[-C.SNIP_KEEP_TAIL:])

    def test_attachment_message_in_middle_survives(self):
        """核心：带附件的消息不能被裁掉。

        图片在 `content_to_str` 里只折算成 `[图片: name]`（约 5 个字符），
        在 token 估算里看起来是最廉价的裁剪对象 —— 裁掉它模型就再也看不到那份
        附件了，而图片无法靠"用工具再读一次"补回来。
        """
        keep = attachment_message("报告.png", "看这个")
        msgs = self._history([user("m0"), keep, user("m2"), user("m3")])
        out = self.cc.snip_compact(msgs)
        self.assertIn(keep, out)
        # 占位里的条数只统计真正被裁掉的（4 条中裁 3 条、留 1 条附件）
        placeholder = [m for m in out if isinstance(m.get("content"), str)
                       and "snipped" in m["content"]][0]
        self.assertIn("snipped 3 messages", placeholder["content"])

    def test_multiple_attachment_messages_all_survive(self):
        keeps = [attachment_message(f"{i}.png") for i in range(3)]
        msgs = self._history([keeps[0], user("m1"), keeps[1], user("m3"), keeps[2]])
        out = self.cc.snip_compact(msgs)
        for keep in keeps:
            self.assertIn(keep, out)
        # 保留相对顺序
        idx = [out.index(k) for k in keeps]
        self.assertEqual(idx, sorted(idx))

    def test_placeholder_is_a_plain_dict_message(self):
        """占位必须是 dict —— 历史要落 jsonl 并能从磁盘读回。"""
        msgs = self._history([user(f"m{i}") for i in range(4)])
        placeholder = [m for m in self.cc.snip_compact(msgs)
                       if isinstance(m.get("content"), str) and "snipped" in m["content"]][0]
        self.assertIsInstance(placeholder, dict)
        self.assertEqual(placeholder["role"], "user")

    def test_tool_use_and_result_are_not_split(self):
        """裁剪边界不能把 assistant(tool_calls) 与它的 tool 结果拆开。"""
        paired = [assistant("", tool_calls=[{"id": "c9"}]),
                  tool_result("结果", "c9")]
        msgs = [user("h0"), user("h1"), user("h2")] + paired + \
               [user(f"t{i}") for i in range(47)]
        out = self.cc.snip_compact(msgs)
        # 要么两条都在，要么两条都被裁；不允许只剩一个
        ai_in = [i for i, m in enumerate(out) if m.get("tool_calls")]
        tool_in = [i for i, m in enumerate(out) if m.get("role") == "tool"]
        self.assertEqual(len(ai_in), len(tool_in))


class MicroCompactTests(_Base):
    """L2：旧的超长 tool_result 换成占位，不删除（保住 tool_use/tool_result 配对）。"""

    def test_old_long_tool_results_are_replaced(self):
        msgs = [tool_result("Y" * 500, f"c{i}") for i in range(6)]
        replaced = self.cc.micro_compact(msgs, keep_recent=2)
        self.assertEqual(replaced, 4)
        for msg in msgs[:4]:
            self.assertEqual(msg["content"], self.cc._MICRO_PLACEHOLDER)
        for msg in msgs[4:]:
            self.assertEqual(msg["content"], "Y" * 500)

    def test_short_and_already_placeholder_are_untouched(self):
        msgs = [tool_result("短", "c0"), tool_result("Z" * 500, "c1"),
                tool_result(self.cc._MICRO_PLACEHOLDER, "c2"),
                tool_result("最近1", "c3"), tool_result("最近2", "c4")]
        replaced = self.cc.micro_compact(msgs, keep_recent=2)
        self.assertEqual(replaced, 1)          # 只有 c1 那条
        self.assertEqual(msgs[3]["content"], "最近1")


class ToolResultBudgetTests(_Base):
    """L3：把超大的 tool_result 落盘，换成"路径 + 预览"。"""

    def test_large_tool_result_is_persisted(self):
        # 注意：`persist_large_output` 内部用的是**模块常量** `PERSIST_THRESHOLD`
        # （默认 30000），并不吃 `tool_result_budget(persist_threshold=...)` 那个参数
        # —— 传更小的值只会影响"要不要尝试"，真正落盘的闸门仍是 30000。所以这里的
        # 正文必须超过 30000 才会被换掉。（这个耦合是既有的，本次只记录不动它。）
        big = "X" * 40_000
        msgs = [assistant("", tool_calls=[{"id": "c1"}]),
                tool_result(big, "c1")]
        n = self.cc.tool_result_budget(msgs, max_bytes=1000, persist_threshold=500)
        self.assertEqual(n, 1)
        self.assertIn("persisted-output", str(msgs[1]["content"]))
        self.assertLess(len(msgs[1]["content"]), len(big))

    def test_under_budget_is_untouched(self):
        msgs = [assistant("", tool_calls=[{"id": "c1"}]),
                tool_result("X" * 100, "c1")]
        self.assertEqual(
            self.cc.tool_result_budget(msgs, max_bytes=10_000), 0)
        self.assertEqual(msgs[1]["content"], "X" * 100)

    def test_no_tool_results_returns_zero(self):
        self.assertEqual(self.cc.tool_result_budget([user("hi")]), 0)


class CompactHistoryTests(_Base):
    """L4：中段摘要成一条 dict 消息（用注入的 summarizer，不调 API）。"""

    def _history(self, n: int) -> list:
        return ([{"role": "system", "content": "sys"}]
                + [user("u" * 50) for _ in range(n)])

    def test_middle_is_summarized_into_a_dict_message(self):
        msgs = self._history(20)
        out = self.cc.compact_history(msgs, summarizer=lambda p: "SUMMARY")
        self.assertLess(len(out), len(msgs))
        self.assertEqual(out[0], {"role": "system", "content": "sys"})
        summary = [m for m in out if isinstance(m.get("content"), str)
                   and "<context_summary>" in m["content"]]
        self.assertEqual(len(summary), 1)
        self.assertIsInstance(summary[0], dict)
        self.assertEqual(summary[0]["role"], "user")
        self.assertIn("SUMMARY", summary[0]["content"])

    def test_transcript_is_written_before_compaction(self):
        self.cc.compact_history(self._history(20), summarizer=lambda p: "S")
        self.assertTrue(any(self.cc.transcript_dir.iterdir()))

    def test_short_history_is_left_alone(self):
        msgs = self._history(3)
        self.assertEqual(self.cc.compact_history(msgs, summarizer=lambda p: "S"),
                         msgs)


class CompactPipelineTests(_Base):
    """编排层：四层在 **dict 消息**上都不许抛。

    这是本次修的既存缺陷的回归闸门 —— 改前任一层抛 NameError/AttributeError
    都会顺着 `maybe_compact_context` 打到 `run_turn`，把整轮打死。
    """

    def _big_history(self) -> list:
        """构造 > SNIP_MAX_MESSAGES 条的真实形状历史。

        布局要点：把 assistant(tool_calls) + 6 条 tool 结果放在**尾部保留区内**。
        L1 先于 L2 执行，放中段的 tool 结果会被 L1 整段裁掉，L2 就无货可换 ——
        这正是这四层"谁先跑"的耦合，测试要贴着真实顺序摆数据。
        """
        msgs = [{"role": "system", "content": "sys"}]
        msgs += [user(f"h{i}") for i in range(C.SNIP_KEEP_HEAD)]
        msgs += [user("m" * 200) for _ in range(20)]          # 会被 L1 裁掉的中段
        # 尾部保留区 = 最后 SNIP_KEEP_TAIL 条，从下面这条 assistant 开始
        msgs += [assistant("说点什么", tool_calls=[{"id": f"c{i}"} for i in range(6)])]
        msgs += [tool_result("R" * 500, f"c{i}") for i in range(6)]
        msgs += [user(f"t{i}") for i in range(C.SNIP_KEEP_TAIL - 7)]
        return msgs

    def test_pipeline_runs_without_raising(self):
        msgs = self._big_history()
        result = self.cc.compact_if_needed(msgs)   # 不抛即通过
        self.assertIsInstance(result.messages, list)
        self.assertGreater(len(result.messages), 0)

    def test_pipeline_triggers_snip(self):
        ops = self.cc.compact_if_needed(self._big_history()).operations
        self.assertGreater(ops.get("messages_snip_compacted", 0), 0)

    def test_pipeline_triggers_micro_on_surviving_tool_results(self):
        ops = self.cc.compact_if_needed(self._big_history()).operations
        self.assertGreater(ops.get("tool_results_micro_compacted", 0), 0)

    def test_pipeline_keeps_attachment_messages(self):
        keep = attachment_message("重要.png", "看这个")
        msgs = self._big_history()
        # 塞进会被裁掉的中段
        msgs.insert(C.SNIP_KEEP_HEAD + 2, keep)
        result = self.cc.compact_if_needed(msgs)
        self.assertIn(keep, result.messages)

    def test_pipeline_does_not_touch_a_small_history(self):
        msgs = [user("a"), assistant("b")]
        result = self.cc.compact_if_needed(msgs)
        self.assertEqual(len(result.messages), 2)
        self.assertFalse(result.changed)

    def test_stats_work_on_dict_messages(self):
        """token 估算一直是好的（`message_to_text` 有 hasattr 兜底），
        别在修四层时把它弄坏。"""
        stats = self.cc.context_stats(self._big_history())
        self.assertGreater(stats.used_tokens, 0)
        self.assertLessEqual(stats.used_percent, 100.0)


if __name__ == "__main__":
    unittest.main()
