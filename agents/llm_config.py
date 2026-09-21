#!/usr/bin/env python3
"""
llm_config.py - 大模型配置管理（v2：以「连接（供应商）」为中心）

文件布局
- ~/.aigent/llmconfig.json   用户维护的模型配置（含 api_key，权限 0600）
- ~/.aigent/providers.json   预置厂商公共元数据（DeepSeek / 硅基流动 …），
                             与用户配置解耦，便于随时整体更新

llmconfig.json v2 结构（连接 → 模型 两级）：

    {
      "version": 2,
      "active_model_id": "m_xxx",
      "connections": [
        { "id": "c_xxx",                       # 连接（= 一个「模型服务」）
          "provider": "deepseek",              # 预置 catalog key，或 "custom:<slug>"
          "name": "DeepSeek",                  # 展示名（可改）
          "base_url": "https://api.deepseek.com",
          "api_format": "chat_completions",
          "api_key": "sk-xxx",
          "models": [
            { "id": "m_xxx", "model": "deepseek-v4-flash",
              "display_name": "deepseek-v4-flash", "enabled": true,
              "tags": ["1M"], "context_in": "1M", "context_out": "",
              "capabilities": {"input": ["text"], "output": ["text"]},
              "capability_source": "auto" }
          ] }
      ]
    }

兼容：读到 v1（扁平 `models[]`，每条自带 provider/base_url/api_key）时按
(provider, base_url, api_key) 自动聚合迁移成 v2 并就地写回；`get_config()`
额外下发一份**扁平 models 视图**（每条带 connection_id / provider / base_url /
api_key），使输入区模型下拉、会话绑定、resolveModelMeta 等既有链路零改动。

启动语义（"没有配置时不加载"）与热切换语义保持不变，见旧版说明。
依赖方向：本模块单向 import config（仅取 AIGENT_HOME / CREDENTIALS_FILE）。
"""

import json
import os
import random
import string
import threading
from pathlib import Path

from config import AIGENT_HOME, CREDENTIALS_FILE
from logger import get_logger

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("llm_config")

# ~/.aigent/llmconfig.json（含 api_key，权限收紧到 0600）
LLM_CONFIG_FILE = AIGENT_HOME / "llmconfig.json"
# ~/.aigent/providers.json（预置厂商公共元数据，无密钥，可随官方更新覆盖）
PROVIDER_CATALOG_FILE = AIGENT_HOME / "providers.json"

CONFIG_VERSION = 2
# 出厂预置目录版本。提升它 = 声明"内置目录改版"，既有 providers.json 会按内置
# 重建（详见 load_provider_catalog）—— 修正既有模型的能力声明时必须提升，
# 否则文件里的旧值会把内置值永久压住。
CATALOG_VERSION = 2
DEFAULT_API_FORMAT = "chat_completions"

# API 格式选项（前端下拉展示；当前仅 chat_completions 参与运行）
API_FORMATS: list[dict] = [
    {"id": "chat_completions", "label": "Chat Completions (/chat/completions)"},
    {"id": "responses", "label": "Responses (/responses)"},
]

# ── 内置预置厂商目录（首次运行物化写出到 ~/.aigent/providers.json） ──────
# 本期仅 DeepSeek 与 硅基流动；新增厂商只需往该文件补一条（或改这里的默认值）。
# models[].capabilities 声明输入/输出能力（text/image/video/pdf），供 UI 展示；
# max_context(_extended) / thinking_strengths / default_thinking 供输入区悬浮面板用。
_DEFAULT_CATALOG: dict = {
    "version": CATALOG_VERSION,
    "providers": {
        "deepseek": {
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_format": DEFAULT_API_FORMAT,
            "api_key_env": "DEEPSEEK_API_KEY",
            "docs_url": "https://platform.deepseek.com/api_keys",
            "models": [
                # 图像理解：官方「模型 & 价格」表明确 deepseek-flash 支持、
                # deepseek-v4-pro 不支持（2026-09 核对）。
                {"id": "deepseek-flash", "display_name": "deepseek-flash",
                 "tags": ["1M", "图片"], "max_context": "128k", "max_context_extended": "1M",
                 "capabilities": {"input": ["text", "image"], "output": ["text"]},
                 "thinking_strengths": ["low", "high", "very_high"],
                 "default_thinking": "high"},
                # 已下线：官方说明 v4-flash / v4-flash-vision-exp 仍可调用，但请求由
                # DeepSeek-V4.1-Flash 承接并按 Flash 计费 —— 因此能力同 deepseek-flash。
                {"id": "deepseek-v4-flash", "display_name": "deepseek-v4-flash",
                 "tags": ["1M", "图片"], "max_context": "128k", "max_context_extended": "1M",
                 "capabilities": {"input": ["text", "image"], "output": ["text"]},
                 "thinking_strengths": ["low", "high", "very_high"],
                 "default_thinking": "high"},
                {"id": "deepseek-v4-pro", "display_name": "deepseek-v4-pro",
                 "tags": ["1M"], "max_context": "128k", "max_context_extended": "1M",
                 "capabilities": {"input": ["text"], "output": ["text"]},
                 "thinking_strengths": ["low", "high", "very_high"],
                 "default_thinking": "high"},
                {"id": "deepseek-v4-flash-vision-exp",
                 "display_name": "deepseek-v4-flash-vision-exp",
                 "tags": ["1M", "图片"], "max_context": "128k", "max_context_extended": "1M",
                 "capabilities": {"input": ["text", "image"], "output": ["text"]},
                 "thinking_strengths": ["low", "high", "very_high"],
                 "default_thinking": "high"},
            ],
        },
        "siliconflow": {
            "name": "硅基流动",
            "base_url": "https://api.siliconflow.cn/v1",
            "api_format": DEFAULT_API_FORMAT,
            "api_key_env": "SILICONFLOW_API_KEY",
            "docs_url": "https://cloud.siliconflow.cn/account/ak",
            "models": [
                {"id": "deepseek-ai/DeepSeek-V3", "display_name": "DeepSeek-V3",
                 "tags": ["1M"], "max_context": "128k", "max_context_extended": "1M",
                 "capabilities": {"input": ["text"], "output": ["text"]},
                 "thinking_strengths": ["high", "very_high"], "default_thinking": "high"},
                {"id": "deepseek-ai/DeepSeek-R1", "display_name": "DeepSeek-R1",
                 "tags": ["1M"], "max_context": "128k", "max_context_extended": "1M",
                 "capabilities": {"input": ["text"], "output": ["text"]},
                 "thinking_strengths": ["high", "very_high"], "default_thinking": "high"},
                {"id": "Qwen/Qwen2.5-72B-Instruct", "display_name": "Qwen2.5-72B-Instruct",
                 "tags": [], "max_context": "128k",
                 "capabilities": {"input": ["text"], "output": ["text"]},
                 "thinking_strengths": ["low", "high"], "default_thinking": "high"},
            ],
        },
    },
}


# ── 小工具 ─────────────────────────────────────────────────────────
_ALPHABET = string.ascii_lowercase + string.digits


def _gen_id(prefix: str) -> str:
    """生成短 id（连接 c_xxx / 模型 m_xxx）。"""
    return prefix + "".join(random.choices(_ALPHABET, k=8))


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


def normalize_base_url(base_url: str) -> str:
    """把「完整请求地址」归一到 OpenAI SDK 可用的 base_url。

    用户在 UI 里可能填 https://api.deepseek.com/chat/completions（Reasonix 那种
    完整地址），SDK 需要的是去掉具体路径的 base_url。这里统一裁掉尾部的
    /chat/completions、/responses、/models 等路径段。
    """
    u = str(base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses", "/embeddings", "/models"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u.rstrip("/")


def _empty_config() -> dict:
    return {"version": CONFIG_VERSION, "active_model_id": None, "connections": []}


# ── 预置厂商目录 ────────────────────────────────────────────────────

def _merge_provider(base: dict, override: dict) -> dict:
    """按「内置为底、文件为准」合并单个厂商；模型按 id 逐条合并。

    这样既能保留用户对某厂商端点/模型的修改，又能让内置目录新增的模型
    （官方上新）在旧 providers.json 上自动补进来，无需用户手改。
    """
    out = {**base, **override}
    override_models = {
        m.get("id"): m for m in (override.get("models") or []) if isinstance(m, dict)
    }
    merged, seen = [], set()
    # 先按内置顺序输出（文件值覆盖同 id 的内置值），再把文件新增的模型追加到末尾
    for m in base.get("models") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        merged.append({**m, **override_models.get(mid, {})})
        seen.add(mid)
    for mid, m in override_models.items():
        if mid not in seen:
            merged.append(m)
    out["models"] = merged
    return out


def load_provider_catalog() -> dict:
    """读取预置厂商目录；文件缺失时物化写出内置默认值（首次可用、可手工编辑）。

    合并策略按**目录版本**分两档：
    - 同版本：内置为底、文件逐条覆盖 —— 保留用户对 providers.json 的手工微调；
    - 版本落后（`CATALOG_VERSION` 被提升 = 出厂目录改版）：**内置值全量覆盖**，
      只保留文件里内置没有的厂商（用户自建的 custom:*）。

    为什么必须分档：`_merge_provider` 是「文件为准」，所以官方**修正既有模型**
    的能力声明时（不是新增模型），旧 providers.json 会把它永久压住；而且自愈
    回写也会失效 —— merge 结果恒等于文件，被判成"一致"不写回。没有版本闸门，
    改内置目录等于没改。
    """
    providers: dict = {}
    for key, val in _DEFAULT_CATALOG["providers"].items():
        providers[key] = json.loads(json.dumps(val))

    if PROVIDER_CATALOG_FILE.exists():
        try:
            stored = json.loads(PROVIDER_CATALOG_FILE.read_text(encoding="utf-8"))
            same_version = stored.get("version") == CATALOG_VERSION
            for key, val in (stored.get("providers") or {}).items():
                if not isinstance(val, dict):
                    continue
                if key not in providers:
                    providers[key] = val                    # 用户自建厂商：两档都保留
                elif same_version:
                    providers[key] = _merge_provider(providers[key], val)
                # 版本落后且内置已有该厂商 → 丢弃文件值，改用内置
            if not same_version:
                save_provider_catalog(providers)
                log.info("预置厂商目录版本 %s → %s，已按出厂目录重建 %s",
                         stored.get("version"), CATALOG_VERSION, PROVIDER_CATALOG_FILE)
            elif (stored.get("providers") or {}) != providers:
                # 自愈回写：文件被裁剪 / 内置上新时写回，使文件始终等于「生效中的
                # 目录」，便于用户直接查看与编辑。
                save_provider_catalog(providers)
                log.info("已更新预置厂商目录 %s", PROVIDER_CATALOG_FILE)
        except (json.JSONDecodeError, OSError):
            log.error("%s 解析失败，回退内置预置厂商目录", PROVIDER_CATALOG_FILE)
    else:
        try:
            save_provider_catalog(providers)
            log.info("已物化预置厂商目录到 %s", PROVIDER_CATALOG_FILE)
        except OSError as exc:
            log.error("写出预置厂商目录失败：%s", exc)

    return {"version": CATALOG_VERSION, "providers": providers}


def save_provider_catalog(providers: dict) -> dict:
    """把预置厂商目录落盘（无密钥，权限沿用默认）。"""
    payload = {"version": CATALOG_VERSION, "providers": providers or {}}
    PROVIDER_CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROVIDER_CATALOG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _lookup_preset(providers: dict, provider: str, model_id: str) -> dict:
    """在预置目录里查某厂商某模型的元数据（供迁移时补 capabilities/tags）。"""
    if provider in providers:
        for m in providers[provider].get("models") or []:
            if m.get("id") == model_id:
                return m
    return {}


# ── 归一化 / 迁移 ───────────────────────────────────────────────────

def _normalize_model(m: dict, catalog: dict, conn: dict | None = None) -> dict:
    """归一化单个模型条目，保留未知字段（向前兼容）。"""
    provider = (conn or {}).get("provider") or m.get("provider") or "custom"
    mid = str(m.get("model") or m.get("id") or "").strip()
    preset = _lookup_preset(catalog, provider, mid)

    out = dict(m)
    # 端点与密钥属于「连接」层级：模型条目不重复保存（v1 遗留字段在此清理），
    # 避免同一信息两处存放、改动后不一致。响应里的扁平视图由 _flatten() 补齐。
    for k in ("provider", "base_url", "api_key", "connection_id", "connection_name"):
        out.pop(k, None)
    out["id"] = str(m.get("id") or _gen_id("m_"))
    out["model"] = mid or out["id"]
    out["display_name"] = str(m.get("display_name") or preset.get("display_name") or out["model"])
    out["enabled"] = bool(m.get("enabled", True))

    # tags / 能力 / 上下文：用户显式值优先，缺省时从预置目录继承
    tags = m.get("tags")
    out["tags"] = list(tags) if isinstance(tags, list) else list(preset.get("tags") or [])
    source = str(m.get("capability_source") or ("auto" if preset else "manual"))
    preset_caps = preset.get("capabilities")
    stored_caps = m.get("capabilities")
    # capability_source 的语义（与前端 AddModelModal 的「自动识别 / 手动指定」对齐）：
    # - auto：能力是当初从预置目录**推断**出来的，每次加载都重新推导。否则首轮
    #   归一化写回的 capabilities 会永远压住预置值，providers.json「可随官方整体
    #   更新」的设计就失效了（官方改能力声明，既有安装收不到）。
    # - manual：用户在 UI 里显式指定过，一律尊重，绝不覆盖。
    if source == "auto" and isinstance(preset_caps, dict) and preset_caps:
        caps = preset_caps
    elif isinstance(stored_caps, dict) and stored_caps:
        caps = stored_caps
    else:
        caps = preset_caps if isinstance(preset_caps, dict) else None
    caps = caps or {"input": ["text"], "output": ["text"]}
    out["capabilities"] = {
        "input": list(caps.get("input") or ["text"]),
        "output": list(caps.get("output") or ["text"]),
    }
    out["capability_source"] = source

    # 上下文窗口：模型级 context_in/out 优先；兼容旧 advanced.context_in/out
    adv = m.get("advanced") if isinstance(m.get("advanced"), dict) else {}
    out["context_in"] = str(m.get("context_in") or adv.get("context_in") or "")
    out["context_out"] = str(m.get("context_out") or adv.get("context_out") or "")
    # 供输入区悬浮面板用的窗口/思考元数据（用户值优先，否则继承预置）
    for k in ("max_context", "max_context_extended", "thinking_strengths", "default_thinking"):
        if m.get(k) is not None:
            out[k] = m[k]
        elif preset.get(k) is not None:
            out[k] = preset[k]
    if adv:
        out["advanced"] = adv
    else:
        out.pop("advanced", None)
    return out


def _normalize_connection(c: dict, catalog: dict, version: int) -> dict:
    provider = str(c.get("provider") or "custom")
    preset = catalog.get(provider) or {}
    name = str(c.get("name") or preset.get("name") or provider)
    base_url = str(c.get("base_url") or preset.get("base_url") or "")
    out = {
        "id": str(c.get("id") or _gen_id("c_")),
        "provider": provider,
        "name": name,
        "base_url": base_url,
        "api_format": str(c.get("api_format") or preset.get("api_format") or DEFAULT_API_FORMAT),
        "api_key": str(c.get("api_key") or ""),
        "models": [
            _normalize_model(m, catalog, {"provider": provider})
            for m in (c.get("models") or [])
            if isinstance(m, dict)
        ],
    }
    if isinstance(c.get("compat"), dict) and c["compat"]:
        out["compat"] = c["compat"]
    if version >= CONFIG_VERSION:
        out["custom"] = bool(c.get("custom", False))
    return out


def _migrate_v1_connections(raw: dict, catalog: dict) -> list[dict]:
    """v1（扁平 models）→ v2 connections：按 (provider, base_url, api_key) 聚合。"""
    flat = [m for m in (raw.get("models") or []) if isinstance(m, dict)]
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for m in flat:
        key = (m.get("provider") or "custom", m.get("base_url") or "", m.get("api_key") or "")
        if key not in groups:
            provider = key[0]
            preset = catalog.get(provider) or {}
            groups[key] = {
                "id": _gen_id("c_"),
                "provider": provider,
                "name": preset.get("name") or provider,
                "base_url": key[1] or preset.get("base_url") or "",
                "api_format": preset.get("api_format") or DEFAULT_API_FORMAT,
                "api_key": key[2],
                "models": [],
                "custom": provider not in catalog,
            }
            order.append(key)
        groups[key]["models"].append(m)
    return [groups[k] for k in order]


def ensure_v2(raw: dict, catalog: dict) -> list[dict]:
    """把任意版本的配置归一化成 connections 列表（内存态，不落盘）。"""
    if not isinstance(raw, dict):
        return []
    if raw.get("connections"):
        return [
            _normalize_connection(c, catalog, CONFIG_VERSION)
            for c in raw["connections"]
            if isinstance(c, dict)
        ]
    return [
        _normalize_connection(c, catalog, CONFIG_VERSION)
        for c in _migrate_v1_connections(raw, catalog)
    ]


def _flatten(connections: list[dict]) -> list[dict]:
    """connections → 扁平 models 视图（每条补 connection_id/provider/base_url/api_key）。

    这是历史链路（输入区下拉、会话绑定、resolveModelMeta、apply_to_env）读取的形状。
    """
    out: list[dict] = []
    for c in connections:
        for m in c.get("models") or []:
            item = dict(m)
            item["connection_id"] = c.get("id")
            item["provider"] = c.get("provider")
            item["connection_name"] = c.get("name")
            item["base_url"] = c.get("base_url")
            item["api_key"] = c.get("api_key")
            out.append(item)
    return out


def _read_raw() -> dict:
    if not LLM_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_normalized() -> dict:
    """读盘 → 归一化（含 v1 自动迁移）→ 返回 {active_model_id, connections, models}。"""
    raw = _read_raw()
    catalog = load_provider_catalog()["providers"]
    connections = ensure_v2(raw, catalog)
    return {
        "active_model_id": raw.get("active_model_id"),
        "connections": connections,
        "models": _flatten(connections),
    }


# ── 对外接口 ────────────────────────────────────────────────────────

def get_config() -> dict:
    """读取配置并附预置厂商目录，供前端渲染。

    返回 {version, active_model_id, connections, models(扁平视图), providers, api_formats}。
    """
    data = _load_normalized()
    data["version"] = CONFIG_VERSION
    data["providers"] = load_provider_catalog()["providers"]
    data["api_formats"] = API_FORMATS
    return data


def save_config(data: dict) -> dict:
    """持久化模型配置到 llmconfig.json（v2 结构，权限 0600）。

    兼容 v1 输入（扁平 models）：统一归一化成 connections 后落盘。
    允许删光所有模型（connections 为空 → active_model_id 置 None）。
    """
    catalog = load_provider_catalog()["providers"]
    connections = ensure_v2(data or {}, catalog)
    flat = _flatten(connections)

    active_id = (data or {}).get("active_model_id")
    ids = {m.get("id") for m in flat}
    if active_id not in ids:
        active_id = None
        enabled = [m for m in flat if m.get("enabled")]
        if enabled:
            active_id = enabled[0].get("id")
        elif flat:
            active_id = flat[0].get("id")

    payload = {
        "version": CONFIG_VERSION,
        "active_model_id": active_id,
        "connections": connections,
    }
    LLM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LLM_CONFIG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(LLM_CONFIG_FILE, 0o600)
    return payload


def apply_to_env(data: dict) -> dict:
    """把配置映射进环境变量（OPENAI_API_KEY/BASE_URL/MODEL_ID/FALLBACK_MODEL_ID）。

    接受 v2（connections）或 v1（扁平 models）输入；直接赋值（用户显式模型配置是
    权威来源）。返回生效摘要。
    """
    catalog = load_provider_catalog()["providers"]
    connections = ensure_v2(data or {}, catalog)
    flat = _flatten(connections)
    models = [m for m in flat if m.get("enabled")]
    if not models:
        # 全部删除/停用时清掉模型绑定 env，避免已删除的模型继续在运行期生效
        # （key/base_url 保留：密钥与端点不随删模型丢失）
        os.environ.pop("OPENAI_MODEL_ID", None)
        os.environ.pop("FALLBACK_MODEL_ID", None)
        return {"applied": False, "primary": None, "fallback": None,
                "reason": "无启用模型，LLM 绑定已清空"}
    active_id = (data or {}).get("active_model_id")
    primary = next((m for m in models if m.get("id") == active_id), models[0])
    fallback = next((m for m in models if m.get("id") != primary.get("id")), None)

    os.environ["OPENAI_BASE_URL"] = normalize_base_url(str(primary.get("base_url") or ""))
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
        if str(adv.get("tool_rounds") or "").strip():
            os.environ["OPENAI_TOOL_ITERATIONS"] = str(adv["tool_rounds"]).strip()
        thinking = str(adv.get("thinking") or "").strip()
        if thinking in ("default", "enabled", "disabled"):
            os.environ["OPENAI_THINKING_MODE"] = thinking

    # 上下文窗口：模型级 context_in/out（新 UI）优先
    out_tokens = _parse_tokens(primary.get("context_out"))
    if out_tokens:
        os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = str(out_tokens)
    in_tokens = _parse_tokens(primary.get("context_in"))
    if in_tokens:
        os.environ["OPENAI_CONTEXT_WINDOW_IN"] = str(in_tokens)

    # 模型能力声明 → 图片输入标记（声明存在才写，避免误覆盖）
    caps = primary.get("capabilities")
    if isinstance(caps, dict) and caps.get("input") is not None:
        inputs = caps.get("input") or []
        os.environ["OPENAI_IMAGE_INPUT"] = "1" if "image" in inputs else "0"

    return {
        "applied": True,
        "primary": primary.get("display_name") or primary.get("id"),
        "fallback": (fallback.get("display_name") or fallback.get("id"))
        if fallback else None,
    }


def load_llm_config() -> dict:
    """启动/重载时调用：读文件并映射进 env。文件不存在时不加载（返回空）。

    读到 v1 结构时自动迁移并写回 v2（一次性，幂等）。
    """
    if not LLM_CONFIG_FILE.exists():
        return {}
    raw = _read_raw()
    if not raw:
        log.error("%s 解析失败，跳过模型加载", LLM_CONFIG_FILE)
        return {}
    data = get_config()
    # v1 → v2 就地升级：写回迁移结果，后续读写都走新结构
    if not raw.get("connections") and raw.get("models"):
        try:
            save_config({"active_model_id": raw.get("active_model_id"),
                         "connections": data["connections"]})
            log.info("已把 v1 模型配置迁移为 v2（%d 个连接）",
                     len(data["connections"]))
        except OSError as exc:
            log.error("v1→v2 迁移写回失败（内存内仍可用）：%s", exc)
    apply_to_env(data)
    log.info("已加载模型配置 %s：%s",
             LLM_CONFIG_FILE, os.environ.get("OPENAI_MODEL_ID", ""))
    return data


def hint_if_missing_key() -> None:
    """密钥缺失时的引导提示（复用 credentials 的报错文案习惯）。"""
    if not os.environ.get("OPENAI_API_KEY"):
        log.info(
            "未配置 API Key：请在 %s 填入 OPENAI_API_KEY，"
            "或在设置中添加模型，或设置同名环境变量", CREDENTIALS_FILE
        )


# ── 远端模型列表（「刷新」按钮） ─────────────────────────────────────

def fetch_remote_models(base_url: str, api_key: str = "", connection_id: str = "",
                        models_path: str = "/models", timeout: int = 20) -> dict:
    """调 GET {base_url}{models_path} 拉取该服务商的模型 id 列表（「刷新」按钮）。

    - api_key 留空时回退到已保存配置里 connection_id 对应的密钥（前端未改密钥场景）
    - base_url 允许传完整请求地址（自动裁掉 /chat/completions 等）
    - models_path 来自「兼容设置」，默认 /models；个别中转服务路径不同时可改
    返回 {"ok": bool, "models": [{"id":...}], "error": str?}
    """
    base = normalize_base_url(base_url)
    key = str(api_key or "").strip()
    if (not base or not key) and connection_id:
        cfg = _load_normalized()
        conn = next((c for c in cfg["connections"] if c.get("id") == connection_id), None)
        if conn:
            base = base or normalize_base_url(conn.get("base_url") or "")
            key = key or str(conn.get("api_key") or "")

    if not base:
        return {"ok": False, "models": [], "error": "缺少 API 地址"}
    if not key:
        return {"ok": False, "models": [], "error": "缺少 API Key"}

    path = str(models_path or "/models").strip() or "/models"
    if not path.startswith("/"):
        path = "/" + path
    url = base + path

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - 读取错误体失败不影响主流程
            pass
        return {"ok": False, "models": [], "error": f"HTTP {exc.code} {detail}".strip()}
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一回前端展示
        return {"ok": False, "models": [], "error": f"{type(exc).__name__}: {exc}"}

    items = payload.get("data") if isinstance(payload, dict) else payload
    ids = set()
    for item in items or []:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
            if mid:
                ids.add(str(mid))
        elif isinstance(item, str):
            ids.add(item)
    return {"ok": True, "base_url": base, "models": [{"id": i} for i in sorted(ids)]}


# ── 每会话独立绑定模型：env 换绑临界区 ────────────────────────────────
# LLMClient 在 __init__ 一次性捕获 api_key/base_url（llm_manage.create_llm），
# run_turn 走已固化的实例、不再读 env。因此只需对「swap 会话模型 env →
# 构造/重载 Agent」这段加全局锁，并事后恢复为全局快照，即可让每个会话
# 独立绑定自己的模型，且不污染其它并发线程的全局 env。
ENV_LLM_LOCK = threading.Lock()

# apply_to_env() 会写入的 LLM 相关 env 键集合（快照/恢复范围与此一致）
_LLM_ENV_KEYS = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL_ID",
    "FALLBACK_MODEL_ID", "OPENAI_TEMPERATURE", "OPENAI_TOP_P",
    "OPENAI_TOP_K", "OPENAI_MAX_OUTPUT_TOKENS", "OPENAI_TOOL_ITERATIONS",
    "OPENAI_THINKING_MODE", "OPENAI_CONTEXT_WINDOW_IN", "OPENAI_IMAGE_INPUT",
)


def snapshot_llm_env() -> dict:
    """捕获当前 LLM env 键现值，供 build_agent 换绑后恢复。"""
    return {k: os.environ.get(k) for k in _LLM_ENV_KEYS}


def restore_llm_env(snap: dict) -> None:
    """按快照恢复 LLM env；快照中不存在的键则剔除。"""
    for k in _LLM_ENV_KEYS:
        val = snap.get(k)
        if val is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = val


def apply_model_to_env(model_id: str | None) -> bool:
    """把指定模型（llmconfig.json 中某个条目）临时映射进 env。

    返回 True 表示 env 已被该模型的配置覆盖（含高级设置）；返回 False 表示
    未改动 env（model_id 为空 / 文件缺失或损坏 / 未找到该模型），此时调用方
    应沿用当前全局 env 构造 Agent。必须配合 snapshot/restore 在临界区内使用。
    """
    if not model_id:
        return False
    data = _load_normalized()
    if not any(m.get("id") == model_id for m in data["models"]):
        return False
    # 临时数据：克隆全部模型并启用，使 apply_to_env 能选中目标模型、推导 fallback
    ephemeral = {
        "active_model_id": model_id,
        "connections": [
            {**c, "models": [dict(m, enabled=True) for m in c.get("models") or []]}
            for c in data["connections"]
        ],
    }
    apply_to_env(ephemeral)
    return True


def get_model_by_id(model_id: str | None) -> dict | None:
    """按 id 查归一化模型条目（llmconfig.json v2）。

    id 为空（会话绑定的是全局 active 模型，会话元数据 model_id 为 None）时
    回落全局 active 模型条目；模型未配置/找不到返回 None。
    供轮级 model_info 快照、会话上下文窗口解析等按 id 反查模型元数据
    （display_name / max_context / default_thinking 等）的场景使用。
    """
    data = _load_normalized()
    if not model_id:
        model_id = data.get("active_model_id")
    if not model_id:
        return None
    for m in data.get("models") or []:
        if m.get("id") == model_id:
            return m
    return None


def caps_allow_image(caps) -> bool:
    """能力声明是否允许图片输入 —— **纯函数**，只吃 capabilities 那一小块。

    三态（与前端 `modelSupportsImage`、`ws_bridge._model_supports_image` 同口径）：
    - 明确声明 input 含 image → True；
    - 明确声明了 input 列表但不含 image → False（应把图片降级为占位）；
    - 元数据缺失 / 形状不认识 → True。

    最后一条是刻意的：本地目录可能没收录用户新加的模型，**不能因为"我们不知道"
    就当作不支持** —— 宁可让 provider 回一个真实错误，也不要本地静默吞掉图片。

    单独抽出来是为了让 ws_bridge 与引擎共用同一套规则，而各自保留自己的
    `get_model_by_id` 调用点（ws_bridge 的测试在该名字上打桩）。
    """
    if not isinstance(caps, dict):
        return True
    inputs = caps.get("input")
    if not isinstance(inputs, list) or not inputs:
        return True
    return "image" in inputs


def model_supports_image(model_id: str | None) -> bool:
    """按模型条目 id（m_xxx）查图片能力；空 id 回落全局 active 模型。

    读配置失败一律按"支持"处理（发送边界的调用方依赖本函数不抛异常）。
    """
    try:
        model = get_model_by_id(model_id)
    except Exception as exc:  # noqa: BLE001 - 能力查询失败不阻断对话
        log.warning("读取模型能力失败（按支持图片处理）: %s: %s",
                    type(exc).__name__, exc)
        return True
    if not isinstance(model, dict):
        return True
    return caps_allow_image(model.get("capabilities"))


def resolve_model_window(model_id: str | None, extended: bool = False) -> str | None:
    """按模型元数据解析上下文窗口字符串（如 "128k" / "1M"）。

    extended=True 且模型声明了扩展窗口时取扩展值，否则取标准窗口；
    模型未配置/无窗口声明返回 None（调用方回落全局默认）。
    统计与压缩阈值以「所选模型的真实窗口」为准，避免全局 env
    MAX_CONTEXT_TOKENS 与模型实际窗口不符导致的误统计。
    """
    m = get_model_by_id(model_id)
    if not m:
        return None
    if extended:
        return str(m.get("max_context_extended") or "").strip() or None
    return str(m.get("max_context") or "").strip() or None
