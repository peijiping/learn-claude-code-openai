#!/usr/bin/env python3
"""
mcp_store.py - MCP 服务器配置 store（设置页「MCP」面板的读写门面）

背景：`mcp_manager.py` 的 `load_config()` **只能读**，没有任何写入 API；设置页
需要一个能安全增删改 `~/.aigent/mcp/mcp_servers.json` 的落点。本模块就是它，
与 `permission.PermissionStore` 同构（原子写 + 锁 + 同一出口归一化）。

三个刻意的设计决定（改之前先读，见 docs/frontend/23）：

1. **UI 数据源 = 原始文件，不是 `load_config()` 的返回值。**
   `load_config()` 会把 `enable: 0` 的条目**过滤掉**（不连接、不可枚举）。
   若拿它当列表数据源，被禁用的 server 就永远看不见 —— 也就无法被重新启用。
   所以这里读的是「原始 mcpServers 映射」（含禁用项），运行时连接状态另由
   `mcp_manager` 叠加（见 ws_bridge 的 `_mcp_runtime_snapshot`）。

2. **元数据走旁路文件 `mcp_sources.json`，不内嵌进条目。** 理由见 paths.py 注释。

3. **文件权限 0600。** 条目 `env` / `headers` 里可能含明文 API key
   （`${VAR}` 插值机制目前没有调用方，见 docs/frontend/23 §后续增量）。

错误语义（对齐 `_save_sandbox_enabled`，ws_bridge.py:685）：
- 读：文件不存在 → `{}`；**存在但损坏 → 抛 ValueError**，绝不吞成空 dict
  （否则调用方会拿空 dict 回写，把用户手写的全部条目抹掉）。
- 写：先全量校验、无错才落盘；落盘失败抛 OSError，由调用方决定怎么回执。
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from logger import get_logger
from paths import MCP_CONFIG, MCP_SOURCES

log = get_logger("mcp_store")

# 与 mcp_manager.MCPServerSession._connect 的分派逻辑保持一致
VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")

# 会被脱敏的键名（回执里绝不回传明文；编辑时留空 = 不改）
_SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)

# 条目允许出现的键（白名单）：写入时未知键一律丢弃，保持文件干净
_ENTRY_KEYS = ("type", "command", "args", "env", "cwd", "url", "headers", "enable")

NAME_MAX_LEN = 64
SECRET_MASK = "••••••"


def is_secret_key(key: str) -> bool:
    """键名是否像密钥（用于回执脱敏）。"""
    return bool(_SECRET_KEY_RE.search(key or ""))


def is_enabled(value) -> bool:
    """`enable` 字段的启用判定 —— 与 `mcp_manager.load_config` 的过滤规则**逐字一致**。

    `load_config` 判定的是「禁用」：`value in (0, "0", False, "false")`。
    这里取反。两处口径必须同源，否则会出现「页面显示已启用、实际没连接」。
    """
    return value not in (0, "0", False, "false")


def transport_of(config: dict) -> str:
    """推断传输类型 —— 与 `MCPServerSession._connect` 的分派规则一致。

    显式 `type` 优先；否则有 `command` 走 stdio、没有则默认 streamable-http。
    """
    if not isinstance(config, dict):
        return "stdio"
    raw = config.get("type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "stdio" if "command" in config else "streamable-http"


def mask_kv(value):
    """把 `env` / `headers` 里的密钥值替换为掩码；非 dict 原样返回。"""
    if not isinstance(value, dict):
        return value
    return {k: (SECRET_MASK if is_secret_key(k) else v) for k, v in value.items()}


class McpStore:
    """`mcp_servers.json` + `mcp_sources.json` 的门面（无状态，多实例共享同一对文件）。"""

    def __init__(self, path: Path | str | None = None,
                 sources_path: Path | str | None = None):
        self.path = Path(path) if path else MCP_CONFIG
        self.sources_path = Path(sources_path) if sources_path else MCP_SOURCES
        self._lock = threading.Lock()

    # ── 读 ────────────────────────────────────────────────────────

    def load_raw(self) -> dict:
        """读原始 `mcpServers` 映射（**含 `enable: 0` 的条目**）。

        文件不存在 → `{}`；存在但损坏 → 抛 `ValueError`（见模块 docstring 第 1 条）。
        """
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as e:
            raise ValueError(f"读取失败：{e}") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败（第 {e.lineno} 行）：{e.msg}") from e
        if not isinstance(raw, dict):
            raise ValueError("顶层不是 JSON 对象（期望 {\"mcpServers\": {...}}）")
        servers = raw.get("mcpServers")
        if servers is None:
            return {}
        if not isinstance(servers, dict):
            raise ValueError("mcpServers 的值不是 JSON 对象")
        return servers

    def load_sources(self) -> dict:
        """读旁路元数据。**损坏时退化成 `{}`** —— 它只影响来源标记展示，
        不值得为它拦下整个设置页（与 `mcp_servers.json` 的严格语义刻意不同）。
        """
        if not self.sources_path.exists():
            return {}
        try:
            raw = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("mcp_sources.json 无法解析，来源标记降级为空：%s", e)
            return {}
        return raw if isinstance(raw, dict) else {}

    def list_servers(self) -> tuple[list[dict], list[str]]:
        """给 UI 的条目列表（原始配置 + 旁路元数据）。返回 `(servers, errors)`。

        `errors` 非空表示读文件失败 —— 此时 `servers` 为空，前端应展示错误而不是
        「一个 MCP 都没有」（后者会诱导用户重新添加，反而覆盖掉磁盘上还在的内容）。
        """
        try:
            raw = self.load_raw()
        except (ValueError, OSError) as e:
            log.error("MCP 配置读取失败：%s", e)
            return [], [str(e)]
        sources = self.load_sources()
        out: list[dict] = []
        for name in sorted(raw.keys()):
            cfg = raw[name] if isinstance(raw[name], dict) else {}
            meta = sources.get(name)
            meta = meta if isinstance(meta, dict) else {}
            out.append({
                "name": name,
                "enable": is_enabled(cfg.get("enable", 1)),
                "transport": transport_of(cfg),
                "command": cfg.get("command"),
                "args": list(cfg.get("args") or []),
                "env": mask_kv(cfg.get("env")),
                "cwd": cfg.get("cwd"),
                "url": cfg.get("url"),
                "headers": mask_kv(cfg.get("headers")),
                "source": meta.get("source") or "local",
                "market_id": meta.get("market_id"),
                "market_name": meta.get("market_name"),
                "publisher": meta.get("publisher"),
                "installed_at": meta.get("installed_at"),
                # 本地包来源标记（docs/frontend/23 §本地安装）：条目由
                # `mcp_installer` 装出来的包提供时，这里带 {slug,name,version,
                # command,integrity,registry}，前端据此显示"本地包 vX.Y.Z"徽标、
                # 并让「重新校验 / 卸载」按钮找到目标目录。**不是**条目字段 ——
                # 条目本身只有 `command`，标准 mcpServers 格式一字未变。
                "pkg": meta.get("pkg") if isinstance(meta.get("pkg"), dict) else None,
            })
        return out, []

    # ── 校验（写之前先过这一关）───────────────────────────────────

    def validate(self, name: str, config: dict) -> list[str]:
        """名称 + 配置全量校验；返回错误文案列表（空 = 通过）。只校验，不落盘。"""
        return self.validate_name(name) + self.validate_config(config)

    def validate_name(self, name: str) -> list[str]:
        """名称校验（`mcp_server_test` 没有名称，故与配置校验拆开）。"""
        name = name or ""
        if not name.strip():
            return ["名称不能为空"]
        if name != name.strip():
            return ["名称首尾不能有空白字符"]
        if len(name) > NAME_MAX_LEN:
            return [f"名称过长（上限 {NAME_MAX_LEN} 字符）"]
        if not re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_"):
            return ["名称至少要含一个字母或数字（工具名前缀不能为空）"]
        return []

    def validate_config(self, config: dict) -> list[str]:
        """配置体校验；返回错误文案列表（空 = 通过）。"""
        errors: list[str] = []
        if not isinstance(config, dict):
            return ["配置必须是 JSON 对象"]

        transport = transport_of(config)
        if transport not in VALID_TRANSPORTS:
            errors.append(
                f"传输类型 {transport!r} 不支持（可选：{' / '.join(VALID_TRANSPORTS)}）")

        if transport == "stdio":
            command = config.get("command")
            if not isinstance(command, str) or not command.strip():
                errors.append("stdio 传输必须提供可执行的 command")
            args = config.get("args", [])
            if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
                errors.append("args 必须是字符串数组")
            cwd = config.get("cwd")
            if cwd is not None and not isinstance(cwd, str):
                errors.append("cwd 必须是字符串")
        else:
            url = config.get("url")
            if not isinstance(url, str) or not url.strip():
                errors.append(f"{transport} 传输必须提供 url")
            elif not re.match(r"^https?://", url.strip()):
                errors.append("url 必须以 http:// 或 https:// 开头")

        errors.extend(_validate_kv("env", config.get("env")))
        errors.extend(_validate_kv("headers", config.get("headers")))
        return errors

    def normalize_collisions(self, servers: list[dict]) -> list[str]:
        """检测「工具名前缀撞车」：`normalize_mcp_name` 会把非 `[a-zA-Z0-9_]` 折成 `_`，
        于是 `my-server` 与 `my_server` 会生成同一个前缀 `mcp__my_server__`，
        工具名互相覆盖（handlers 是 dict，后来的静默赢）。**给警告，不拦保存。**
        """
        buckets: dict[str, list[str]] = {}
        for s in servers:
            buckets.setdefault(_prefix_key(s.get("name") or ""), []).append(s.get("name") or "")
        return [
            "名称 " + " 与 ".join(names) + f" 会归一化成同一个工具前缀（mcp__{key}__），"
            "模型侧只有一份工具可见，建议改名"
            for key, names in buckets.items() if len(names) > 1
        ]

    # ── 写 ────────────────────────────────────────────────────────

    def upsert(self, name: str, config: dict, original_name: str | None = None,
               meta: dict | None = None) -> tuple[list[dict], list[str]]:
        """新增或更新一条（`original_name` 与 `name` 不同 = 重命名，改 key）。

        返回 `(最新 servers 列表, warnings)`。校验不过抛 `ValueError`；
        落盘失败抛 `OSError`。`meta` 是市场来源信息，写入旁路文件。
        """
        name = (name or "").strip()
        errors = self.validate(name, config)
        if errors:
            raise ValueError("；".join(errors))

        with self._lock:
            servers = self._read_for_write()
            old_key = (original_name or name).strip()
            renamed = old_key != name and old_key in servers

            # 密钥掩码回传后用户没动过 → 保留磁盘上的原值（不能把 •••••• 写进去）
            entry = _clean_entry(config)
            for field in ("env", "headers"):
                if field in entry:
                    entry[field] = self._merge_secret(field, old_key if renamed else name,
                                                      entry[field])
            if renamed:
                servers.pop(old_key, None)
            servers[name] = entry
            self._write_json(self.path, {"mcpServers": servers})

            sources = self.load_sources()
            touched = False
            if renamed and old_key in sources:
                sources[name] = sources.pop(old_key)
                touched = True
            if meta:
                cur = sources.get(name)
                cur = dict(cur) if isinstance(cur, dict) else {}
                cur.update(meta)
                sources[name] = cur
                touched = True
            if touched:
                self._write_json(self.sources_path, sources)

        servers_list, _ = self.list_servers()
        return servers_list, self.normalize_collisions(servers_list)

    def remove(self, name: str) -> tuple[list[dict], list[str]]:
        """删一条（含旁路元数据）。条目不存在时幂等返回（不报错）。"""
        name = (name or "").strip()
        with self._lock:
            servers = self._read_for_write()
            if name in servers:
                servers.pop(name, None)
                self._write_json(self.path, {"mcpServers": servers})
                sources = self.load_sources()
                if sources.pop(name, None) is not None:
                    self._write_json(self.sources_path, sources)
        return self.list_servers()

    # ── 内部 ──────────────────────────────────────────────────────

    def _read_for_write(self) -> dict:
        """落盘路径专用读：坏文件必须抛，不能静默当成空。"""
        return dict(self.load_raw())

    def _merge_secret(self, field: str, name: str, incoming) -> dict:
        """掩码回填：回执里脱敏过的密钥，用户在编辑框里没动 → 保留磁盘原值。

        三种情形（容易搞反，改之前先看这里）：
          - `incoming[key] == 掩码` → 用户没改 → **回填磁盘原值**（关键：不能把
            `••••••` 真写进文件，那等于把密钥替换成了星号）
          - `incoming[key]` 是别的值 → 用户改了/新填了 → 用新值
          - 磁盘有、`incoming` 里完全没有 → 用户在表单里删掉了这一项 → 不保留
        """
        incoming = incoming if isinstance(incoming, dict) else {}
        existing: dict = {}
        try:
            raw = self.load_raw()
        except (ValueError, OSError):
            raw = {}
        cfg = raw.get(name)
        if isinstance(cfg, dict) and isinstance(cfg.get(field), dict):
            existing = cfg[field]
        out: dict = {}
        for key, value in incoming.items():
            if value == SECRET_MASK and key in existing:
                out[key] = existing[key]
            elif value != SECRET_MASK:
                out[key] = value
        return out

    def _write_json(self, path: Path, data) -> None:
        """原子写（tmp + os.replace）+ 权限 0600（可能含明文密钥）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.error("%s 写入失败: %s", path.name, e)
            raise


def market_meta(market_id: str, market_name: str = "", publisher: str = "",
                source: str = "market") -> dict:
    """构造一条旁路元数据（市场安装用）。"""
    return {
        "source": source,
        "market_id": market_id,
        "market_name": market_name,
        "publisher": publisher,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def local_pkg_meta(slug: str, name: str, version: str, command: str = "",
                   integrity: str = "", registry: str = "") -> dict:
    """构造"本地包"来源的旁路元数据（`mcp_installer` 装出来的条目用）。

    ⚠️ 只有 `source`/`pkg` 两个语义位，**不要**把 `pkg` 内嵌进 `mcp_servers.json`
    的条目里 —— 那会破坏"配置文件保持标准 mcpServers 格式"这条承诺，并且
    `maybe_reload` 会误判成配置变化而整条断连重连（见 paths.py 的 MCP_SOURCES 注释）。
    """
    return {
        "source": "local-pkg",
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pkg": {
            "slug": slug,
            "name": name,
            "version": version,
            "command": command,
            "integrity": integrity,
            "registry": registry,
        },
    }


# ── 模块级小工具 ──────────────────────────────────────────────────

def _validate_kv(field: str, value) -> list[str]:
    if value is None or value == {}:
        return []
    if not isinstance(value, dict):
        return [f"{field} 必须是键值对对象"]
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        return [f"{field} 的键与值都必须是字符串"]
    return []


def _clean_entry(config: dict) -> dict:
    """只保留白名单键；`enable` 落成 1 / 0 数字（与存量文件一致）。"""
    out: dict = {}
    for key in _ENTRY_KEYS:
        if key not in config or key == "enable":
            continue
        out[key] = config[key]
    out["enable"] = 1 if is_enabled(config.get("enable", 1)) else 0
    return out


def _prefix_key(name: str) -> str:
    """工具名前缀里用的归一化名（与 `mcp_manager.normalize_mcp_name` 同规则）。

    这里**刻意不 import** mcp_manager —— 那会把官方 `mcp` SDK 拉进设置页的导入链
    （设置页可能在 Agent 构造之前就被打开，见 ws_bridge._permission_store 的同类注释）。
    规则只有一行正则，重复比引入依赖更便宜；有测试锁住两处一致。
    """
    return re.sub(r"[^a-zA-Z0-9_]", "_", name or "")
