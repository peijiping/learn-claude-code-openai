#!/usr/bin/env python3
"""
plugin_market.py - 插件市场（Claude Code `marketplace.json` 客户端 + 安装计划翻译）

数据源形态（2026-09-30 实测定稿，见 docs/frontend/25 §1）：

**一个市场 = 一个 git 仓库 + 根目录一份 `.claude-plugin/marketplace.json`。**
这不是我们定的规范，是 Claude Code 的既定事实标准，好处是"兼容即市场"：

    https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/
        .claude-plugin/marketplace.json      → 实测 200，**314 个插件**

条目 `source` 实测三种形态（我按官方市场 314 条统计过分布）：

| 形态         | 计数 | 例子                                                       |
| ------------ | ---- | ---------------------------------------------------------- |
| 相对路径     | 52   | `"./plugins/agent-sdk-dev"`（就在市场仓库自己里面）         |
| 完整 git url | 164  | `{source:"url", url:"https://github.com/o/r.git", sha}`     |
| git 子目录   | 98   | `{source:"git-subdir", url, path:"plugins/x", ref, sha}`    |

⚠️ 安全前提（必须原样告知用户）：官方市场做的是**质量与安全筛查**，但
**社区市场 / 任意第三方仓库的市场只做格式校验**。插件能带 hooks / MCP 服务器
（= 在本机跑代码），被列出来 ≠ 安全。安装确认页是唯一的安全闸门。

⚠️ 与 `skill_market` 完全同源的两条硬约束：**任何异常都收敛成 `error` 文案、
绝不上抛**（`ws_bridge` 的分发链没有兜底 try）；**网络调用必须由调用方放进
`asyncio.to_thread`**（实测最慢一次十几秒）。
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from logger import get_logger
from paths import PLUGIN_MARKETS
from plugin_store import (
    MANIFEST_DIR,
    MANIFEST_NAME,
    inventory_of_files,
    unique_name,
)
from skill_market import (
    _get_json as http_json,
    _owner_repo as owner_repo,
    _raw_bytes as raw_bytes,
    _raw_text as raw_text,
    market_timeout,
)
from skill_store import safe_relpath
from store_io import read_json_lenient, write_json_atomic

log = get_logger("plugin_market")

MARKET_FILE = f"{MANIFEST_DIR}/marketplace.json"
# marketplace.json 本身可能很大（官方市场 314 条 ≈ 数百 KB）→ 单独放宽上限，
# 不能被 `_raw_bytes` 的默认 512 KiB 截断（截断后 JSON 解析必然失败）
MARKET_FILE_MAX_BYTES = 8 * 1024 * 1024

MAX_PLUGINS_PER_MARKET = 1000
MAX_PLUGIN_FILES = 300
FETCH_WORKERS = 8

# ── 内置市场（不可删除，只能启停；文件里的 `enabled` 优先）──────────────
BUILTIN_MARKETS: list[dict] = [
    {
        "id": "claude-plugins-official",
        "name": "Claude 官方插件市场",
        "repo": "anthropics/claude-plugins-official",
        "ref": "main",
        "publisher": "official",
        "homepage": "https://github.com/anthropics/claude-plugins-official",
        "note": "官方维护；实测 314 个插件，含开发工作流、外部集成、语言服务器",
        "enabled": 1,
        "builtin": 1,
    },
    {
        "id": "claude-plugins-community",
        "name": "Claude 社区插件市场",
        "repo": "anthropics/claude-plugins-community",
        "ref": "main",
        "publisher": "community",
        "homepage": "https://github.com/anthropics/claude-plugins-community",
        "note": "第三方插件经自动校验后收录，每条 pin 到具体 commit",
        "enabled": 1,
        "builtin": 1,
    },
    {
        "id": "claude-code-demo",
        "name": "Claude Code 示例市场",
        "repo": "anthropics/claude-code",
        "ref": "main",
        "publisher": "official",
        "homepage": "https://github.com/anthropics/claude-code",
        "note": "官方示例插件，用来理解插件能做什么",
        "enabled": 1,
        "builtin": 1,
    },
    {
        "id": "anthropics-skills",
        "name": "Anthropic 技能仓库（也可作为插件市场）",
        "repo": "anthropics/skills",
        "ref": "main",
        "publisher": "official",
        "homepage": "https://github.com/anthropics/skills",
        "note": "同一个仓库既是技能仓库，也带 .claude-plugin 清单",
        "enabled": 1,
        "builtin": 1,
    },
]

DEFAULT_MARKET_ID = "claude-plugins-official"

_CATALOG_CACHE: dict[tuple, tuple[float, dict]] = {}
_FILES_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL_SECONDS = 300.0


# ═══════════════════════════════════════════════════════════════════════
#  市场注册表（~/.aigent/plugin_markets.json）
# ═══════════════════════════════════════════════════════════════════════

def load_markets(path=None) -> list[dict]:
    """读市场列表（内置市场自动合并；身份字段以代码为准，用户只能改 `enabled`）。"""
    raw = read_json_lenient(path or PLUGIN_MARKETS, {}, label="plugin_markets.json")
    saved: dict[str, dict] = {}
    custom: list[dict] = []
    if isinstance(raw, dict):
        for entry in raw.get("markets") or []:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("id") or "").strip()
            if not mid:
                continue
            saved[mid] = entry
            if not _is_builtin_id(mid):
                custom.append(dict(entry))

    out: list[dict] = []
    for builtin in BUILTIN_MARKETS:
        merged = dict(builtin)
        override = saved.get(builtin["id"])
        if isinstance(override, dict):
            merged["enabled"] = override.get("enabled", builtin.get("enabled", 1))
        out.append(merged)
    out.extend(custom)
    return out


def list_markets(path=None) -> list[dict]:
    out = []
    for m in load_markets(path):
        item = dict(m)
        item["enabled"] = _enabled(item.get("enabled", 1))
        item["builtin"] = _is_builtin_id(item.get("id"))
        item["is_default"] = item.get("id") == DEFAULT_MARKET_ID
        out.append(item)
    return out


def get_market(market_id: str, path=None) -> dict | None:
    mid = (market_id or "").strip()
    for m in load_markets(path):
        if str(m.get("id")) == mid:
            return m
    return None


def save_markets(markets: list[dict], path=None) -> None:
    write_json_atomic(path or PLUGIN_MARKETS, {"markets": markets})


def validate_market(entry: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["市场配置必须是 JSON 对象"]
    mid = str(entry.get("id") or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", mid):
        errors.append("市场 id 只能用字母、数字与 . _ -，且必须以字母或数字开头")
    if not str(entry.get("name") or "").strip():
        errors.append("市场名称不能为空")
    repo = str(entry.get("repo") or "").strip()
    if not repo:
        errors.append("插件市场必须提供 repo（`owner/name` 或完整仓库 URL）")
    elif not owner_repo(repo):
        errors.append(f"无法识别的仓库地址：{repo}（应形如 owner/name 或 https://github.com/owner/name）")
    return errors


def upsert_market(entry: dict, path=None) -> list[dict]:
    """新增或更新一个市场（内置市场只要 `{id, enabled}`，见 skill_market 同款注释）。"""
    entry = dict(entry or {})
    mid = str(entry.get("id") or "").strip()
    if _is_builtin_id(mid):
        return _set_builtin_enabled(mid, entry.get("enabled", 1), path)

    errors = validate_market(entry)
    if errors:
        raise ValueError("；".join(errors))
    markets = [dict(m) for m in load_markets(path) if not _is_builtin_id(m.get("id"))]
    markets = [m for m in markets if str(m.get("id")) != mid]
    markets.append(_clean_market(entry))
    save_markets(markets, path)
    return list_markets(path)


def remove_market(market_id: str, path=None) -> tuple[list[dict], list[str]]:
    market_id = (market_id or "").strip()
    if _is_builtin_id(market_id):
        return list_markets(path), [f"「{market_id}」是内置市场，只能停用，不能删除"]
    raw = read_json_lenient(path or PLUGIN_MARKETS, {}, label="plugin_markets.json")
    markets = [dict(m) for m in (raw.get("markets") or []) if isinstance(m, dict)]
    save_markets([m for m in markets if str(m.get("id")) != market_id], path)
    return list_markets(path), []


def _set_builtin_enabled(mid: str, enabled, path=None) -> list[dict]:
    raw = read_json_lenient(path or PLUGIN_MARKETS, {}, label="plugin_markets.json")
    markets = [dict(m) for m in (raw.get("markets") or []) if isinstance(m, dict)]
    for m in markets:
        if str(m.get("id")) == mid:
            m["enabled"] = enabled
            break
    else:
        markets.append({"id": mid, "enabled": enabled})
    save_markets(markets, path)
    return list_markets(path)


def _clean_market(entry: dict) -> dict:
    keys = ("id", "name", "repo", "ref", "publisher", "homepage", "note", "enabled")
    out = {k: entry[k] for k in keys if k in entry}
    out["enabled"] = 1 if _enabled(out.get("enabled", 1)) else 0
    return out


def _is_builtin_id(market_id) -> bool:
    mid = str(market_id or "").strip()
    return any(b["id"] == mid for b in BUILTIN_MARKETS)


def _enabled(value) -> bool:
    return value not in (0, "0", False, "false")


# ═══════════════════════════════════════════════════════════════════════
#  目录（marketplace.json）
# ═══════════════════════════════════════════════════════════════════════

def load_catalog(market: dict) -> dict | list:
    """读某个市场的 `marketplace.json`。返回目录对象，失败返回 `{"error": ...}`。"""
    repo = str(market.get("repo") or "").strip()
    ref = str(market.get("ref") or "").strip() or "main"
    key = (repo, ref)
    cached = _cache_get(_CATALOG_CACHE, key)
    if cached is not None:
        return cached

    text = raw_text(repo, ref, MARKET_FILE, MARKET_FILE_MAX_BYTES)
    if text is None:
        owner = owner_repo(repo)
        return {"error": f"读不到 {owner or repo} 的 {MARKET_FILE} —— "
                         f"该仓库可能不是插件市场（市场 = 仓库根目录放这份清单）"}
    import json  # noqa: PLC0415 - 只在解析这一处需要

    try:
        data = json.loads(text)
    except ValueError as exc:
        return {"error": f"{MARKET_FILE} 不是合法 JSON：{exc}"}
    if not isinstance(data, dict):
        return {"error": f"{MARKET_FILE} 顶层不是 JSON 对象"}
    _cache_put(_CATALOG_CACHE, key, data)
    return data


def search(market_id: str, query: str = "", cursor: str = "",
           limit: int | None = None, path=None) -> dict:
    """搜索指定市场的插件。返回 `{items, next_cursor, market_id, query, catalog_name,
    total, error, elapsed_ms}`。**绝不抛异常。**"""
    started = time.time()
    mid = (market_id or DEFAULT_MARKET_ID).strip()
    q = (query or "").strip()
    try:
        size = max(1, min(50, int(limit))) if limit else 20
    except (TypeError, ValueError):
        size = 20

    market = get_market(mid, path)
    if market is None:
        return _fail(mid, q, f"没有名为「{mid}」的插件市场", started)
    if not _enabled(market.get("enabled", 1)):
        return _fail(mid, q, f"插件市场「{market.get('name') or mid}」已停用", started)

    try:
        catalog = load_catalog(market)
    except Exception as exc:  # noqa: BLE001
        log.error("插件目录读取失败 %s：%s: %s", mid, type(exc).__name__, exc)
        return _fail(mid, q, f"读取市场失败：{type(exc).__name__}: {exc}", started)
    if isinstance(catalog, dict) and catalog.get("error"):
        return _fail(mid, q, str(catalog["error"]), started)

    rows = catalog.get("plugins") if isinstance(catalog, dict) else None
    if not isinstance(rows, list):
        rows = []
    items = [_normalize(market, row) for row in rows if isinstance(row, dict)]
    items = [it for it in items if it is not None][:MAX_PLUGINS_PER_MARKET]

    if q:
        low = q.lower()
        items = [it for it in items if low in it["_haystack"]]
    for it in items:
        it.pop("_haystack", None)

    try:
        offset = max(0, int(cursor)) if str(cursor or "").strip() else 0
    except (TypeError, ValueError):
        offset = 0
    page = items[offset:offset + size]
    next_cursor = str(offset + size) if offset + size < len(items) else ""

    return {
        "items": page,
        "next_cursor": next_cursor,
        "market_id": mid,
        "market_name": market.get("name") or mid,
        "catalog_name": str(catalog.get("name") or market.get("name") or mid)
        if isinstance(catalog, dict) else mid,
        "catalog_owner": _owner_name(catalog) if isinstance(catalog, dict) else "",
        "query": q,
        "total": len(items),
        "error": "",
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _fail(mid: str, query: str, msg: str, started: float) -> dict:
    return {"items": [], "next_cursor": "", "market_id": mid, "market_name": mid,
            "catalog_name": "", "catalog_owner": "", "query": query, "total": 0,
            "error": msg, "elapsed_ms": int((time.time() - started) * 1000)}


def _owner_name(catalog: dict) -> str:
    owner = catalog.get("owner")
    if isinstance(owner, dict):
        return str(owner.get("name") or "").strip()
    if isinstance(owner, str):
        return owner.strip()
    return ""


def _normalize(market: dict, row: dict) -> dict | None:
    """市场条目 → 前端要的形状。结构不认识就返回 None（静默跳过这一条）。"""
    name = str(row.get("name") or "").strip()
    if not name:
        return None
    src = row.get("source")
    repo, ref, prefix, kind, reason = _source_of(src, market)

    version = str(row.get("version") or "").strip()
    author = row.get("author")
    if isinstance(author, dict):
        author = str(author.get("name") or "").strip()
    else:
        author = str(author or "").strip()

    publisher = market.get("publisher") or _publisher_of(repo or str(market.get("repo") or ""))
    haystack = " ".join([
        name, str(row.get("displayName") or ""),
        str(row.get("description") or ""), str(row.get("category") or ""),
        " ".join(_as_tags(row.get("keywords"))), " ".join(_as_tags(row.get("tags"))),
        repo,
    ]).lower()

    return {
        "id": f"{market['id']}:{name}",
        "market_id": market["id"],
        "market_name": market.get("name") or market["id"],
        "name": name,
        "display_name": str(row.get("displayName") or "").strip() or name,
        "description": str(row.get("description") or "").strip(),
        "version": version,
        "author": author,
        "category": str(row.get("category") or "").strip(),
        "tags": _as_tags(row.get("keywords")) or _as_tags(row.get("tags")),
        "homepage": str(row.get("homepage") or "").strip(),
        "publisher": publisher,
        "source_kind": kind,
        "source_label": _SOURCE_LABEL.get(kind, kind or "未知"),
        "repo": repo,
        "ref": ref,
        "path": prefix,
        "installable": not reason,
        "reason": reason,
        "declared_skills": _as_tags(row.get("skills")),
        "_haystack": haystack,
        # 原始条目**必须原样带回**：`resolve` 靠它重算 source（不依赖前端的任何解析）
        "raw": row,
    }


_SOURCE_LABEL = {
    "relpath": "市场仓库内",
    "url": "独立仓库",
    "git-subdir": "仓库子目录",
    "github": "GitHub 仓库",
    "invalid": "无法识别",
}


def _source_of(src, market: dict) -> tuple[str, str, str, str, str]:
    """`source` 字段 → `(repo, ref, prefix, kind, reason)`。识别不了时 reason 非空。

    兼容四种写法（前三种实测覆盖官方市场 314 条的全部）：
      · 字符串 `"./plugins/x"`                 → 就在市场仓库里
      · `{source:"url", url, sha}`             → 独立仓库
      · `{source:"git-subdir", url, path, ref, sha}` → 仓库的某个子目录
      · `{source:"github", repo}`              → GitHub 简写
    绝对路径 / `../`（本地市场专用）**明确不支持**并给出原因 —— 静默忽略会让用户
    以为"这个插件消失了"。
    """
    market_repo = str(market.get("repo") or "")
    market_ref = str(market.get("ref") or "main")

    if isinstance(src, str):
        s = src.strip()
        if not s:
            return "", "", "", "invalid", "该条目没有声明 source"
        if s.startswith("/") or s.startswith("~") or ".." in s.split("/"):
            return "", "", "", "invalid", (
                "这是指向本地路径的插件（本地市场专用），远程安装不支持")
        prefix = safe_relpath(s)
        if prefix is None:
            return "", "", "", "invalid", f"无法识别的 source 路径：{s}"
        return market_repo, market_ref, prefix, "relpath", ""

    if isinstance(src, dict):
        kind = str(src.get("source") or "").strip()
        if kind == "github":
            repo_raw = str(src.get("repo") or "").strip()
            repo = owner_repo(repo_raw)
            if not repo:
                return "", "", "", "invalid", f"无法识别的仓库：{repo_raw}"
            return repo, str(src.get("sha") or src.get("ref") or market_ref), "", "github", ""
        if kind in ("url", "git"):
            repo = owner_repo(str(src.get("url") or ""))
            if not repo:
                return "", "", "", "invalid", f"无法识别的仓库地址：{src.get('url')}"
            return repo, str(src.get("sha") or src.get("ref") or market_ref), "", "url", ""
        if kind == "git-subdir":
            repo = owner_repo(str(src.get("url") or ""))
            if not repo:
                return "", "", "", "invalid", f"无法识别的仓库地址：{src.get('url')}"
            prefix = safe_relpath(str(src.get("path") or ""))
            if prefix is None:
                return "", "", "", "invalid", f"无法识别的子目录：{src.get('path')}"
            return (repo, str(src.get("sha") or src.get("ref") or market_ref),
                    prefix, "git-subdir", "")
        return "", "", "", "invalid", f"暂不支持的 source 类型：{kind or '(空)'}"

    return "", "", "", "invalid", "该条目没有声明 source"


def _publisher_of(repo: str) -> str:
    head = (owner_repo(repo) or "").split("/")[0].lower()
    if head in ("anthropics", "modelcontextprotocol"):
        return "official"
    return "community"


def _as_tags(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


# ═══════════════════════════════════════════════════════════════════════
#  抓取：插件的全部文件
# ═══════════════════════════════════════════════════════════════════════

def fetch_files(market_id: str, item: dict, path=None) -> dict:
    """把插件对应的**全部文件**取回来 → `{相对路径: str|bytes}`（相对插件根）。

    失败返回 `{"__error__": "原因"}`（不抛）—— 调用方在无兜底 try 的链路上。
    """
    mid = str(item.get("market_id") or market_id or "").strip()
    key = (mid, str(item.get("id") or ""))
    cached = _cache_get(_FILES_CACHE, key)
    if cached is not None:
        return cached

    repo = str(item.get("repo") or "")
    ref = str(item.get("ref") or "main")
    prefix = str(item.get("path") or "").strip("/")
    if not repo:
        return {"__error__": item.get("reason") or "该条目没有可用的仓库地址"}

    try:
        files = _fetch_dir(repo, ref, prefix)
    except Exception as exc:  # noqa: BLE001
        log.error("插件抓取失败 %s：%s: %s", item.get("id"), type(exc).__name__, exc)
        return {"__error__": f"抓取失败：{type(exc).__name__}: {exc}"}
    if isinstance(files, dict) and files.get("__error__"):
        return files
    if f"{MANIFEST_DIR}/{MANIFEST_NAME}" not in files:
        return {"__error__": (
            f"该插件里没有 {MANIFEST_DIR}/{MANIFEST_NAME}，不是有效的 Claude Code 插件")}

    _cache_put(_FILES_CACHE, key, files)
    return files


def _fetch_dir(repo: str, ref: str, prefix: str) -> dict:
    """抓某个仓库（可选子目录）下的全部文件。用一次 tree + 并行 raw 抓取。"""
    from skill_market import _git_tree  # noqa: PLC0415 - 复用同一条 GitHub 树接口

    tree = _git_tree(repo, ref)
    if isinstance(tree, dict) and tree.get("error"):
        return tree
    p = f"{prefix}/" if prefix else ""
    rels = []
    for full in tree:
        if not isinstance(full, str) or not full.startswith(p) or full == p:
            continue
        rel = full[len(p):]
        if not rel or rel.endswith("/"):
            continue
        if any(seg in ("node_modules", ".git", "__pycache__", ".venv") for seg in rel.split("/")):
            continue
        if safe_relpath(rel) is None:
            continue
        rels.append(rel)
    if len(rels) > MAX_PLUGIN_FILES:
        log.warning("插件 %s 文件数 %d 超过上限，只取前 %d 个", repo, len(rels), MAX_PLUGIN_FILES)
        rels = rels[:MAX_PLUGIN_FILES]
    if not rels:
        return {"__error__": f"{repo} 的 {prefix or '/'} 下没有任何文件"}

    out: dict[str, object] = {}
    errors: list[str] = []

    def _one(rel: str) -> tuple[str, object]:
        raw = raw_bytes(repo, ref, f"{p}{rel}")
        return rel, raw

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for rel, raw in pool.map(_one, rels):
            if raw is None:
                errors.append(rel)
                continue
            out[rel] = _decode(raw, rel)
    if not out:
        return {"__error__": f"没能取到 {repo} 的任何文件内容"}
    if errors:
        log.warning("插件 %s 有 %d 个文件没取到（已跳过）：%s",
                    repo, len(errors), ", ".join(errors[:5]))
    return out


_TEXT_EXT = {
    ".md", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh", ".bash", ".zsh",
    ".ps1", ".cmd", ".bat", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".php", ".swift", ".sql", ".css", ".scss", ".html",
    ".htm", ".xml", ".svg", ".vue", ".svelte", ".lua", ".r", ".pl", ".dart",
    ".gitignore", ".editorconfig", ".env.example", ".lock",
}


def _decode(raw: bytes, rel: str) -> object:
    """文本按 utf-8 解出来存字符串；二进制（图片 / 字体 / 压缩包）原样存字节。

    为什么必须区分：`plugin_store` 落盘时对 `str` 用 `write_text`、对 `bytes` 用
    `write_bytes`。把一张 PNG 当字符串写出去会得到一个损坏的文件 —— 而且**不会报错**，
    只会在用的时候才发现图挂了。
    """
    import os  # noqa: PLC0415

    ext = os.path.splitext(rel)[1].lower()
    if ext and ext not in _TEXT_EXT:
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw


# ═══════════════════════════════════════════════════════════════════════
#  翻译：条目 → 安装计划（**不落盘**）
# ═══════════════════════════════════════════════════════════════════════

def resolve(market_id: str, item: dict, existing_names: list[str] | None = None,
            path=None) -> dict:
    """把市场条目转成**安装确认页**要展示的东西。

    这是整条链路上**唯一**的安全闸门，两条不可妥协的约束：

    1. **必须列出该插件将贡献的全部组件**（技能 / 命令 / 子智能体 / 钩子 /
       MCP 服务器 / 语言服务器），并**明确标注哪些本期真正生效**。技能是真注入
       系统提示的；钩子与 MCP 服务器本期**只入库不执行** —— 这个区分必须写在界面上，
       否则用户会以为装上就等于那些代码开始跑了。
    2. **必须展示 `plugin.json` 原文**：这是"这个插件自称是什么"的唯一凭据。

    返回 `{ok, name, ..., components, wired, plugin_json, skill_previews, files[],
    warnings, unsupported, error, meta}`。
    """
    if not isinstance(item, dict):
        return plan_fail("条目格式非法")
    mid = str(item.get("market_id") or market_id or "").strip()
    market = get_market(mid, path)
    if market is None:
        return plan_fail(f"没有名为「{mid}」的插件市场")
    if item.get("installable") is False:
        return plan_fail(str(item.get("reason") or "该条目无法一键安装"))

    warnings: list[str] = []
    try:
        files = fetch_files(mid, item, path)
    except Exception as exc:  # noqa: BLE001
        return plan_fail(f"抓取失败：{type(exc).__name__}: {exc}")
    if isinstance(files, dict) and files.get("__error__"):
        return plan_fail(str(files["__error__"]))

    manifest_text = files.get(f"{MANIFEST_DIR}/{MANIFEST_NAME}")
    if not isinstance(manifest_text, str):
        return plan_fail(f"读不到 {MANIFEST_DIR}/{MANIFEST_NAME} 的内容")
    import json  # noqa: PLC0415

    try:
        manifest = json.loads(manifest_text)
    except ValueError as exc:
        return plan_fail(f"{MANIFEST_NAME} 不是合法 JSON：{exc}")
    if not isinstance(manifest, dict):
        return plan_fail(f"{MANIFEST_NAME} 顶层不是 JSON 对象")

    declared_name = str(manifest.get("name") or "").strip() or item.get("name") or ""
    suggested = declared_name or item.get("name") or "plugin"
    name = unique_name(suggested, existing_names or [])
    if name != suggested:
        warnings.append(f"已存在同名插件或名称需归一化，建议名改为「{name}」")

    components = inventory_of_files(
        {k: v for k, v in files.items() if isinstance(k, str)}, manifest)
    file_list = []
    for rel in sorted(k for k in files if isinstance(k, str)):
        content = files[rel]
        size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        file_list.append({"path": rel, "size": size})

    # 贡献技能的首行描述 —— 让用户在确认页就能看出"这些技能会教模型做什么"
    skill_previews = []
    for skill in components.get("skills", []):
        rel = f"skills/{skill}/SKILL.md" if skill != "." else "SKILL.md"
        text = files.get(rel)
        if not isinstance(text, str):
            skill_previews.append({"name": skill, "description": ""})
            continue
        from skill_store import parse_frontmatter  # noqa: PLC0415

        fm, body = parse_frontmatter(text)
        desc = str(fm.get("description") or "").strip() or _first_line(body)
        skill_previews.append({"name": skill, "description": desc})

    if components.get("hooks"):
        warnings.append(
            f"该插件带 {len(components['hooks'])} 组钩子。钩子会在本机执行命令，"
            "但**本期未接入运行时**（只入库不执行），详见安装后的组件说明。")
    if components.get("mcp_servers"):
        warnings.append(
            f"该插件带 {len(components['mcp_servers'])} 个 MCP 服务器声明，"
            "**本期未接入运行时**（不会自动连接）。可在「MCP」页手工添加。")

    return {
        "ok": True,
        "name": name,
        "plugin_name": declared_name or item.get("name") or "",
        "display_name": str(manifest.get("displayName") or item.get("display_name")
                            or declared_name or name),
        "description": str(manifest.get("description") or item.get("description") or ""),
        "version": str(manifest.get("version") or item.get("version") or ""),
        "author": _author_of(manifest) or str(item.get("author") or ""),
        "homepage": str(manifest.get("homepage") or item.get("homepage") or ""),
        "plugin_json": manifest_text,
        "components": components,
        "component_counts": {k: len(v) for k, v in components.items()},
        "wired": ["skills"],
        "skill_previews": skill_previews,
        "files": file_list,
        "file_count": len(file_list),
        "total_bytes": sum(f["size"] for f in file_list),
        "source_kind": item.get("source_kind") or "",
        "repo": item.get("repo") or "",
        "warnings": warnings,
        "unsupported": "",
        "error": "",
        "meta": {
            "source": "market",
            "market_id": mid,
            "market_name": market.get("name") or mid,
            "publisher": item.get("publisher") or market.get("publisher") or "",
            "market_url": str(item.get("homepage") or ""),
            "repo": _repo_url(item.get("repo") or ""),
        },
    }


def plan_fail(msg: str) -> dict:
    return {"ok": False, "name": "", "plugin_name": "", "display_name": "",
            "description": "", "version": "", "author": "", "homepage": "",
            "plugin_json": "", "components": {}, "component_counts": {},
            "wired": ["skills"], "skill_previews": [], "files": [], "file_count": 0,
            "total_bytes": 0, "source_kind": "", "repo": "", "warnings": [],
            "unsupported": msg, "error": "", "meta": {}}


def _author_of(manifest: dict) -> str:
    author = manifest.get("author")
    if isinstance(author, str):
        return author.strip()
    if isinstance(author, dict):
        return str(author.get("name") or "").strip()
    return ""


def _repo_url(repo: str) -> str:
    r = owner_repo(repo)
    return f"https://github.com/{r}" if r else ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s.lstrip("#").strip()
    return ""


def _cache_get(cache: dict, key: tuple):
    with _CACHE_LOCK:
        hit = cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        if hit:
            cache.pop(key, None)
    return None


def _cache_put(cache: dict, key: tuple, value) -> None:
    with _CACHE_LOCK:
        cache[key] = (time.time() + CACHE_TTL_SECONDS, value)
        if len(cache) > 60:
            cache.clear()
