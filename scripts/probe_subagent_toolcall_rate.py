#!/usr/bin/env python3
"""判定：同一个请求重复 N 次，"模型不产出工具调用"的比例有多高。

背景：session_4 的第二次调用（历史 = system/mem/env/user/assistant(tool_calls)/tool/tool）
返回了 content + reasoning 但 **零 tool_calls**，于是主循环判定"模型想停"，
turn 直接结束 —— 表现为"说要派子智能体，然后就没有下文"。

本脚本把该请求原样重复 N 次，统计：
  - finish_reason 分布
  - 出现 tool_calls 的比例 / 空 tool_calls 的比例
  - 空 tool_calls 时 content / reasoning 的形状

用法（仓库根目录）：
    .venv/bin/python scripts/probe_subagent_toolcall_rate.py [N]
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from config import load as load_config          # noqa: E402
load_config()
from llm_config import load_llm_config           # noqa: E402
load_llm_config()

from llm_manage import LLMClient                 # noqa: E402
from streaming_client import streamed_create     # noqa: E402
from tools import ToolRegistry                   # noqa: E402

SESSION = ("/Users/peijiping/.aigent/projects/default/.chathistory/session_4.jsonl")


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    msgs = [json.loads(x) for x in Path(SESSION).read_text(encoding="utf-8").splitlines() if x.strip()]
    msgs = msgs[:-1]  # 去掉那条"空 tool_calls"的结尾消息 → 回到派发前的一帧
    reg = ToolRegistry()
    tools = reg.build_agent_tools(team_mode=False)
    llm = LLMClient().llm

    verdicts, frs, empties = [], Counter(), []
    for i in range(1, n + 1):
        msg, fr, usage = streamed_create(
            llm, sinks=[],
            model=os.environ.get("OPENAI_MODEL_ID", ""),
            messages=msgs, tools=tools, tool_choice="auto",
            parallel_tool_calls=True, max_tokens=8000, temperature=0.5,
            reasoning_effort="high", extra_body={"thinking": {"type": "enabled"}},
        )
        names = [tc.function.name for tc in (msg.tool_calls or [])]
        frs[fr] += 1
        ok = bool(names)
        if not ok:
            dbg = json.loads(json.dumps(msg.model_dump(), ensure_ascii=False))
            empties.append(dbg)
        verdicts.append(f"#{i} finish={fr!r} tools={names or '∅'} "
                        f"content={len(msg.content or '')} reasoning={len(msg.reasoning_content or '')} "
                        f"usage={usage.get('total_tokens')}")
        print(verdicts[-1], flush=True)

    print("\n════════ 汇总 ════════")
    print("finish_reason 分布:", dict(frs))
    print(f"有 tool_calls: {sum(1 for v in verdicts if '∅' not in v)}/{n}")
    print(f"零 tool_calls : {len(empties)}/{n}  ← 每次都会让 turn 直接结束（用户看到的“不往下执行”）")
    for j, d in enumerate(empties, 1):
        print(f"\n--- 空 tool_calls 样本 {j} ---")
        print("content:", d.get("content"))
        print("reasoning:", (d.get("reasoning_content") or "")[-400:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
