#!/usr/bin/env python3
"""
web_tools.py - 联网工具（DeepSeek 网页搜索 / 抓取）

把 DeepSeek 的 **Search 接口**与 **Fetch 接口** 封装成两个可调函数，供
`tools.py` 的 `web_search` / `web_fetch` 工具调用。

    指南：https://api-docs.deepseek.com/zh-cn/guides/search/
    Search：POST {base}/v1/tools/search   按 query 搜索网页，返回结构化结果
    Fetch ：POST {base}/v1/tools/fetch    抓取单个网页，以 Markdown 返回正文

**为什么键名带 DeepSeek 前缀**：本 Agent 支持多家模型的 OpenAI/Anthropic 兼容
端点，主模型未必是 DeepSeek，故模型侧的 `OPENAI_API_KEY` 不能假定为 DeepSeek 的
Key。这两个联网工具是 DeepSeek 平台侧的能力（计价按请求次数，与 token 无关），
因此独立配置一份 Key（默认值取用户的 DeepSeek Key 即可）。

配置（全部走 `os.environ.get(...)`，来源见 config.py 的加载链；
密钥写 `~/.aigent/config/credentials.json`，参数写 `config.json`）：
    DEEPSEEK_SEARCH_API_KEY  Bearer Key（Search + Fetch 共用；缺失时工具返回 Error 文案）
    DEEPSEEK_SEARCH_BASE_URL 端点基址，默认 https://api.deepseek.com
    DEEPSEEK_SEARCH_TIMEOUT  单次请求超时（秒），默认 30

**契约**：两个函数**永远返回字符串、绝不抛异常**（与 tools.py 工具层铁律一致）——
网络/鉴权/限流/空结果都收敛成 `Error: ...`（或明确空结果文案）回给模型，绝不打死整轮。

本模块是**叶子模块**：只依赖标准库 + httpx + logger，不 import config/paths，
与 mcp_market.py 同构（运行时直接读 env，配置由启动期 config.load() 灌入）。
"""

from __future__ import annotations

import os

import httpx

from logger import get_logger

log = get_logger("web_tools")

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

# 结果渲染上限（字符）。工具结果会进模型上下文，必须封顶：
#   - 搜索：count 最多 50，每条 summary 约 600~800 字，全量可到 4 万字 → 截到 1.2 万；
#   - 抓取：正文可能极长 → 截到 3 万（与 attachments 的 ATTACHMENT_TEXT_MAX_CHARS 同口径）。
# 截断是**明确告知**的（末尾附提示），不静默丢内容。
_SEARCH_MAX_CHARS = 12000
_FETCH_MAX_CHARS = 30000
_SUMMARY_MAX_CHARS = 600
# 错误响应的 message 一般很短，读一点点即可；避免把整页 HTML 塞进上下文。
_ERROR_BODY_MAX_CHARS = 500


# ═══════════════════════════════════════════════════════════════════════
#  配置读取
# ═══════════════════════════════════════════════════════════════════════

def api_key() -> str:
    """DeepSeek API Key（Search + Fetch 共用）。未配置返回空串。"""
    return (os.environ.get("DEEPSEEK_SEARCH_API_KEY") or "").strip()


def base_url() -> str:
    """端点基址（不含尾部斜杠），默认官方地址。"""
    return (os.environ.get("DEEPSEEK_SEARCH_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def timeout() -> float:
    try:
        return float(os.environ.get("DEEPSEEK_SEARCH_TIMEOUT") or "30")
    except (TypeError, ValueError):
        return 30.0


def configured() -> bool:
    """是否已配置 Key（供调用方/文档判断工具是否可用）。"""
    return bool(api_key())


# ═══════════════════════════════════════════════════════════════════════
#  内部工具
# ═══════════════════════════════════════════════════════════════════════

def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文 {len(text)} 字）"


# 各 HTTP 状态码 → 给模型看的中文说明（对齐官方错误表）。
_STATUS_HINT = {
    400: "请求参数不合法",
    401: "API Key 缺失或无效，请检查 ~/.aigent/config/credentials.json 里的 DEEPSEEK_SEARCH_API_KEY",
    402: "账号余额不足",
    403: "该请求被安全策略拒绝",
    404: "未能获取到可用的网页内容（URL 不可用 / 被站点拦截 / 内容类型不支持 / 无可提取正文）",
    408: "读取请求体超时",
    413: "请求体超过大小限制",
    429: "超过 QPS 限速（Search / Fetch 各 5 QPS），请稍后重试",
    500: "DeepSeek 服务端内部错误",
    504: "下游请求超时",
}


def _post(path: str, payload: dict) -> tuple[dict | None, str | None]:
    """POST 一个 JSON 请求；成功返回 `(data, None)`，失败返回 `(None, error_text)`。

    `error_text` 已按"给模型看的可操作说明"措辞（含状态码与官方 message）。
    除 `Error: ...` 外**绝不抛异常**。
    """
    key = api_key()
    if not key:
        return None, (
            "Error: 未配置 DeepSeek 联网 API Key（DEEPSEEK_SEARCH_API_KEY）。"
            "请在 ~/.aigent/config/credentials.json 中填入你的 DeepSeek Key 后重试"
            "（Search 与 Fetch 共用同一把 Key）。"
        )
    url = base_url() + path
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout(), follow_redirects=True) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        return None, f"Error: 联网请求超时（>{timeout():.0f}s）：{url}"
    except httpx.HTTPError as e:
        return None, f"Error: 联网请求失败（{type(e).__name__}: {e}）"

    if resp.status_code == 200:
        try:
            return resp.json(), None
        except ValueError:
            return None, "Error: 联网接口返回了非 JSON 内容（可能是网关/代理异常）"

    hint = _STATUS_HINT.get(resp.status_code, "请求失败")
    msg = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
    except ValueError:
        msg = _clip(resp.text, _ERROR_BODY_MAX_CHARS).strip()
    detail = f"：{msg}" if msg else ""
    return None, f"Error: 联网接口返回 HTTP {resp.status_code}（{hint}）{detail}"


def _clip_total(lines: list[str], limit: int) -> str:
    """把多行拼成一个字符串并按上限截断，截断处附明确提示。"""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（输出已截断至 {limit} 字，如需更多请减少 count 或分多次搜索）"


# ═══════════════════════════════════════════════════════════════════════
#  web_search → DeepSeek Search 接口
# ═══════════════════════════════════════════════════════════════════════

def search(query: str, count: int | None = None,
           need_content: bool = False, ori_question: str | None = None) -> str:
    """按 query 搜索网页，返回结构化结果文本。

    `count` 会被钳到 1~50（缺省 10）；`need_content=True` 时随附网页正文
    （正文很长，会显著占用上下文，仅在需要细读时开启）。
    `ori_question` 是触发搜索的用户原始问题，有助于理解搜索意图（可选）。
    """
    q = str(query or "").strip()
    if not q:
        return "Error: web_search 需要非空的 query。"
    if len(q) > 128:
        return f"Error: query 超过 128 个字符（当前 {len(q)} 个），请精简后重试。"
    try:
        n = int(count) if count is not None else 10
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(50, n))

    payload: dict = {"query": q, "count": n, "need_content": bool(need_content)}
    if isinstance(ori_question, str) and ori_question.strip():
        payload["ori_question"] = ori_question.strip()[:128]

    data, err = _post(SEARCH_PATH, payload)
    if err is not None:
        return err
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return f"未搜索到结果（query: {q}）。可尝试更换关键词或放宽搜索语法。"

    lines = [f"共 {len(results)} 条结果（query: {q}）"]
    for i, r in enumerate(results, 1):
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip() or "(无标题)"
        lines.append(f"{i}. {title}")
        url = str(r.get("url") or "").strip()
        if url:
            lines.append(f"   URL: {url}")
        meta = []
        site = r.get("site_name")
        if site:
            meta.append(f"来源: {site}")
        date = r.get("date")
        if date:
            meta.append(f"日期: {date}")
        authority = r.get("authority")
        if authority not in (None, -1):
            meta.append(f"权威: {authority}")
        score = r.get("rerank_score")
        if score is not None:
            meta.append(f"相关度: {score}")
        if meta:
            lines.append("   " + " | ".join(meta))
        summary = str(r.get("summary") or r.get("snippet") or "").strip()
        if summary:
            lines.append(f"   摘要: {_clip(summary, _SUMMARY_MAX_CHARS)}")
        if need_content:
            content = str(r.get("content") or "").strip()
            if content:
                lines.append(f"   正文: {_clip(content, _SUMMARY_MAX_CHARS * 2)}")
    return _clip_total(lines, _SEARCH_MAX_CHARS)


# ═══════════════════════════════════════════════════════════════════════
#  web_fetch → DeepSeek Fetch 接口
# ═══════════════════════════════════════════════════════════════════════

def fetch(url: str) -> str:
    """抓取单个网页，返回标题 + Markdown 正文。

    仅接受带 host 的 http/https URL（对齐接口约束），本地文件请用 run_read。
    """
    u = str(url or "").strip()
    if not u:
        return "Error: web_fetch 需要非空的 url。"
    if len(u) > 8192:
        return "Error: url 过长（接口上限 8192 字符）。"
    if not (u.startswith("http://") or u.startswith("https://")):
        return (
            "Error: web_fetch 只支持 http/https URL。"
            "读取工作空间里的本地文件请用 run_read 工具。"
        )

    data, err = _post(FETCH_PATH, {"url": u})
    if err is not None:
        return err
    if not isinstance(data, dict):
        return "Error: 联网接口返回了非预期的内容结构。"
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if not content:
        return f"未能抓取到网页正文（{u}）。页面可能受站点策略限制或无可提取的 Markdown 正文。"
    header = f"# {title}\n\n" if title else ""
    return _clip(header + content, _FETCH_MAX_CHARS)
