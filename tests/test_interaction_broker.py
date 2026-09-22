#!/usr/bin/env python3
"""交互提问 broker（`agents/interaction.py`）守护测试 —— 2026-09-21。

`ask_user` 工具的价值全在"跨线程请求-应答"这条链上，因此这里**不 mock 内部函数**，
而是真起一个线程跑 `broker.ask()`（模拟 turn 工作线程），主线程负责结算
（模拟 asyncio 事件循环线程），断言：

- 事件流：`ask_request` → `ask_resolved`，载荷字段与 status 正确；
- 返回文本：结构化作答 / 多选 + 自定义 / 未选占位 / 自由文本 / 取消 / 停止，
  六条路径的回填文案（这些文本同时是前端只读小结展示的同一份，必须稳定）；
- 幂等：迟到/重复提交返回 False、不二次唤醒、不二次广播；
- **等待期不持锁**：ask 阻塞期间主线程仍能 `resolve` 且立即生效
  （若把 `done.wait()` 写在锁内，这里会死锁 —— 这是本模块最要命的回归）；
- 兜底唤醒：只置 `stop_event`（不走 `cancel_all`）也能在轮询切片内收束；
- 参数校验：模型乱填（题数/选项数/空 label/重复 label）一律 `Error:` 文本；
- 工具层契约：`deliver` 抛异常也不影响 `ask` 返回字符串。

入口：`.venv/bin/python -m unittest discover -s tests`
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from interaction import (  # noqa: E402
    BUSY_TEXT,
    CANCELLED_TEXT,
    MAX_QUESTIONS,
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_STOPPED,
    STOPPED_TEXT,
    InteractionBroker,
    format_answered,
    format_free_text,
    normalize_answers,
    normalize_questions,
    status_of_result,
)

SID = "FAKESESSION01"

QUESTIONS = [
    {
        "id": "color",
        "header": "主题色",
        "question": "你希望主题色是？",
        "options": [{"label": "蓝色"}, {"label": "绿色", "description": "护眼"}],
    },
    {
        "id": "fmt",
        "header": "导出",
        "question": "需要支持哪些导出格式？",
        "multi_select": True,
        "options": [{"label": "Markdown"}, {"label": "HTML"}, {"label": "PDF"}],
    },
]


class _Asker:
    """在独立线程里跑 `broker.ask()`，主线程负责结算（复刻生产的双线程模型）。

    `ready` 是"可以开始结算"的判据。**默认用 `has_pending()` 不够**：
    pending 是在广播 `ask_request` 之前注册的，只等它会出现"结算抢在广播之前"
    的竞态（表现为 `requested()` 拿到空 dict）。所以用例里统一传
    `lambda: bool(self.requested())`，等广播真的发出来。
    """

    def __init__(self, broker, questions, stop_event=None, ready=None):
        self.broker = broker
        self.questions = questions
        self.stop_event = stop_event
        self._ready = ready or broker.has_pending
        self.result = None
        self.exc = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = self.broker.ask(
                self.questions, tool_call_id="toolu_fake_1", stop_event=self.stop_event
            )
        except BaseException as e:  # noqa: BLE001 - 就是要抓住"抛了"
            self.exc = e

    def __enter__(self):
        self.thread.start()
        # 等在途请求注册 **且** ask_request 已广播，避免与主线程的 resolve 抢跑
        deadline = time.time() + 3.0
        while time.time() < deadline and not self._ready():
            time.sleep(0.005)
        return self

    def __exit__(self, *exc_info):
        self.thread.join(timeout=5.0)
        return False

    @property
    def finished(self) -> bool:
        return not self.thread.is_alive()


class _BrokerTestCase(unittest.TestCase):
    def setUp(self):
        self.events: list[tuple[str, dict]] = []

        def deliver(kind, payload):
            self.events.append((kind, payload))

        self.broker = InteractionBroker(SID, deliver)

    # ── 事件流辅助 ────────────────────────────────────────────
    def asker(self, questions=QUESTIONS, stop_event=None) -> _Asker:
        """起一个 ask 线程，等 `ask_request` 真的广播出来后再返回（避免结算抢跑）。"""
        return _Asker(self.broker, questions, stop_event,
                      ready=lambda: bool(self.requested()))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def resolved(self) -> dict:
        for k, p in self.events:
            if k == "ask_resolved":
                return p
        return {}

    def requested(self) -> dict:
        for k, p in self.events:
            if k == "ask_request":
                return p
        return {}


class TestAskRequestBroadcast(_BrokerTestCase):
    """发起提问：注册 + 广播载荷 + 结算广播。"""

    def test_request_payload_shape(self):
        with self.asker() as a:
            self.assertTrue(self.broker.has_pending())
            payload = self.requested()
            self.assertEqual(payload["session_id"], SID)
            self.assertEqual(payload["tool_call_id"], "toolu_fake_1")
            self.assertTrue(payload["request_id"].startswith("ask_"))
            self.assertIsInstance(payload["created_at"], float)
            self.assertEqual([q["id"] for q in payload["questions"]], ["color", "fmt"])
            # allow_custom 必须是显式字段（禁止靠 label == "其他" 的隐式约定）
            self.assertTrue(all(q["allow_custom"] for q in payload["questions"]))
            self.assertEqual(payload["questions"][0]["custom_label"], "其他")
            a.broker.resolve(payload["request_id"], [])
        self.assertEqual(a.result, format_answered(payload["questions"], []))

    def test_resolved_payload_shape_on_answer(self):
        with self.asker() as a:
            rid = self.requested()["request_id"]
            self.assertTrue(self.broker.resolve(rid, [
                {"question_id": "color", "selected": ["绿色"]},
            ]))
        self.assertEqual(self.kinds(), ["ask_request", "ask_resolved"])
        payload = self.resolved()
        self.assertEqual(payload["status"], OUTCOME_ANSWERED)
        self.assertEqual(payload["session_id"], SID)
        self.assertEqual(payload["tool_call_id"], "toolu_fake_1")
        self.assertEqual(payload["answers"][0]["selected"], ["绿色"])
        # 前端只读小结与模型看到的文本必须是同一份
        self.assertEqual(payload["result_text"], a.result)


class TestAnswerFormatting(_BrokerTestCase):
    """返回给模型的 tool_result 文案（前端只读小结同源）。"""

    def _ask_and_answer(self, answers, questions=QUESTIONS) -> str:
        with self.asker(questions) as a:
            self.broker.resolve(self.requested()["request_id"], answers)
        return a.result

    def test_single_select_and_multi_select_and_custom(self):
        text = self._ask_and_answer([
            {"question_id": "color", "selected": ["绿色"]},
            {"question_id": "fmt", "selected": ["Markdown", "PDF"], "custom_text": "还想要 EPUB"},
        ])
        self.assertIn("用户已完成选择：", text)
        self.assertIn("- [主题色] 你希望主题色是？ → 绿色", text)
        self.assertIn("- [导出] 需要支持哪些导出格式？（可多选） → Markdown、PDF；其他：还想要 EPUB", text)

    def test_unselected_shows_placeholder(self):
        text = self._ask_and_answer([])
        self.assertIn("→ （未选择）", text)
        self.assertEqual(text.count("（未选择）"), 2)

    def test_single_select_keeps_first_only(self):
        text = self._ask_and_answer([
            {"question_id": "color", "selected": ["蓝色", "绿色"]},
        ])
        self.assertIn("→ 蓝色", text)
        self.assertNotIn("蓝色、绿色", text)

    def test_dirty_labels_are_dropped(self):
        """前端/模型塞进来的非法 label 必须被过滤，不能污染回填文本。"""
        text = self._ask_and_answer([
            {"question_id": "color", "selected": ["紫色", "蓝色"]},
        ])
        self.assertIn("→ 蓝色", text)
        self.assertNotIn("紫色", text)

    def test_multi_select_dedupes_and_keeps_order(self):
        text = self._ask_and_answer([
            {"question_id": "fmt", "selected": ["PDF", "HTML", "PDF"]},
        ])
        self.assertIn("→ PDF、HTML", text)

    def test_unknown_question_id_is_ignored(self):
        text = self._ask_and_answer([
            {"question_id": "不存在", "selected": ["蓝色"]},
        ])
        self.assertIn("→ （未选择）", text)

    def test_free_text_format(self):
        with self.asker() as a:
            self.broker.resolve_free_text(self.requested()["request_id"], "我自己来定")
        self.assertEqual(a.result, format_free_text("我自己来定"))
        self.assertTrue(a.result.startswith("用户没有选择选项，而是直接回复了："))
        self.assertIn("我自己来定", a.result)
        self.assertEqual(self.resolved()["status"], OUTCOME_ANSWERED)

    def test_free_text_via_any(self):
        with self.asker() as a:
            self.assertTrue(self.broker.resolve_free_text_any("直接用默认"))
        self.assertIn("直接用默认", a.result)

    def test_blank_free_text_is_rejected(self):
        with self.asker() as a:
            self.assertFalse(self.broker.resolve_free_text_any("   "))
            self.assertTrue(self.broker.has_pending())
            self.broker.cancel_all(OUTCOME_STOPPED)
        self.assertEqual(a.result, STOPPED_TEXT)


class TestCancellation(_BrokerTestCase):
    """取消（给默认值继续）与停止（立刻收工）文案必须不同。"""

    def test_cancel_returns_cancelled_text(self):
        with self.asker() as a:
            self.assertTrue(self.broker.cancel(self.requested()["request_id"]))
        self.assertEqual(a.result, CANCELLED_TEXT)
        self.assertEqual(self.resolved()["status"], OUTCOME_CANCELLED)
        self.assertIn("默认方案继续推进", a.result)

    def test_cancel_all_returns_stopped_text(self):
        with self.asker() as a:
            self.assertEqual(self.broker.cancel_all(OUTCOME_STOPPED), 1)
        self.assertEqual(a.result, STOPPED_TEXT)
        self.assertEqual(self.resolved()["status"], OUTCOME_STOPPED)
        self.assertIn("不要再调用任何工具", a.result)

    def test_cancel_all_on_empty_is_noop(self):
        self.assertEqual(self.broker.cancel_all(OUTCOME_STOPPED), 0)
        self.assertEqual(self.events, [])

    def test_cancel_unknown_request_returns_false(self):
        self.assertFalse(self.broker.cancel("ask_nope"))


class TestIdempotency(_BrokerTestCase):
    """迟到/重复的提交必须被丢弃，且不二次广播、不二次唤醒。"""

    def test_resolve_twice_second_is_false(self):
        with self.asker() as a:
            rid = self.requested()["request_id"]
            self.assertTrue(self.broker.resolve(rid, [{"question_id": "color", "selected": ["蓝色"]}]))
            self.assertFalse(self.broker.resolve(rid, [{"question_id": "color", "selected": ["绿色"]}]))
        self.assertEqual(self.kinds().count("ask_resolved"), 1)
        self.assertIn("→ 蓝色", a.result)

    def test_resolve_after_cancel_is_discarded(self):
        """先点停止、后到的答案必须丢弃（用户主动放弃的预期语义）。"""
        with self.asker() as a:
            rid = self.requested()["request_id"]
            self.assertEqual(self.broker.cancel_all(OUTCOME_STOPPED), 1)
            self.assertFalse(self.broker.resolve(rid, [{"question_id": "color", "selected": ["蓝色"]}]))
        self.assertEqual(a.result, STOPPED_TEXT)
        self.assertEqual(self.kinds().count("ask_resolved"), 1)

    def test_resolve_unknown_request_returns_false(self):
        self.assertFalse(self.broker.resolve("ask_nope", []))

    def test_close_makes_later_ask_return_stopped(self):
        self.broker.close()
        self.assertEqual(self.broker.ask(QUESTIONS), STOPPED_TEXT)
        self.assertEqual(self.events, [])


class TestConcurrency(_BrokerTestCase):
    """跨线程语义 —— 本模块最容易回归的地方。"""

    def test_waiting_does_not_hold_lock(self):
        """ask 阻塞期间主线程必须仍能查询与结算（把 wait 写进锁内会死锁）。

        这里用"resolve 调用在极短时间内返回"来取证：若 `ask` 持锁等待，
        resolve 会卡在 `self._lock` 上，超时即失败。
        """
        with self.asker() as a:
            rid = self.requested()["request_id"]
            done = threading.Event()
            ok: list[bool] = []

            def _resolve():
                ok.append(self.broker.resolve(rid, [{"question_id": "color", "selected": ["蓝色"]}]))
                done.set()

            t = threading.Thread(target=_resolve, daemon=True)
            t.start()
            self.assertTrue(done.wait(2.0), "resolve 被阻塞：ask 等待期持有了锁")
            t.join(timeout=2.0)
            self.assertEqual(ok, [True])
            # 等待期 concurrent 查询也不能卡
            self.assertFalse(self.broker.has_pending())
        self.assertTrue(a.finished)
        self.assertIn("→ 蓝色", a.result)

    def test_second_ask_rejected_while_pending(self):
        with self.asker() as a:
            first_rid = self.requested()["request_id"]
            second = self.broker.ask(QUESTIONS, tool_call_id="toolu_fake_2")
            self.assertEqual(second, BUSY_TEXT)
            # 不叠加面板：仍然只广播了一次 ask_request
            self.assertEqual(self.kinds().count("ask_request"), 1)
            self.broker.cancel_all(OUTCOME_STOPPED)
        self.assertEqual(self.resolved()["request_id"], first_rid)
        self.assertEqual(a.result, STOPPED_TEXT)

    def test_stop_event_polling_unblocks(self):
        """兜底路径：只置 stop_event（不走 cancel_all）也要在轮询切片内收束。"""
        stop_evt = threading.Event()
        with self.asker(stop_event=stop_evt) as a:
            stop_evt.set()
            deadline = time.time() + 3.0
            while time.time() < deadline and not a.finished:
                time.sleep(0.02)
        self.assertTrue(a.finished, "置位 stop_event 后 ask 未在 3s 内退出")
        self.assertEqual(a.result, STOPPED_TEXT)
        self.assertEqual(self.resolved()["status"], OUTCOME_STOPPED)

    def test_close_unblocks_waiting_thread(self):
        with self.asker() as a:
            self.broker.close()
            deadline = time.time() + 3.0
            while time.time() < deadline and not a.finished:
                time.sleep(0.02)
        self.assertTrue(a.finished, "close() 未能解锁阻塞中的 ask 线程")
        self.assertEqual(a.result, CANCELLED_TEXT)

    def test_deliver_exception_never_breaks_ask(self):
        """投递失败（连接已断等）不能让 ask 抛异常或拿不到返回文本。"""
        def boom(kind, payload):
            raise RuntimeError("deliver 炸了")

        broker = InteractionBroker(SID, boom)
        with _Asker(broker, QUESTIONS) as a:
            self.assertTrue(broker.has_pending())
            self.assertTrue(broker.cancel_all(OUTCOME_STOPPED))
        self.assertIsNone(a.exc)
        self.assertEqual(a.result, STOPPED_TEXT)


class TestAnnounceOrdering(unittest.TestCase):
    """不变量：`ask_request` 一定先于任何**外部触发**的 `ask_resolved` 到达前端。

    否则前端会先收到 ask_resolved —— 表现为"提问面板还没出现就被判定已解决"，
    且只读小结块缺问题。做法是"广播之后才把请求标记为对外可见"。

    这里用**阻塞 deliver** 把广播卡住，制造出"已注册但未广播"的窗口，
    从而可以确定性地断言该窗口内外部一律不可见、不可结算。
    """

    def _broker_with_gate(self):
        gate = threading.Event()
        events: list[tuple[str, dict]] = []

        def deliver(kind, payload):
            if kind == "ask_request":
                gate.wait(5.0)          # 把广播卡在窗口内
            events.append((kind, payload))

        return InteractionBroker(SID, deliver), gate, events

    def test_request_invisible_until_broadcast(self):
        broker, gate, events = self._broker_with_gate()
        worker = threading.Thread(target=lambda: broker.ask(QUESTIONS), daemon=True)
        worker.start()

        # 等 worker 进到 _emit("ask_request") 里卡住
        deadline = time.time() + 3.0
        while time.time() < deadline and not events and not broker._pending:
            time.sleep(0.01)
        time.sleep(0.05)

        self.assertTrue(broker._pending, "worker 未注册 pending（测试前提不成立）")
        # ── 窗口内：对外一律不可见、不可结算 ──
        self.assertFalse(broker.has_pending())
        self.assertEqual(broker.pending_payloads(), [])
        self.assertEqual(broker.first_pending_id(), "")
        self.assertFalse(broker.resolve_free_text_any("抢跑的自由作答"))
        # 一条信封都没发出去（广播还卡在 gate 上）
        self.assertEqual(events, [])

        # 放行广播 → 立刻可见可结算
        gate.set()
        deadline = time.time() + 3.0
        while time.time() < deadline and not broker.has_pending():
            time.sleep(0.01)
        self.assertTrue(broker.has_pending())
        rid = broker.pending_payloads()[0]["request_id"]
        self.assertTrue(broker.resolve(rid, [{"question_id": "color", "selected": ["蓝色"]}]))
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive())

        # 顺序：先 ask_request，后 ask_resolved
        self.assertEqual([k for k, _ in events], ["ask_request", "ask_resolved"])

    def test_cancel_all_settles_even_unannounced(self):
        """停止/删除会话要能结算任何状态（哪怕广播还没发出去）。"""
        broker, gate, events = self._broker_with_gate()
        result: list[str] = []
        worker = threading.Thread(target=lambda: result.append(broker.ask(QUESTIONS)), daemon=True)
        worker.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and not broker._pending:
            time.sleep(0.01)
        self.assertEqual(broker.cancel_all(OUTCOME_STOPPED), 1)
        gate.set()
        worker.join(timeout=3.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [STOPPED_TEXT])


class TestValidation(_BrokerTestCase):
    """模型乱填参数：一律 Error 文本，绝不抛异常、绝不注册 pending。"""

    def _ask_invalid(self, questions) -> str:
        return self.broker.ask(questions)

    def test_no_questions(self):
        self.assertIn("Error: ask_user 参数非法", self._ask_invalid([]))
        self.assertIn("Error: ask_user 参数非法", self._ask_invalid(None))

    def test_too_many_questions(self):
        q = {"id": "x", "header": "h", "question": "q?",
             "options": [{"label": "a"}, {"label": "b"}]}
        out = self._ask_invalid([dict(q, id=f"x{i}") for i in range(MAX_QUESTIONS + 1)])
        self.assertIn(f"最多 {MAX_QUESTIONS} 个问题", out)

    def test_missing_question_text(self):
        out = self._ask_invalid([{"id": "x", "header": "h", "options": [{"label": "a"}, {"label": "b"}]}])
        self.assertIn("缺少 question", out)

    def test_too_few_options(self):
        out = self._ask_invalid([{"id": "x", "header": "h", "question": "q?", "options": [{"label": "a"}]}])
        self.assertIn("至少要 2 个", out)

    def test_too_many_options(self):
        out = self._ask_invalid([{"id": "x", "header": "h", "question": "q?",
                                  "options": [{"label": c} for c in "abcde"]}])
        self.assertIn("最多 4 个", out)

    def test_empty_label(self):
        out = self._ask_invalid([{"id": "x", "header": "h", "question": "q?",
                                  "options": [{"label": "a"}, {"label": "  "}]}])
        self.assertIn("空 label", out)

    def test_duplicate_label(self):
        out = self._ask_invalid([{"id": "x", "header": "h", "question": "q?",
                                  "options": [{"label": "a"}, {"label": "a"}]}])
        self.assertIn("label 重复", out)

    def test_invalid_input_registers_nothing(self):
        self.broker.ask([])
        self.assertFalse(self.broker.has_pending())
        self.assertEqual(self.events, [])


class TestNormalization(unittest.TestCase):
    """规范化与答案对齐的纯函数语义。"""

    def test_defaults_filled(self):
        qs, err = normalize_questions([{
            "question": "你希望主题色是？",
            "options": [{"label": "蓝色"}, {"label": "绿色"}],
        }])
        self.assertEqual(err, "")
        self.assertEqual(qs[0]["id"], "q1")
        self.assertEqual(qs[0]["header"], "你希望主题色是？"[:12])
        self.assertFalse(qs[0]["multi_select"])
        self.assertTrue(qs[0]["allow_custom"])
        self.assertEqual(qs[0]["options"][0]["description"], "")

    def test_header_clipped_and_duplicate_ids_deduped(self):
        qs, err = normalize_questions([
            {"id": "same", "header": "很" * 40, "question": "a?",
             "options": [{"label": "x"}, {"label": "y"}]},
            {"id": "same", "header": "h", "question": "b?",
             "options": [{"label": "x"}, {"label": "y"}]},
        ])
        self.assertEqual(err, "")
        self.assertEqual(len(qs[0]["header"]), 12)
        self.assertEqual(qs[0]["id"], "same")
        self.assertEqual(qs[1]["id"], "same_2")

    def test_allow_custom_can_be_disabled(self):
        qs, _ = normalize_questions([{
            "question": "q?", "allow_custom": False, "custom_label": "自定义",
            "options": [{"label": "a"}, {"label": "b"}],
        }])
        self.assertFalse(qs[0]["allow_custom"])
        self.assertEqual(qs[0]["custom_label"], "自定义")

    def test_answers_aligned_to_questions(self):
        qs, _ = normalize_questions(QUESTIONS)
        out = normalize_answers(qs, [
            {"question_id": "fmt", "selected": "HTML"},
            {"question_id": "color", "selected": ["绿色"], "custom_text": " 其他想法 "},
        ])
        # 结果按 questions 顺序对齐（不是按入参顺序），且每题都在
        self.assertEqual([a["question_id"] for a in out], ["color", "fmt"])
        self.assertEqual(out[0]["selected"], ["绿色"])
        self.assertEqual(out[0]["custom_text"], "其他想法")
        # 字符串形式的 selected 也要被接受（前端偶尔会塞单值）
        self.assertEqual(out[1]["selected"], ["HTML"])

    def test_answers_garbage_input_is_safe(self):
        qs, _ = normalize_questions(QUESTIONS)
        for garbage in (None, "x", 42, [None, 1, {"question_id": "color"}]):
            out = normalize_answers(qs, garbage)
            self.assertEqual([a["selected"] for a in out], [[], []])


class TestStatusOfResult(unittest.TestCase):
    """`status_of_result`：回放路径从 tool_result 文本反推结局徽标（2026-09-21）。

    为什么值得单独守：jsonl 不存 outcome，前端只读小结的徽标只能靠这份映射。
    它必须**与生成文案的三个常量同源**（都在 interaction 模块），否则改一句文案
    就会让回放徽标静默错位 —— 所以这里断言的是"与常量一致"，而不是硬编码字符串。
    """

    def test_matches_each_outcome_constant(self):
        # 必须走 normalize（format_answered 依赖规范化后的字段：multi_select / custom_label）
        qs, err = normalize_questions(QUESTIONS)
        self.assertEqual(err, "")
        self.assertEqual(status_of_result(format_answered(qs, normalize_answers(qs, []))),
                         OUTCOME_ANSWERED)
        self.assertEqual(status_of_result(CANCELLED_TEXT), OUTCOME_CANCELLED)
        self.assertEqual(status_of_result(STOPPED_TEXT), OUTCOME_STOPPED)

    def test_free_text_answer_counts_as_answered(self):
        # 用户直接在输入框打字（放弃选择题）也是对本次提问的作答
        self.assertEqual(status_of_result(format_free_text("就用 Markdown 吧")), OUTCOME_ANSWERED)

    def test_empty_result_is_incomplete(self):
        for empty in ("", None, "   \n "):
            self.assertEqual(status_of_result(empty), "incomplete")

    def test_blank_lines_before_text_do_not_confuse_it(self):
        # 落盘/读取过程可能引入前后空行，判定不能因此失效
        self.assertEqual(status_of_result("\n\n" + CANCELLED_TEXT + "\n"), OUTCOME_CANCELLED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
