#!/usr/bin/env python3
"""
llm_config.py - 大模型配置文件管理（~/.aigent/llmconfig.json）

从非敏感的 config.json 独立出来，单独存储多家服务商的大模型配置（含
api_key，权限 0600）。支持维护多个厂家的多个模型。

启动语义（"没有配置时不加载"）：
- 文件不存在 → load_llm_config() 直接返回空，不碰环境变量，LLM 走原有
  config.json / credentials.json / .env 兜底。
- 文件存在   → 把「启用且（激活或被选中）的模型」映射进
  OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL_ID / FALLBACK_MODEL_ID，
  以及该模型的高级设置（OPENAI_TEMPERATURE / OPENAI_TOP_P / OPENAI_TOP_K /
  OPENAI_MAX_OUTPUT_TOKENS / OPENAI_TOOL_ITERATIONS / OPENAI_THINKING_MODE /
  OPENAI_CONTEXT_WINDOW_IN / OPENAI_IMAGE_INPUT，均可选，未配置即清位走默认）。
  因为是「用户显式配置的大模型」，直接赋值（不 setdefault），保证相对
  .env / config.json 是权威来源。

热切换（"加模型/改密钥不重启"）：
  保存配置 → save_config() 落盘 → load_llm_config() 重新映射进 env →
  Agent.reload_llm_bindings() 就地重建 LLM 绑定 → 立即生效，无需重启。

依赖方向：本模块单向 import config（仅取 AIGENT_HOME / CREDENTIALS_FILE）。
config.py 不 import 本模块（避免循环依赖），由各入口点调用 load_llm_config()。
"""

import json
import os
from pathlib import Path

from config import AIGENT_HOME, CREDENTIALS_FILE

# ~/.aigent/llmconfig.json（含 api_key，权限收紧到 0600）
LLM_CONFIG_FILE = AIGENT_HOME / "llmconfig.json"

# ── 预置服务商（本期仅 DeepSeek 与 硅基流动，后续扩展） ──────────────
# base_url 为 OpenAI 兼容端点；models 是给「选择模型」下拉的候选，允许自定义输入。
# tags 为模型能力/上下文标签（前端下拉里展示，如 1M / 图片），仅作展示不参与运行逻辑。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": [
            {"id": "deepseek-v4-flash", "display_name": "deepseek-v4-flash",
             "tags": ["1M"]},
            {"id": "deepseek-v4-pro", "display_name": "deepseek-v4-pro",
             "tags": ["1M"]},
            {"id": "deepseek-v4-flash-vision-exp", "display_name": "deepseek-v4-flash-vision-exp",
             "tags": ["1M", "图片"]},
        ],
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            {"id": "deepseek-ai/DeepSeek-V3", "display_name": "DeepSeek-V3"},
            {"id": "deepseek-ai/DeepSeek-R1", "display_name": "DeepSeek-R1"},
            {"id": "Qwen/Qwen2.5-72B-Instruct", "display_name": "Qwen2.5-72B-Instruct"},
        ],
    },
}


def _empty_config() -> dict:
    return {"active_model_id": None, "models": []}


def _parse_tokens(raw) -> int | None:
    """把 '1M' / '128k' / '8000' 这类窗口值解析成 token 数（k=1000、M=1000_000）。
    纯数字直接取整；空串/非法返回 None（表示「未配置，走默认」）。"""
    s = str(raw or "").strip().upper()
    if not s:
        return None
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        val = float(s)
    except ValueError:
        return None
    if val <= 0:
        return None
    return int(val * mult)


def get_config() -> dict:
    """读取 llmconfig.json；不存在时返回空结构。
    附带预置服务商信息（providers）供前端渲染 添加模型 选择面板。"""
    data = _empty_config()
    if LLM_CONFIG_FILE.exists():
        try:
            stored = json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stored = {}
        data["active_model_id"] = stored.get("active_model_id")
        data["models"] = stored.get("models", []) or []
    data["providers"] = PROVIDERS
    return data


def save_config(data: dict) -> dict:
    """持久化模型配置到 llmconfig.json（权限 0600）。
    返回规范化后的配置；异常（JSON 非法）抛 ValueError。
    允许删光所有模型（models 为空 → active_model_id 置 None），运行时走未配置态。"""
    models = [m for m in data.get("models", []) if m and isinstance(m, dict)]
    active_id = data.get("active_model_id")
    if models:
        if active_id and not any(m.get("id") == active_id for m in models):
            active_id = models[0].get("id")
    else:
        active_id = None
    payload = {"active_model_id": active_id, "models": models}
    LLM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LLM_CONFIG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(LLM_CONFIG_FILE, 0o600)
    return payload


def apply_to_env(data: dict) -> dict:
    """把配置映射进环境变量（OPENAI_API_KEY/BASE_URL/MODEL_ID/FALLBACK_MODEL_ID）。
    直接赋值，作为用户显式模型配置的权威来源。返回生效摘要。"""
    models = [m for m in data.get("models", []) if m.get("enabled")]
    if not models:
        # 全部删除/停用时清掉模型绑定 env，避免已删除的模型继续在运行期生效
        # （key/base_url 保留：密钥与端点不随删模型丢失）
        os.environ.pop("OPENAI_MODEL_ID", None)
        os.environ.pop("FALLBACK_MODEL_ID", None)
        return {"applied": False, "primary": None, "fallback": None,
                "reason": "无启用模型，LLM 绑定已清空"}
    active_id = data.get("active_model_id")
    primary = next((m for m in models if m.get("id") == active_id), models[0])
    fallback = next((m for m in models if m.get("id") != primary.get("id")), None)

    os.environ["OPENAI_BASE_URL"] = str(primary.get("base_url") or "")
    os.environ["OPENAI_MODEL_ID"] = str(primary.get("model") or primary.get("id") or "")
    # 密钥为空时不动 env（避免用空串覆盖当前可用密钥，导致运行期 key 被清空）
    if primary.get("api_key"):
        os.environ["OPENAI_API_KEY"] = str(primary.get("api_key"))
        applied_key = True
    else:
        applied_key = os.environ.get("OPENAI_API_KEY") or ""
    if fallback:
        os.environ["FALLBACK_MODEL_ID"] = str(
            fallback.get("model") or fallback.get("id") or ""
        )
    else:
        os.environ.pop("FALLBACK_MODEL_ID", None)

    # ── 高级设置（可选）→ env，供 Agent 实例合成调用参数 ──────────────
    # 先全部清除旧值再按需写入：切换主模型时避免上一个模型的高级配置残留
    for key in ("OPENAI_TEMPERATURE", "OPENAI_TOP_P", "OPENAI_TOP_K",
                "OPENAI_MAX_OUTPUT_TOKENS", "OPENAI_TOOL_ITERATIONS",
                "OPENAI_THINKING_MODE", "OPENAI_CONTEXT_WINDOW_IN",
                "OPENAI_IMAGE_INPUT"):
        os.environ.pop(key, None)
    adv = primary.get("advanced") or {}
    if isinstance(adv, dict):
        if str(adv.get("temperature") or "").strip():
            os.environ["OPENAI_TEMPERATURE"] = str(adv["temperature"]).strip()
        if str(adv.get("top_p") or "").strip():
            os.environ["OPENAI_TOP_P"] = str(adv["top_p"]).strip()
        if str(adv.get("top_k") or "").strip():
            os.environ["OPENAI_TOP_K"] = str(adv["top_k"]).strip()
        out_tokens = _parse_tokens(adv.get("context_out"))
        if out_tokens:
            os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = str(out_tokens)
        in_tokens = _parse_tokens(adv.get("context_in"))
        if in_tokens:
            os.environ["OPENAI_CONTEXT_WINDOW_IN"] = str(in_tokens)
        if str(adv.get("tool_rounds") or "").strip():
            os.environ["OPENAI_TOOL_ITERATIONS"] = str(adv["tool_rounds"]).strip()
        thinking = str(adv.get("thinking") or "").strip()
        if thinking in ("default", "enabled", "disabled"):
            os.environ["OPENAI_THINKING_MODE"] = thinking
        image_input = str(adv.get("image_input") or "").strip()
        if image_input in ("yes", "no"):
            os.environ["OPENAI_IMAGE_INPUT"] = "1" if image_input == "yes" else "0"

    return {
        "applied": True,
        "primary": primary.get("display_name") or primary.get("id"),
        "fallback": (fallback.get("display_name") or fallback.get("id"))
        if fallback else None,
    }


def load_llm_config() -> dict:
    """启动/重载时调用：读文件并映射进 env。文件不存在时不加载（返回空）。"""
    if not LLM_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 损坏的配置文件不致命：打印提示但继续走原有兜底，不拦截启动
        print(f"[llm_config] {LLM_CONFIG_FILE} 解析失败，跳过模型加载")
        return {}
    data["providers"] = PROVIDERS
    apply_to_env(data)
    print(
        f"[llm_config] 已加载模型配置 {LLM_CONFIG_FILE}："
        f"{os.environ.get('OPENAI_MODEL_ID', '')}"
    )
    return data


def hint_if_missing_key() -> None:
    """密钥缺失时的引导提示（复用 credentials 的报错文案习惯）。"""
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            f"[llm_config] 未配置 API Key：请在 {CREDENTIALS_FILE} 填入 OPENAI_API_KEY，"
            "或在设置中添加模型，或设置同名环境变量"
        )