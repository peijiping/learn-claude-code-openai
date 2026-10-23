#!/usr/bin/env python3
"""
skill_market.py - 技能市场（多源客户端 + 安装计划翻译）

**技能这一侧没有 MCP Registry 那样的单一官方源**（2026-09-30 实测结论，见
docs/frontend/24 §1）。所以这里做的是「**多源聚合 + 统一形状**」，而不是接一个中心。

三种源类型（同一个 `search / resolve / fetch_files` 接口，前端零分派）：

| type    | 判据                        | 覆盖对象                                        |
| ------- | --------------------------- | ----------------------------------------------- |
| `git`   | 一个 git 仓库地址           | `anthropics/skills`、任何社区技能仓库（**主力**） |
| `api`   | 第三方公开 HTTP API         | `ruleskill.com` / `openpaths.io`（**尽力而为**）  |
| `index` | 一份自建 JSON 索引          | 团队自托管（最可控，见 §后续增量）                |

⚠️ 三条硬约束（改之前先读）：

1. **任何异常都转成 `error` 文案返回，绝不上抛** —— 调用方在 `ws_bridge` 的命令
   分发链里，那条链**没有兜底 try/except**，抛出去会直接掀掉整条 WS 连接。
2. **所有网络调用必须由调用方放进 `asyncio.to_thread`** —— 实测搜索耗时 0.5s ~ 17s
   （第三方源波动极大），在事件循环里同步等会把所有会话的流式事件一起卡住。
3. **不落盘缓存市场结构** —— 第三方源的响应 schema 随时可能变（`ruleskill.com`
   2026-09-30 实测就已经返回 `Registry temporarily unavailable`）。固化到磁盘上
   等于把坏结构留下来。只做内存 TTL 缓存，用于翻页与"确认页 → 安装"的复用。

⚠️ 安全前提（必须原样告知用户，见 docs/frontend/24）：**这些源都不做代码审计**。
技能正文是**给模型看的指令**（不是可执行文件，但能指挥模型去执行东西）——
安装确认页要原样展示 SKILL.md 全文，那是用户唯一的判断依据。
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx

from logger import get_logger
from paths import SKILL_MARKETS
from skill_store import parse_frontmatter, safe_relpath, unique_name
from store_io import read_json_lenient, write_json_atomic

log = get_logger("skill_market")

MANIFEST_NAME = "SKILL.md"

# ── 内置源（不可删除，只能启停；用户在文件里的 `enabled` 优先）─────────────
#
# 为什么把第三方 API 也预置进来却**不当默认视图**：它们是"能连上就赚到"的补充，
# 而实测稳定性差（ruleskill 今天就在返回错误）。默认选中 git 源 = 打开市场页永远
# 有一个能用的视图；第三方源出问题时只是那一页报错，不会让整个功能看起来是坏的。
BUILTIN_MARKETS: list[dict] = [
    {
        "id": "anthropics-official",
        "name": "Anthropic 官方技能",
        "type": "git",
        "repo": "anthropics/skills",
        "ref": "main",
        "publisher": "official",
        "homepage": "https://github.com/anthropics/skills",
        "note": "Agent Skills 规范的参考实现，含 SKILL.md 规范文档",
        "enabled": 1,
        "builtin": 1,
    },
    {
        "id": "claude-community-skills",
        "name": "社区技能合集（Claude 生态）",
        "type": "git",
        "repo": "ComposioHQ/awesome-claude-skills",
        "ref": "main",
        "publisher": "community",
        "homepage": "https://github.com/ComposioHQ/awesome-claude-skills",
        "note": "社区维护的技能清单仓库；同一仓库内可能同时是插件市场",
        "enabled": 1,
        "builtin": 1,
    },
    {
        "id": "ruleskill",
        "name": "RuleSkill（第三方 API）",
        "type": "api",
        "provider": "ruleskill",
        "base_url": "https://ruleskill.com/api/v1",
        "publisher": "third-party",
        "homepage": "https://ruleskill.com/tools/api",
        "note": "免鉴权 REST；实测稳定性一般，level 3 内容需要授权",
        "enabled": 1,
        "builtin": 1,
    },
    {
        "id": "openpaths",
        "name": "OpenPaths（第三方 API）",
        "type": "api",
        "provider": "openpaths",
        "base_url": "https://openpaths.io/v1",
        "publisher": "third-party",
        "homepage": "https://openpaths.io/skills",
        "note": "读接口免鉴权，支持 ?version= 固定版本",
        "enabled": 1,
        "builtin": 1,
    },
]

DEFAULT_MARKET_ID = "anthropics-official"

VALID_TYPES = ("git", "api", "index")
VALID_PROVIDERS = ("ruleskill", "openpaths")

# 单次搜索/抓取的规模闸门（防止一个巨型仓库把面板拖死）
MAX_SKILLS_PER_MARKET = 400
MAX_META_WORKERS = 8
RAW_MAX_BYTES = 512 * 1024

CACHE_TTL_SECONDS = float(os.environ.get("SKILL_MARKET_CACHE_TTL") or "300")
_META_CACHE: dict[tuple, tuple[float, dict]] = {}
_FILES_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
#  可调参数
# ═══════════════════════════════════════════════════════════════════════

def market_timeout() -> float:
    try:
        return float(os.environ.get("SKILL_MARKET_TIMEOUT") or "20")
    except (TypeError, ValueError):
        return 20.0


def raw_timeout() -> float:
    """raw CDN 单次尝试的超时（独立于 market_timeout，默认更短）。

    CDN 健康时 <3s 就该回包；挂起通道多等无益 —— 逐文件串行抓时每个文件
    都可能吃满一次超时，20s × n 个文件能把一次 resolve 拖过前端 60s IPC
    上限（表现为确认页「安装信息已失效」）。这里收紧到 10s，配合并行抓取
    把 resolve 的最坏墙时间压回预算内。
    """
    try:
        return float(os.environ.get("SKILL_MARKET_RAW_TIMEOUT") or "10")
    except (TypeError, ValueError):
        return 10.0


def page_size() -> int:
    try:
        return max(1, min(50, int(os.environ.get("SKILL_MARKET_PAGE_SIZE") or "20")))
    except (TypeError, ValueError):
        return 20


def github_token() -> str:
    """可选。带上可以显著提高 GitHub API 限额（未鉴权 60 次/时）。"""
    return (os.environ.get("SKILL_MARKET_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN") or "").strip()


# ═══════════════════════════════════════════════════════════════════════
#  源注册表（~/.aigent/skill_markets.json）
# ═══════════════════════════════════════════════════════════════════════

def load_markets(path=None) -> list[dict]:
    """读源列表（内置源自动合并进来；文件损坏时**退回只剩内置源**，不拦页面）。

    合并规则：内置源的 `type/repo/provider/name` 等身份字段**以代码为准**，
    用户在文件里只能改 `enabled`（以及自定义源的全部字段）。这样改一次默认源地址
    就随版本生效，不必让用户去编辑 JSON。
    """
    raw = read_json_lenient(path or SKILL_MARKETS, {}, label="skill_markets.json")
    custom: list[dict] = []
    saved: dict[str, dict] = {}
    if isinstance(raw, dict):
        for entry in raw.get("markets") or []:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("id") or "").strip()
            if mid:
                saved[mid] = entry
        for entry in raw.get("markets") or []:
            if isinstance(entry, dict) and not _is_builtin_id(entry.get("id")):
                custom.append(dict(entry))

    out: list[dict] = []
    for builtin in BUILTIN_MARKETS:
        merged = dict(builtin)
        override = saved.get(builtin["id"])
        if isinstance(override, dict):
            merged["enabled"] = override.get("enabled", builtin.get("enabled", 1))
        out.append(merged)
    for entry in custom:
        out.append(entry)
    return out


def list_markets(path=None) -> list[dict]:
    """给 UI 的源列表（附 `item_count` 占位与 `is_default` 标记）。"""
    out = []
    for m in load_markets(path):
        item = dict(m)
        item["enabled"] = _enabled(item.get("enabled", 1))
        item["builtin"] = bool(_is_builtin_id(item.get("id")))
        item["is_default"] = item.get("id") == DEFAULT_MARKET_ID
        item["type_label"] = {
            "git": "git 仓库",
            "api": "第三方 API",
            "index": "JSON 索引",
        }.get(str(item.get("type") or ""), str(item.get("type") or ""))
        out.append(item)
    return out


def save_markets(markets: list[dict], path=None) -> None:
    write_json_atomic(path or SKILL_MARKETS, {"markets": markets})


def upsert_market(entry: dict, path=None) -> list[dict]:
    """新增或更新一个源。返回最新源列表。校验不过抛 `ValueError`。

    **内置源与自定义源走两条不同的路**：内置源的身份字段（type / repo / provider /
    name）以代码为准，所以调用方**只需要给 `{id, enabled}`** —— 不能要求它先把
    name / type / repo 都填一遍才让改启停（那是把实现细节泄漏给了调用方）。
    """
    entry = dict(entry or {})
    mid = str(entry.get("id") or "").strip()
    if _is_builtin_id(mid):
        raw = read_json_lenient(path or SKILL_MARKETS, {}, label="skill_markets.json")
        markets = [dict(m) for m in (raw.get("markets") or []) if isinstance(m, dict)]
        for m in markets:
            if str(m.get("id")) == mid:
                m["enabled"] = entry.get("enabled", 1)
                break
        else:
            markets.append({"id": mid, "enabled": entry.get("enabled", 1)})
        save_markets(markets, path)
        return list_markets(path)

    errors = validate_market(entry)
    if errors:
        raise ValueError("；".join(errors))
    markets = [dict(m) for m in load_markets(path) if not _is_builtin_id(m.get("id"))]
    markets = [m for m in markets if str(m.get("id")) != mid]
    markets.append(_clean_market(entry))
    save_markets(markets, path)
    return list_markets(path)


def remove_market(market_id: str, path=None) -> tuple[list[dict], list[str]]:
    """删除自定义源。内置源**拒绝删除**（给明确原因，不是静默忽略）。"""
    market_id = (market_id or "").strip()
    if _is_builtin_id(market_id):
        return list_markets(path), [f"「{market_id}」是内置源，只能停用，不能删除"]
    raw = read_json_lenient(path or SKILL_MARKETS, {}, label="skill_markets.json")
    markets = [dict(m) for m in (raw.get("markets") or []) if isinstance(m, dict)]
    save_markets([m for m in markets if str(m.get("id")) != market_id], path)
    return list_markets(path), []


def validate_market(entry: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["源配置必须是 JSON 对象"]
    mid = str(entry.get("id") or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", mid):
        errors.append("源 id 只能用字母、数字与 . _ -，且必须以字母或数字开头")
    if not str(entry.get("name") or "").strip():
        errors.append("源名称不能为空")
    mtype = str(entry.get("type") or "").strip()
    if mtype not in VALID_TYPES:
        errors.append(f"源类型 {mtype!r} 不支持（可选：{' / '.join(VALID_TYPES)}）")
    elif mtype == "git":
        repo = str(entry.get("repo") or "").strip()
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo) and not repo.startswith("http"):
            errors.append("git 源必须提供 repo（`owner/name` 或完整仓库 URL）")
    elif mtype == "api":
        provider = str(entry.get("provider") or "").strip()
        if provider not in VALID_PROVIDERS:
            errors.append(f"API 提供方 {provider!r} 不支持（可选：{' / '.join(VALID_PROVIDERS)}）")
    elif mtype == "index":
        url = str(entry.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            errors.append("JSON 索引源必须提供以 http:// 或 https:// 开头的 url")
    return errors


def get_market(market_id: str, path=None) -> dict | None:
    mid = (market_id or "").strip()
    for m in load_markets(path):
        if str(m.get("id")) == mid:
            return m
    return None


def _clean_market(entry: dict) -> dict:
    keys = ("id", "name", "type", "repo", "ref", "provider", "base_url", "url",
            "publisher", "homepage", "note", "enabled")
    out = {k: entry[k] for k in keys if k in entry}
    out["enabled"] = 1 if _enabled(out.get("enabled", 1)) else 0
    return out


def _is_builtin_id(market_id) -> bool:
    mid = str(market_id or "").strip()
    return any(b["id"] == mid for b in BUILTIN_MARKETS)


def _enabled(value) -> bool:
    return value not in (0, "0", False, "false")


# ═══════════════════════════════════════════════════════════════════════
#  搜索
# ═══════════════════════════════════════════════════════════════════════

def search(market_id: str, query: str = "", cursor: str = "",
           limit: int | None = None, path=None) -> dict:
    """在指定源里搜索。返回 `{items, next_cursor, market_id, query, error, elapsed_ms}`。

    **绝不抛异常**：网络 / 超时 / schema 变动 / 源不存在都收敛成 `error` 文案。
    """
    started = time.time()
    mid = (market_id or DEFAULT_MARKET_ID).strip()
    q = (query or "").strip()
    try:
        size = max(1, min(50, int(limit))) if limit else page_size()
    except (TypeError, ValueError):
        size = page_size()

    market = get_market(mid, path)
    if market is None:
        return _fail(mid, q, f"没有名为「{mid}」的技能源", started)
    if not _enabled(market.get("enabled", 1)):
        return _fail(mid, q, f"技能源「{market.get('name') or mid}」已停用", started)

    mtype = str(market.get("type") or "")
    try:
        if mtype == "git":
            items = _search_git(market, q)
        elif mtype == "api":
            items = _search_api(market, q, started)
        elif mtype == "index":
            items = _search_index(market, q)
        else:
            return _fail(mid, q, f"源类型 {mtype!r} 不支持", started)
    except Exception as exc:  # noqa: BLE001 - 分发链无兜底，这里必须兜住
        log.error("技能市场搜索失败 %s：%s: %s", mid, type(exc).__name__, exc)
        return _fail(mid, q, f"搜索失败：{type(exc).__name__}: {exc}", started)

    if isinstance(items, dict) and "error" in items:
        return _fail(mid, q, str(items["error"]), started)

    # 分页用**游标式偏移**（cursor 就是下一段的起始下标，字符串形态）
    try:
        offset = max(0, int(cursor)) if str(cursor or "").strip() else 0
    except (TypeError, ValueError):
        offset = 0
    page = items[offset:offset + size]
    next_cursor = str(offset + size) if offset + size < len(items) else ""

    # 只给**当前页**补描述（描述在 frontmatter 里，要逐个抓 SKILL.md）
    _enrich_git(market, page)

    return {
        "items": page,
        "next_cursor": next_cursor,
        "market_id": mid,
        "query": q,
        "total": len(items),
        "error": "",
        "cached": False,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _fail(mid: str, query: str, msg: str, started: float) -> dict:
    return {"items": [], "next_cursor": "", "market_id": mid, "query": query,
            "total": 0, "error": msg, "cached": False,
            "elapsed_ms": int((time.time() - started) * 1000)}


# ── git 源 ────────────────────────────────────────────────────────────

def _search_git(market: dict, query: str) -> list[dict] | dict:
    repo = str(market.get("repo") or "").strip()
    ref = str(market.get("ref") or "").strip() or "main"
    tree = _git_tree(repo, ref)
    if isinstance(tree, dict) and tree.get("error"):
        return tree
    dirs = _skill_dirs(tree)
    if not dirs:
        return {"error": f"仓库 {repo} 里没有找到任何 {MANIFEST_NAME}（这不一定是错误，"
                         f"可能是该仓库只放插件清单）"}
    items = [_git_item(market, repo, ref, d) for d in dirs]
    if query:
        low = query.lower()
        items = [it for it in items if low in it["_haystack"]]
    for it in items:
        it.pop("_haystack", None)
    return items[:MAX_SKILLS_PER_MARKET]


def _git_tree(repo: str, ref: str) -> dict | list:
    """拉一次递归 tree —— 一个请求拿到全仓库路径（比逐目录探索省得多）。"""
    owner_repo = _owner_repo(repo)
    if not owner_repo:
        return {"error": f"无法识别的仓库地址：{repo}"}
    url = f"https://api.github.com/repos/{owner_repo}/git/trees/{ref}?recursive=1"
    res = _get_json(url, headers=_gh_headers())
    if isinstance(res, dict) and res.get("error"):
        return res
    tree = res.get("tree") if isinstance(res, dict) else None
    if not isinstance(tree, list):
        return {"error": "GitHub 返回的 tree 结构异常"}
    if res.get("truncated"):
        log.warning("仓库 %s 的 tree 被截断，技能列表可能不完整", repo)
    return [t.get("path") for t in tree
            if isinstance(t, dict) and t.get("type") == "blob" and t.get("path")]


def _skill_dirs(paths: list[str]) -> list[str]:
    """从全量路径里挑出「含 SKILL.md 的目录」。

    接受任意深度（不只 `skills/`）—— Agent Skills 生态的仓库布局没有强制约定，
    实测有 `skills/x/`、`.claude/skills/x/`、也有根目录直接就是技能。但剪掉
    明显不该扫的目录（依赖、构建产物、示例），否则一个大仓库能刷出上千条。
    """
    skip = ("node_modules/", ".git/", "vendor/", "dist/", "build/",
            "__pycache__/", ".venv/", "site-packages/")
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if not p.endswith("/" + MANIFEST_NAME) and p != MANIFEST_NAME:
            continue
        if any(s in p for s in skip):
            continue
        d = p[:-(len(MANIFEST_NAME) + 1)] if p != MANIFEST_NAME else ""
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
        if len(out) >= MAX_SKILLS_PER_MARKET:
            break
    return sorted(out)


def _git_item(market: dict, repo: str, ref: str, directory: str) -> dict:
    dir_name = directory.split("/")[-1] if directory else _owner_repo(repo).split("/")[-1]
    owner_repo = _owner_repo(repo)
    return {
        "id": f"{market['id']}:{directory or '.'}",
        "market_id": market["id"],
        "market_name": market.get("name") or market["id"],
        "source_kind": "git",
        "name": dir_name,
        "dir_name": dir_name,
        "path": directory,
        "description": "",
        "version": "",
        "tags": [],
        "publisher": market.get("publisher") or _publisher_of(owner_repo),
        "installable": True,
        "reason": "",
        "repo": owner_repo,
        "ref": ref,
        "url": f"https://github.com/{owner_repo}/tree/{ref}/{directory}" if directory
               else f"https://github.com/{owner_repo}",
        "_haystack": f"{dir_name} {directory} {owner_repo}".lower(),
    }


def _enrich_git(market: dict, page: list[dict]) -> None:
    """给当前页补 description / version / tags（读 SKILL.md 的 frontmatter）。

    并发但**限量**（8 线程），并且**任何一条失败都只让那一条空着** —— 补描述是
    锦上添花，绝不能让它把整个搜索变成错误。
    """
    targets = [it for it in page if it.get("source_kind") == "git" and "._meta" not in it]
    if not targets:
        return

    def _one(item: dict) -> None:
        repo, ref, directory = item["repo"], item["ref"], item["path"]
        cached = _meta_cache_get((repo, ref, directory))
        if cached is None:
            text = _raw_text(repo, ref, f"{directory}/{MANIFEST_NAME}" if directory
                             else MANIFEST_NAME)
            if not text:
                item["_meta"] = True
                return
            meta, body = parse_frontmatter(text)
            cached = {
                "description": str(meta.get("description") or "").strip() or _first_line(body),
                "version": str(meta.get("version") or "").strip(),
                "tags": _tags_of(meta),
                "name": str(meta.get("name") or "").strip(),
                "license": str(meta.get("license") or "").strip(),
            }
            _meta_cache_put((repo, ref, directory), cached)
        item.update({k: v for k, v in cached.items() if k != "name"})
        if cached.get("name"):
            # frontmatter 里的名字优先（目录名可能是 `01-pdf` 这种带序号的）
            item["name"] = cached["name"]
        item.pop("_haystack", None)

    try:
        with ThreadPoolExecutor(max_workers=MAX_META_WORKERS) as pool:
            list(pool.map(_one, targets))
    except Exception as exc:  # noqa: BLE001
        log.warning("补技能描述失败（不影响列表）：%s: %s", type(exc).__name__, exc)


# ── api 源 ────────────────────────────────────────────────────────────

def _search_api(market: dict, query: str, started: float) -> list[dict] | dict:
    provider = str(market.get("provider") or "")
    base = str(market.get("base_url") or "").rstrip("/")
    if provider == "ruleskill":
        res = _get_json(f"{base}/search", params={"q": query or "skill", "limit": 40})
        if isinstance(res, dict) and res.get("error"):
            return res
        rows = _first_list(res, ("results", "items", "skills", "data"))
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue
            out.append({
                "id": f"{market['id']}:{sid}",
                "market_id": market["id"],
                "market_name": market.get("name") or market["id"],
                "source_kind": "api",
                "name": sid,
                "dir_name": sid,
                "path": sid,
                "description": str(row.get("description") or "").strip(),
                "version": "",
                "tags": _as_tags(row.get("tags")),
                "publisher": market.get("publisher") or "third-party",
                "installable": True,
                "reason": "",
                "repo": "",
                "ref": "",
                "url": str(row.get("source_url") or row.get("url") or "").strip(),
            })
        return out
    if provider == "openpaths":
        res = _get_json(f"{base}/skills/search", params={"q": query} if query else {})
        if isinstance(res, dict) and res.get("error"):
            return res
        rows = _first_list(res, ("results", "items", "skills", "data"))
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug") or row.get("id") or row.get("name") or "").strip()
            if not slug:
                continue
            out.append({
                "id": f"{market['id']}:{slug}",
                "market_id": market["id"],
                "market_name": market.get("name") or market["id"],
                "source_kind": "api",
                "name": str(row.get("name") or slug).strip(),
                "dir_name": slug.split("/")[-1],
                "path": slug,
                "description": str(row.get("description") or "").strip(),
                "version": str(row.get("version") or "").strip(),
                "tags": _as_tags(row.get("tags") or row.get("category")),
                "publisher": market.get("publisher") or "third-party",
                "installable": True,
                "reason": "",
                "repo": "",
                "ref": "",
                "url": str(row.get("url") or "").strip(),
            })
        return out
    return {"error": f"API 提供方 {provider!r} 未实现"}


# ── index 源 ──────────────────────────────────────────────────────────

def _search_index(market: dict, query: str) -> list[dict] | dict:
    """自建索引：约定 `{"skills": [{name, description, path, url, files?}]}`。

    `path` 是仓库内的相对目录，`repo`/`ref` 由源配置给出 → 走与 git 源同一条抓取链，
    所以"自建索引"实际上只是给了 git 扫描一个**更准的清单**（避免全仓库 tree）。
    """
    url = str(market.get("url") or "").strip()
    res = _get_json(url)
    if isinstance(res, dict) and res.get("error"):
        return res
    rows = _first_list(res, ("skills", "items", "data", "results"))
    repo = str(market.get("repo") or "").strip()
    ref = str(market.get("ref") or "").strip() or "main"
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip().strip("/")
        name = str(row.get("name") or path.split("/")[-1] or "").strip()
        if not name:
            continue
        item = _git_item(market, repo, ref, path) if repo else {
            "id": f"{market['id']}:{name}", "market_id": market["id"],
            "market_name": market.get("name") or market["id"], "source_kind": "index",
            "name": name, "dir_name": name, "path": path, "repo": "", "ref": "",
            "publisher": market.get("publisher") or "community",
            "installable": True, "reason": "", "url": str(row.get("url") or ""),
        }
        item["id"] = f"{market['id']}:{path or name}"
        item["name"] = name
        item["dir_name"] = name
        item["path"] = path
        item["description"] = str(row.get("description") or "").strip()
        item["version"] = str(row.get("version") or "").strip()
        item["tags"] = _as_tags(row.get("tags"))
        if row.get("files"):
            item["_files"] = row["files"]
        out.append(item)
    return out


# ═══════════════════════════════════════════════════════════════════════
#  翻译：条目 → 安装计划（纯抓取，**不落盘**）
# ═══════════════════════════════════════════════════════════════════════

def resolve(market_id: str, item: dict, existing_names: list[str] | None = None,
            path=None) -> dict:
    """把市场条目转成**安装确认页**要展示的东西。

    刻意拆成独立一步、且**不落盘**：用户必须先看到 `SKILL.md` **全文**与将要写入的
    文件清单，确认后才走 `skill_install`。技能正文是"模型接下来会照着做什么"的
    唯一凭据 —— 这与 MCP 侧必须展示 `command/args` 原文是同一条理由。

    返回 `{ok, name, skill_md, files[], warnings[], unsupported, error, meta}`。
    """
    if not isinstance(item, dict):
        return plan_fail("条目格式非法")
    mid = str(item.get("market_id") or market_id or "").strip()
    market = get_market(mid, path)
    if market is None:
        return plan_fail(f"没有名为「{mid}」的技能源")

    warnings: list[str] = []
    try:
        files = fetch_files(mid, item, path)
    except Exception as exc:  # noqa: BLE001
        log.error("技能安装计划抓取失败：%s: %s", type(exc).__name__, exc)
        return plan_fail(f"抓取失败：{type(exc).__name__}: {exc}")
    if isinstance(files, dict) and files.get("__error__"):
        return plan_fail(str(files["__error__"]))

    if MANIFEST_NAME not in files:
        return plan_fail(f"该条目里没有 {MANIFEST_NAME}，无法作为技能安装")

    skill_md = files[MANIFEST_NAME]
    if not isinstance(skill_md, str):
        skill_md = str(skill_md)
    if len(skill_md) > 200_000:
        skill_md = skill_md[:200_000] + "\n\n…（内容过长，已截断）"
        warnings.append("SKILL.md 过长，确认页只展示前 200,000 字符")

    meta, body = parse_frontmatter(skill_md)
    suggested = str(meta.get("name") or "").strip() or item.get("dir_name") or item.get("name")
    name = unique_name(_slug(suggested), existing_names or [])
    if name != suggested:
        warnings.append(f"已存在同名技能或名称需归一化，建议名改为「{name}」")
    if not str(meta.get("description") or "").strip():
        warnings.append(f"{MANIFEST_NAME} 的 frontmatter 缺少 description —— "
                        "技能列表与系统提示里只能显示正文首行")

    file_list = []
    for rel in sorted(files):
        if rel == "__error__":
            continue
        content = files[rel]
        size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        file_list.append({"path": rel, "size": size})

    return {
        "ok": True,
        "name": name,
        "skill_name": suggested,
        "description": str(meta.get("description") or "").strip() or _first_line(body),
        "skill_md": skill_md,
        "files": file_list,
        "file_count": len(file_list),
        "total_bytes": sum(f["size"] for f in file_list),
        "tags": _tags_of(meta),
        "version": str(meta.get("version") or item.get("version") or "").strip(),
        "warnings": warnings,
        "unsupported": "",
        "error": "",
        "meta": {
            "source": "market",
            "market_id": mid,
            "market_name": market.get("name") or mid,
            "publisher": item.get("publisher") or market.get("publisher") or "",
            "market_url": str(item.get("url") or ""),
        },
    }


def plan_fail(msg: str) -> dict:
    return {"ok": False, "name": "", "skill_name": "", "description": "", "skill_md": "",
            "files": [], "file_count": 0, "total_bytes": 0, "tags": [], "version": "",
            "warnings": [], "unsupported": msg, "error": "", "meta": {}}


def fetch_files(market_id: str, item: dict, path=None) -> dict:
    """把条目对应的**全部文件**取回来 → `{相对路径: str|bytes}`。

    失败时返回 `{"__error__": "原因"}`（而不是抛）—— 调用方在无兜底 try 的链路上。
    结果进内存 TTL 缓存：确认页刚抓完，用户点「确认安装」时不必再抓一次。
    """
    mid = str(item.get("market_id") or market_id or "").strip()
    market = get_market(mid, path)
    if market is None:
        return {"__error__": f"没有名为「{mid}」的技能源"}

    key = (mid, str(item.get("id") or ""))
    cached = _files_cache_get(key)
    if cached is not None:
        return cached

    kind = str(item.get("source_kind") or market.get("type") or "")
    try:
        if kind == "git":
            files = _fetch_git_files(item)
        elif kind == "api":
            files = _fetch_api_files(market, item)
        elif kind == "index":
            files = _fetch_index_files(market, item)
        else:
            return {"__error__": f"不支持的源类型：{kind}"}
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"抓取失败：{type(exc).__name__}: {exc}"}

    if isinstance(files, dict) and files.get("__error__"):
        return files
    if not files:
        return {"__error__": "该条目没有任何文件内容"}
    _files_cache_put(key, files)
    return files


def _fetch_git_files(item: dict) -> dict:
    """git 源：先拿该目录下的完整文件清单（一次 tree），再逐个 raw 抓。"""
    repo = str(item.get("repo") or "")
    ref = str(item.get("ref") or "main")
    directory = str(item.get("path") or "").strip("/")
    tree = _git_tree(repo, ref)
    if isinstance(tree, dict) and tree.get("error"):
        return tree
    prefix = f"{directory}/" if directory else ""
    rels = [p[len(prefix):] for p in tree if p.startswith(prefix) and p != prefix]
    # 只收该目录**直接子树**里的文件（`skills/pdf/...`，不再往外扩）
    rels = [r for r in rels if r and not r.endswith("/")]
    rels = [r for r in rels if safe_relpath(r) and "/node_modules/" not in f"/{r}"]
    if MANIFEST_NAME not in rels:
        # 目录本身可能就是 SKILL.md（root 技能）
        rels = [MANIFEST_NAME]
    if len(rels) > 200:
        rels = rels[:200]
    out: dict[str, Any] = {}
    # **并行抓取**（与插件侧同款）：逐文件串行时，挂起通道会让每个文件独立
    # 吃满一次 raw 超时，n 个文件的技能能把一次 resolve 拖过前端 60s IPC 上限
    # —— 症状是「市场搜得到、点安装后确认页报安装信息已失效」。并行后墙时间
    # ≈ 一轮最慢通道 + 快通道的 ⌈n/8⌉ 轮，稳稳落在预算内。失败语义不变：
    # 单文件失败只让它缺席，全空才算失败。
    with ThreadPoolExecutor(max_workers=MAX_META_WORKERS) as pool:
        fetched = list(pool.map(
            lambda rel: (rel, _raw_text(repo, ref, f"{prefix}{rel}")), rels))
    for rel, text in fetched:
        if text is not None:
            out[rel] = text
    if not out:
        return {"__error__": f"没能取到 {repo} 里 {directory or '/'} 的任何文件内容"}
    return out


def _fetch_api_files(market: dict, item: dict) -> dict:
    """第三方 API：拿 SKILL.md 原文。拿不到附属文件 —— 只有正文。"""
    provider = str(market.get("provider") or "")
    base = str(market.get("base_url") or "").rstrip("/")
    sid = str(item.get("path") or item.get("dir_name") or "").strip()
    if provider == "ruleskill":
        res = _get_json(f"{base}/skills/{sid}/payload", params={"level": 2})
        if isinstance(res, dict) and res.get("error"):
            return res
        raw = _pick_text(res, ("raw_content", "rawContent", "content", "body", "markdown"))
        if not raw:
            payload = res.get("payload") if isinstance(res, dict) else None
            if isinstance(payload, dict):
                raw = _pick_text(payload, ("instruction", "content", "body"))
                fm = payload.get("frontmatter")
                if raw and isinstance(fm, str) and not raw.startswith("---"):
                    raw = f"---\n{fm.strip()}\n---\n\n{raw}"
            if not raw:
                return {"__error__": "该源没有返回技能正文（level 2 可能需要授权）"}
        return {MANIFEST_NAME: raw}
    if provider == "openpaths":
        res = _get_json(f"{base}/skills/{sid}")
        if isinstance(res, dict) and res.get("error"):
            return res
        body = _pick_text(res, ("body", "raw_content", "content", "skill", "markdown"))
        if not body:
            return {"__error__": "该源没有返回技能正文"}
        out = {MANIFEST_NAME: body}
        files = res.get("files") if isinstance(res, dict) else None
        if isinstance(files, list):
            for f in files:
                if not isinstance(f, dict):
                    continue
                rel = safe_relpath(str(f.get("path") or ""))
                content = f.get("content")
                if rel and rel != MANIFEST_NAME and isinstance(content, str):
                    out[rel] = content
        return out
    return {"__error__": f"API 提供方 {provider!r} 未实现"}


def _fetch_index_files(market: dict, item: dict) -> dict:
    """自建索引：索引里直接带 `files` 就优先用；否则回退到 git 抓取。"""
    inline = item.get("_files")
    if isinstance(inline, dict) and inline:
        out: dict[str, Any] = {}
        for rel, content in inline.items():
            safe = safe_relpath(rel)
            if safe and isinstance(content, str):
                out[safe] = content
        if MANIFEST_NAME in out:
            return out
    if str(item.get("repo") or ""):
        return _fetch_git_files(item)
    return {"__error__": "该索引条目既没有内联 files，也没有 repo 可供抓取"}


# ═══════════════════════════════════════════════════════════════════════
#  HTTP 与工具
# ═══════════════════════════════════════════════════════════════════════

def _gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "aigent-skill-market"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, params: dict | None = None,
              headers: dict | None = None) -> dict:
    """GET → JSON。**绝不抛异常**：一切失败收敛成 `{"error": 人话}`。"""
    try:
        with httpx.Client(timeout=market_timeout(), follow_redirects=True) as client:
            resp = client.get(url, params=params or None, headers=headers or
                              {"Accept": "application/json", "User-Agent": "aigent-skill-market"})
            if resp.status_code == 403 and "api.github.com" in url and not github_token():
                return {"error": "GitHub 接口限流（未鉴权每小时 60 次）。"
                                 "可在环境变量里设置 SKILL_MARKET_GITHUB_TOKEN 提高限额。"}
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return {"error": f"请求超时（{market_timeout():.0f}s）：{_host(url)}"}
    except httpx.HTTPStatusError as exc:
        return {"error": f"{_host(url)} 返回 HTTP {exc.response.status_code}"}
    except httpx.HTTPError as exc:
        return {"error": f"无法连接 {_host(url)}：{type(exc).__name__}"}
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"{_host(url)} 的响应不是合法 JSON：{exc}"}


# raw 抓取通道：直连优先、jsDelivr 镜像兜底。2026-09-30 实测国内网络
# raw.githubusercontent.com 整段挂起（TLS 层 0 字节超时），api.github.com 却可达
# → 症状是「市场搜得到条目、点安装没反应」：搜索走 Tree API，抓文件走 raw。
# 技能侧逐文件串行抓（每文件一个 market_timeout）、插件侧并行抓，撞上死通道
# 不是 60s IPC 超时就是全部文件为空。进程内记住最近成功的通道，失败通道冷却
# 5 分钟，避免每次抓取（乃至每个并行 worker）都重新撞一遍超时。
_RAW_BASES = ("https://raw.githubusercontent.com", "https://cdn.jsdelivr.net/gh")
_RAW_COOLDOWN_SECONDS = 300.0
_raw_state: dict = {"good": "", "dead_until": {}}
_raw_state_lock = threading.Lock()


def _raw_url_for(base: str, owner_repo: str, ref: str, relpath: str) -> str | None:
    """通道 base → 完整文件 URL。jsDelivr 的 `@ref` 表达不了带斜杠的分支 → 跳过该通道。"""
    if base.endswith("/gh"):
        if "/" in ref:
            return None
        return f"{base}/{owner_repo}@{ref}/{relpath}"
    return f"{base}/{owner_repo}/{ref}/{relpath}"


def _raw_bases_in_order() -> list[str]:
    """尝试顺序：最近成功的优先，其余按声明顺序（冷却中的靠后但不剔除）。"""
    bases = list(_RAW_BASES)
    good = _raw_state["good"]
    if good in bases:
        bases.remove(good)
        bases.insert(0, good)
    now = time.monotonic()
    bases.sort(key=lambda b: 1 if _raw_state["dead_until"].get(b, 0) > now else 0)
    return bases


def _raw_mark(base: str, ok: bool) -> None:
    with _raw_state_lock:
        if ok:
            _raw_state["good"] = base
            _raw_state["dead_until"].pop(base, None)
        else:
            _raw_state["dead_until"][base] = time.monotonic() + _RAW_COOLDOWN_SECONDS


def _raw_bytes(repo: str, ref: str, relpath: str,
               max_bytes: int = RAW_MAX_BYTES) -> bytes | None:
    """读仓库里某个文件的原始字节（多通道依次回退）。

    **走 raw CDN 而不是 Contents API**：raw 由 CDN 直出、不计入 GitHub API 限额，
    而逐个文件走 Contents API 会瞬间打爆 60 次/时的额度。（`plugin_market` 复用
    本函数 —— 插件里的图片等二进制资产必须原样保留，不能过一遍文本解码。）

    通道语义：
    - 404 是**确定结果**（文件不存在）→ 直接 None，不换通道重试（否则每次 404
      都变成两倍请求，搜索页几十个条目会翻倍）；
    - 连接失败 / 超时 / 5xx → 换下一个通道，并给失败通道记冷却；
    - 成功 → 记住通道，后续请求直接走它。
    """
    owner_repo = _owner_repo(repo)
    if not owner_repo:
        return None
    for base in _raw_bases_in_order():
        url = _raw_url_for(base, owner_repo, ref, relpath)
        if url is None:
            continue
        try:
            with httpx.Client(timeout=raw_timeout(), follow_redirects=True) as client:
                resp = client.get(url, headers={"User-Agent": "aigent-skill-market"})
        except (httpx.HTTPError, OSError) as exc:
            log.debug("raw 通道 %s 失败（%s: %s），尝试下一个通道",
                      _host(url), type(exc).__name__, exc)
            _raw_mark(base, False)
            continue
        if resp.status_code == 200:
            _raw_mark(base, True)
            return resp.content[:max_bytes]
        if resp.status_code == 404:
            return None
        log.debug("raw 通道 %s 返回 HTTP %d，尝试下一个通道",
                  _host(url), resp.status_code)
        _raw_mark(base, False)
    return None


def _raw_text(repo: str, ref: str, relpath: str,
              max_bytes: int = RAW_MAX_BYTES) -> str | None:
    raw = _raw_bytes(repo, ref, relpath, max_bytes)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def _owner_repo(repo: str) -> str:
    """`https://github.com/owner/name(.git)` / `owner/name` → `owner/name`。"""
    s = (repo or "").strip()
    if not s:
        return ""
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)", s)
    if m:
        return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    m = re.match(r"^([\w.-]+)/([\w.-]+)$", s)
    if m:
        return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    return ""


def _publisher_of(owner_repo: str) -> str:
    """信任档：官方 / 社区 / 第三方。

    ⚠️ **这只是"谁发布"，不是"是否安全"** —— 三档都没有代码审计（同 MCP 侧口径，
    docs/frontend/23 §2.4）。UI 徽标一律中性色，不能用绿色暗示"更安全"。
    """
    head = (owner_repo or "").split("/")[0].lower()
    if head in ("anthropics", "modelcontextprotocol"):
        return "official"
    return "community"


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", url or "")
    return m.group(1) if m else url


def _first_list(res, keys) -> list:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in keys:
            if isinstance(res.get(k), list):
                return res[k]
    return []


def _pick_text(res, keys) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        for k in keys:
            v = res.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _as_tags(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def _tags_of(meta: dict) -> list[str]:
    return _as_tags(meta.get("tags") if meta.get("tags") is not None
                    else meta.get("keywords"))


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s.lstrip("#").strip()
    return ""


def _slug(name: str) -> str:
    """磁盘目录名：非白名单字符折成 `-`（`unique_name` 会再兜一层）。"""
    return re.sub(r"[^A-Za-z0-9._-]", "-", (name or "").strip()).strip(".-") or "skill"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta_cache_get(key: tuple):
    with _CACHE_LOCK:
        hit = _META_CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        if hit:
            _META_CACHE.pop(key, None)
    return None


def _meta_cache_put(key: tuple, value: dict) -> None:
    with _CACHE_LOCK:
        _META_CACHE[key] = (time.time() + CACHE_TTL_SECONDS, value)
        if len(_META_CACHE) > 500:
            _META_CACHE.clear()


def _files_cache_get(key: tuple):
    with _CACHE_LOCK:
        hit = _FILES_CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        if hit:
            _FILES_CACHE.pop(key, None)
    return None


def _files_cache_put(key: tuple, value: dict) -> None:
    with _CACHE_LOCK:
        _FILES_CACHE[key] = (time.time() + CACHE_TTL_SECONDS, value)
        if len(_FILES_CACHE) > 40:
            _FILES_CACHE.clear()
