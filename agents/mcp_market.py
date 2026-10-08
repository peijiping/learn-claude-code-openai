#!/usr/bin/env python3
"""
mcp_market.py - MCP 市场（官方 Registry 客户端 + 条目→配置翻译层）

数据源：`https://registry.modelcontextprotocol.io`（免鉴权 REST，无需 API key）。
它由 Linux Foundation 下的 AAIF 托管，**至今仍是 preview** —— 实测同一份响应里
同时存在两版 schema（`2025-09-29` 与 `2025-12-11`）。所以本模块的翻译层一律
「尽力而为 + 失败降级成"请手动填写"」，绝不对任何字段做硬假设。

⚠️ 安全前提（必须原样告知用户，见 docs/frontend/23）：
registry 只做**命名空间所有权校验**（DNS TXT / GitHub 组织），**没有代码审计、
没有漏洞扫描**。被列进去 ≠ 安全。这就是安装前必须有确认弹窗的原因。

两条硬约束（改之前先读）：
1. **任何异常都转成 `error` 文案返回，绝不上抛** —— 调用方在 ws_bridge 的命令
   分发链里，那条链**没有兜底 try/except**，抛出去会直接掀掉整条 WS 连接。
2. **所有网络调用都必须由调用方放进 `asyncio.to_thread`** —— 实测搜索耗时
   0.9s ~ 17s（波动极大），在事件循环里同步等会把所有会话的流式事件一起卡住。
"""

import json
import os
import threading
import time
from typing import Any

import httpx

from logger import get_logger

log = get_logger("mcp_market")

DEFAULT_MARKET_URL = "https://registry.modelcontextprotocol.io"
API_PATH = "/v0.1/servers"

# 搜索结果内存缓存（TTL 秒）。**只做内存、不落盘** —— 官方 schema 还在 preview
# 期间漂移，把结构固化到磁盘上等于把坏结构留下来。缓存只为解决两件事：
# ① 翻页/重搜重复打网络；② 实测单次搜索可达 17s，同一关键词重入必须秒回。
CACHE_TTL_SECONDS = float(os.environ.get("MCP_MARKET_CACHE_TTL") or "300")
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()

# 能一键安装的包类型 → 运行时命令。`runtimeHint` 字段实测**经常缺失**（10 条里 6 条没有），
# 所以必须能按 registryType 推导，不能只信 hint。
_RUNNER_BY_REGISTRY = {"npm": "npx", "pypi": "uvx"}
# 暂不支持的包类型 → 给用户的原因（oci 要 docker、nuget 要 dotnet、mcpb 要专用运行时）
_UNSUPPORTED_REASON = {
    "oci": "需要 Docker 镜像运行时，暂不支持一键安装，请手动填写配置",
    "nuget": "需要 .NET 运行时，暂不支持一键安装，请手动填写配置",
    "mcpb": "这是 MCP Bundle 包，需要专用运行时，请手动填写配置",
}


def market_url() -> str:
    return (os.environ.get("MCP_MARKET_URL") or DEFAULT_MARKET_URL).rstrip("/")


def market_timeout() -> float:
    """单次请求超时。实测最慢一次约 17s → 默认给 20s，别按"HTTP 应该很快"设。"""
    try:
        return float(os.environ.get("MCP_MARKET_TIMEOUT") or "20")
    except (TypeError, ValueError):
        return 20.0


def page_size() -> int:
    try:
        return max(1, min(50, int(os.environ.get("MCP_MARKET_PAGE_SIZE") or "20")))
    except (TypeError, ValueError):
        return 20


# ═══════════════════════════════════════════════════════════════════════
#  搜索
# ═══════════════════════════════════════════════════════════════════════

def search(query: str = "", cursor: str = "", limit: int | None = None) -> dict:
    """查官方 registry。返回 `{items, next_cursor, query, error, elapsed_ms}`。

    **绝不抛异常** —— 网络/超时/schema 变动都收敛成 `error` 文案，前端在面板里
    内联展示，页面不崩、WS 不断。
    """
    started = time.time()
    q = (query or "").strip()
    cur = (cursor or "").strip()
    try:
        size = int(limit) if limit else page_size()
    except (TypeError, ValueError):
        size = page_size()
    size = max(1, min(50, size))
    key = (q, cur, size)

    cached = _cache_get(key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out

    params: dict[str, Any] = {"limit": size, "version": "latest"}
    if q:
        params["search"] = q
    if cur:
        params["cursor"] = cur

    try:
        with httpx.Client(timeout=market_timeout(), follow_redirects=True) as client:
            resp = client.get(market_url() + API_PATH, params=params,
                              headers={"Accept": "application/json"})
            resp.raise_for_status()
            raw = resp.json()
    except httpx.TimeoutException:
        return _fail(f"请求市场超时（{market_timeout():.0f}s）。可稍后重试，或用「手动添加」直接配置。", started)
    except httpx.HTTPStatusError as exc:
        return _fail(f"市场返回 HTTP {exc.response.status_code}。", started)
    except httpx.HTTPError as exc:
        return _fail(f"无法连接市场：{type(exc).__name__}。请检查网络或代理设置。", started)
    except (json.JSONDecodeError, ValueError) as exc:
        return _fail(f"市场响应不是合法 JSON：{exc}", started)

    if not isinstance(raw, dict):
        return _fail("市场响应结构异常（顶层不是对象）。", started)

    items_raw = raw.get("servers")
    if not isinstance(items_raw, list):
        # schema 漂移兜底：老版本可能把列表放在别的键下
        for alt in ("data", "items", "results"):
            if isinstance(raw.get(alt), list):
                items_raw = raw[alt]
                break
        else:
            items_raw = []

    items: list[dict] = []
    for entry in items_raw:
        norm = _normalize_item(entry)
        if norm is not None:
            items.append(norm)

    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    next_cursor = meta.get("nextCursor") if isinstance(meta.get("nextCursor"), str) else ""
    payload = {
        "items": items,
        "next_cursor": next_cursor or "",
        "query": q,
        "error": "",
        "cached": False,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    _cache_put(key, payload)
    log.info("市场搜索 %r → %d 条（%dms）", q or "(全部)", len(items), payload["elapsed_ms"])
    return payload


def _fail(msg: str, started: float) -> dict:
    return {"items": [], "next_cursor": "", "query": "", "error": msg,
            "cached": False, "elapsed_ms": int((time.time() - started) * 1000)}


def _cache_get(key: tuple) -> dict | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        if hit:
            _CACHE.pop(key, None)
    return None


def _cache_put(key: tuple, payload: dict) -> None:
    with _CACHE_LOCK:
        # 容量上限：搜索结果本身不大，但翻页会持续累积
        if len(_CACHE) > 60:
            now = time.time()
            for k in [k for k, v in _CACHE.items() if v[0] <= now]:
                _CACHE.pop(k, None)
            while len(_CACHE) > 60:
                _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.time() + CACHE_TTL_SECONDS, payload)


# ═══════════════════════════════════════════════════════════════════════
#  条目归一化
# ═══════════════════════════════════════════════════════════════════════

def _normalize_item(entry) -> dict | None:
    """把 registry 条目转成前端要的形状。结构不认识就返回 None（静默跳过该条）。

    实测外层是 `{"server": {...server.json...}, "_meta": {...}}`，
    不是裸 server.json —— 这里同时容错裸形态（老 schema / 别的镜像）。
    """
    if not isinstance(entry, dict):
        return None
    srv = entry.get("server") if isinstance(entry.get("server"), dict) else entry
    name = srv.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    short = name.split("/")[-1] or name

    meta = entry.get("_meta")
    official_meta = {}
    if isinstance(meta, dict):
        om = meta.get("io.modelcontextprotocol.registry/official")
        official_meta = om if isinstance(om, dict) else {}

    packages = [p for p in (srv.get("packages") or []) if isinstance(p, dict)]
    remotes = [r for r in (srv.get("remotes") or []) if isinstance(r, dict)]

    kinds: list[str] = []
    for p in packages:
        t = _transport_type_of_package(p)
        if t not in kinds:
            kinds.append(t)
    for r in remotes:
        t = str(r.get("type") or "").strip()
        if t and t not in kinds:
            kinds.append(t)
    if not kinds and packages:
        kinds.append("stdio")

    installable, reason = _installability(packages, remotes)

    return {
        "id": name,
        "name": name,
        "short_name": short,
        "title": (srv.get("title") or "").strip() or short,
        "description": (srv.get("description") or "").strip(),
        "version": str(srv.get("version") or ""),
        "repository": _repo_url(srv.get("repository")),
        "kinds": kinds,
        "installable": installable,
        "reason": reason,
        "publisher": publisher_of(name),
        "published_at": str(official_meta.get("publishedAt") or ""),
        "status": str(official_meta.get("status") or ""),
        # 只带 resolve 真正需要的两个数组（整份 server.json 会白白放大载荷）
        "packages": packages[:5],
        "remotes": remotes[:5],
    }


def publisher_of(name: str) -> str:
    """信任档：官方命名空间 / GitHub 组织 / 自有域名。

    **这只是"谁发布"的分级，不是"是否安全"的评级** —— 三档都只经过命名空间
    所有权校验，都没有代码审计（UI 上必须这么讲）。
    """
    head = (name or "").split("/")[0].lower()
    if head.startswith("io.modelcontextprotocol") or head.startswith("com.modelcontextprotocol"):
        return "official"
    if head.startswith("io.github."):
        return "community"
    return "domain-verified"


def _repo_url(repo) -> str:
    """`repository` 字段形态不一：实测既可能是字符串，也可能是 {url, source, subfolder}。"""
    if isinstance(repo, str):
        return repo.strip()
    if isinstance(repo, dict):
        return str(repo.get("url") or "").strip()
    return ""


def _transport_type_of_package(pkg: dict) -> str:
    t = pkg.get("transport")
    if isinstance(t, dict) and isinstance(t.get("type"), str) and t["type"].strip():
        return t["type"].strip()
    return "stdio"


def _installability(packages: list[dict], remotes: list[dict]) -> tuple[bool, str]:
    for p in packages:
        if str(p.get("registryType") or "") in _RUNNER_BY_REGISTRY:
            return True, ""
    if remotes:
        return True, ""
    if not packages:
        return False, "该条目没有声明任何可用的包或远程端点"
    kinds = [str(p.get("registryType") or "?") for p in packages]
    first = kinds[0]
    return False, _UNSUPPORTED_REASON.get(first, f"暂不支持 {first} 类型的包，请手动填写配置")


# ═══════════════════════════════════════════════════════════════════════
#  翻译：市场条目 → mcpServers 条目（纯函数，**不落盘**）
# ═══════════════════════════════════════════════════════════════════════

def resolve(item: dict, existing_names: list[str] | None = None) -> dict:
    """把市场条目翻译成 `mcpServers` 条目，供**安装确认弹窗**展示。

    刻意拆成独立一步、且**不落盘**：用户必须先看到将要写入的 `command`/`args`
    原文（那是"要执行什么代码"的唯一凭据），确认后才走 `mcp_server_upsert`。

    返回 `{ok, name, config, env_required, warnings, unsupported, error}`。
    """
    if not isinstance(item, dict):
        return _plan_fail("条目格式非法")
    packages = [p for p in (item.get("packages") or []) if isinstance(p, dict)]
    remotes = [r for r in (item.get("remotes") or []) if isinstance(r, dict)]
    warnings: list[str] = []

    cfg: dict | None = None
    env_required: list[dict] = []
    package_args: list[str] = []
    pkg_info: dict | None = None
    unsupported = ""

    # 优先 stdio 包（能力最完整、与远程等价但无网络依赖）；npm 优先于 pypi
    chosen = None
    for want in ("npm", "pypi"):
        for p in packages:
            if str(p.get("registryType") or "") == want:
                chosen = p
                break
        if chosen:
            break

    if chosen is not None:
        try:
            cfg, env_required, w, package_args = _config_from_package(chosen)
            warnings.extend(w)
            # 「下载到本地」用的包坐标。**刻意在后端给出**：前端若自己去读
            # `item.packages` 就会演化出第二处解析规则（见本模块 docstring 的
            # "翻译规则只应有一处"）。`local_installable` 目前只认 npm ——
            # PyPI 侧要落 uv 工具链，口径未定（见 mcp_installer 模块 docstring）。
            rtype = str(chosen.get("registryType") or "")
            pkg_info = {
                "registry_type": rtype,
                "identifier": str(chosen.get("identifier") or "").strip(),
                "version": str(chosen.get("version") or "").strip(),
                "local_installable": rtype == "npm",
            }
        except Exception as exc:  # noqa: BLE001 - 推导失败要能退到远程端点，不能整条失败
            log.warning("包启动参数推导失败（%s）：%s", chosen.get("registryType"), exc)
            warnings.append(f"该包的启动参数无法自动推导（{exc}），请手动核对或改用远程端点")
            cfg = None
    elif packages:
        rtype = str(packages[0].get("registryType") or "?")
        unsupported = _UNSUPPORTED_REASON.get(
            rtype, f"暂不支持 {rtype} 类型的包，请手动填写配置")

    if cfg is None and not unsupported and remotes:
        remote = remotes[0]
        rtype = str(remote.get("type") or "streamable-http").strip()
        url = str(remote.get("url") or "").strip()
        if not url:
            unsupported = "该条目的远程端点没有 url，请手动填写配置"
        else:
            if rtype not in ("sse", "streamable-http"):
                warnings.append(f"远程传输类型 {rtype!r} 不在已知范围内，可能需要手动调整")
            cfg = {"type": rtype, "url": url}
            headers = remote.get("headers")
            if isinstance(headers, list):
                collected = {}
                skipped = 0
                for h in headers:
                    if not isinstance(h, dict) or not isinstance(h.get("name"), str):
                        continue
                    value = str(h.get("value") or "")
                    # **空值 header 一律不写**：实测有条目声明 `Payment-Signature: ""`
                    # （占位符），原样写进去会让服务端收到空签名而拒绝请求 ——
                    # 比"少一个 header"糟得多，后者用户还能在表单里补。
                    if not value.strip():
                        skipped += 1
                        continue
                    collected[h["name"]] = value
                if collected:
                    cfg["headers"] = collected
                    warnings.append("该远程端点声明了固定请求头，已一并写入")
                if skipped:
                    warnings.append(
                        f"{skipped} 个请求头只有占位空值，已跳过；若连不上请手动补全（通常是密钥）")

    if cfg is None:
        return _plan_fail(unsupported or "无法从该条目推导出可用配置，请手动填写")

    name = _unique_name(item, existing_names or [])
    if name != (item.get("short_name") or ""):
        warnings.append(f"已存在同名条目，建议名改为「{name}」")
    if item.get("status") and item["status"] != "active":
        warnings.append(f"市场标记该条目状态为 {item['status']}，请确认是否仍可用")

    return {
        "ok": True,
        "name": name,
        "config": cfg,
        "env_required": env_required,
        # 「下载到本地」时只保留这部分参数（runner 与包名会被换成包内 bin 的绝对路径）
        "package_args": list(package_args),
        # 包坐标（npm/pypi 的 identifier + version + 能否本地安装）；远程端点时为 None
        "pkg": pkg_info,
        "warnings": warnings,
        "unsupported": "",
        "error": "",
    }


def _plan_fail(msg: str) -> dict:
    return {"ok": False, "name": "", "config": {}, "env_required": [],
            "package_args": [], "pkg": None,
            "warnings": [], "unsupported": msg, "error": ""}


def _arg_values(raw) -> list[str]:
    """把 server.json 的 `*Arguments` 数组转成命令行参数列表。

    实测形态：`[{"value": "-y", "type": "positional"}, ...]`；老 schema 里也可能
    直接是字符串数组。两种都收，其余形态原样 `str()` 保留（宁可多一个怪参数，
    也不要静默丢掉用户看不见的启动参数 —— 那会让"确认弹窗展示的原文"失真）。
    """
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for a in raw:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            v = a.get("value")
            if v is None:
                continue
            if a.get("type") == "named" and a.get("name"):
                out.append(f"--{a['name']}")
            out.append(str(v))
    return out


def _config_from_package(pkg: dict) -> tuple[dict, list[dict], list[str], list[str]]:
    """npm / pypi 包 → stdio 配置。

    参数顺序按 npx/uvx 的实际约定：`<runner> <runtimeArguments> <packageSpec> <packageArguments>`。
    实测样本 `runtimeArguments=[{"value":"-y"}]` 正好印证 `-y` 在包名之前。

    第 4 个返回值 `package_args` 是**只属于服务本身**的那部分参数（不含 runner 与包名）
    —— 「下载到本地」时 `command` 换成包内 bin 的绝对路径，此时 `-y` 和包名都不再
    适用，但这几个参数必须原样保留。让前端去切 `cfg["args"]` 会制造第二处解析规则，
    所以在这里一次性给出（docs/frontend/23 §本地安装）。
    """
    warnings: list[str] = []
    rtype = str(pkg.get("registryType") or "")
    identifier = str(pkg.get("identifier") or "").strip()
    version = str(pkg.get("version") or "").strip()
    runner = str(pkg.get("runtimeHint") or "").strip() or _RUNNER_BY_REGISTRY.get(rtype, "")
    if not identifier:
        raise ValueError(f"{rtype} 包缺少 identifier")
    args = _arg_values(pkg.get("runtimeArguments"))
    package_args = _arg_values(pkg.get("packageArguments"))

    if rtype == "pypi":
        spec = f"{identifier}=={version}" if version else identifier
        warnings.append("PyPI 包由 uvx 运行；若该包需要指定入口命令，请手动调整参数")
    else:
        spec = f"{identifier}@{version}" if version else identifier
    args.append(spec)
    args.extend(package_args)

    cfg: dict = {"type": "stdio", "command": runner, "args": args}

    env_required: list[dict] = []
    raw_env = pkg.get("environmentVariables")
    if isinstance(raw_env, list):
        for e in raw_env:
            if not isinstance(e, dict):
                continue
            key = e.get("name")
            if not isinstance(key, str) or not key.strip():
                continue
            env_required.append({
                "name": key.strip(),
                "description": str(e.get("description") or ""),
                "required": bool(e.get("isRequired")),
                # isSecret 只影响 UI 是否用密码框 —— 本期密钥仍明文落盘（0600），
                # 用 ${VAR} 分离到 config.json 属后续增量（见 23 篇）。
                "secret": bool(e.get("isSecret")),
                "default": str(e.get("default") or ""),
            })
    if any(e["required"] for e in env_required):
        warnings.append("该服务需要必填环境变量，未填写可能连不上")
    return cfg, env_required, warnings, package_args


def _unique_name(item: dict, existing: list[str]) -> str:
    """`io.github.acme/my-server` → `my-server`；撞名则 `my-server-2`、`-3`…"""
    base = (item.get("short_name") or item.get("name") or "mcp-server").strip()
    base = base or "mcp-server"
    taken = {n for n in existing if isinstance(n, str)}
    if base not in taken:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in taken:
            return cand
    return base
