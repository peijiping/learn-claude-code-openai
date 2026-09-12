#!/usr/bin/env python3
"""
mcp_manager.py - MCPManager（真实 MCP 客户端接入）

基于官方 `mcp` Python SDK（mcp>=1.0,<2）的真实客户端实现，替代原 mock 版。

⚠️ 命名说明：本地模块命名为 `mcp_manager`（而非 `mcp`），避免与官方 `mcp` SDK
包名冲突（否则 `from mcp import ClientSession` 会解析到本地模块自身）。

核心设计：
- MCPServerSession：单个 MCP 服务器的长连接会话。每个服务器一个后台 daemon 线程
  跑独立 asyncio 事件循环 + 常驻 ClientSession；同步侧用 run_coroutine_threadsafe 桥接。
- 传输：stdio（本地进程）/ streamable-http / sse（远程）；远程支持 headers 鉴权
  （streamable-http 经 httpx.AsyncClient(headers=...) 传入，sse 直接 headers=）。
- 配置：WorkSpace/HomeDir/mcp/mcp_servers.json（mcpServers 主流格式，多服务器），
  支持 ${VAR} 环境变量插值（密钥不落盘）。
- 热加载：assemble_tools() 每轮检测配置文件 mtime，增删改自动 reconnect；死会话清理。
- Resources：每服务器合成 list_resources / read_resource 两个只读工具暴露给模型。
- 工具标注：消费 readOnlyHint / destructiveHint / openWorldHint，description 追加
  [readOnly]/[destructive]/[openWorld]；破坏性工具由 HookSystem.permission_hook 门控。
- 断线重连：call_tool 捕获传输级异常 → restart() 一次并重试一次（调用时自愈）。

对外 API（与旧 mock 版保持兼容，tools.py / agent_full_v2.py 依赖）：
  available_servers / connected_names / catalog / catalog_text / connect /
  connect_all / assemble_tools / assemble_handlers
新增：disconnect / maybe_reload / shutdown / is_destructive

MCP 是 Lead（主智能体）级动态能力：子智能体不暴露 connect_mcp 与 mcp__* 工具。
"""

import asyncio
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

from paths import MCP_CONFIG, ROOT_DIR


# ── 名称规范化 ───────────────────────────────────────────────────────
# 统一转下划线：部分 provider（如 DeepSeek）对 function 名只允许 [a-zA-Z0-9_]，
# 连字符（server 名 / 工具名里常见）一律折叠为下划线，规避 provider 侧校验拒绝。
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_]")


def normalize_mcp_name(name: str) -> str:
    """把名称中所有非 [a-zA-Z0-9_] 字符（含连字符）替换为下划线，保证工具名合法唯一。"""
    return _DISALLOWED_CHARS.sub("_", name)


# ── 配置读取与 ${VAR} 插值 ───────────────────────────────────────────
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def interpolate(text: str) -> str:
    """把 ${VAR} 展开为环境变量值；缺失变量保留原样并打警告（密钥不落盘）。"""
    def _sub(m):
        key = m.group(1)
        val = os.environ.get(key)
        if val is None:
            print(f"  \033[31m[mcp] warning: env var {key} not set (kept literal)\033[0m")
            return m.group(0)
        return val
    return _ENV_VAR_PATTERN.sub(_sub, text)


def _interpolate_value(v):
    """递归插值配置结构（字符串展开，dict/list 递归，其余原样）。"""
    if isinstance(v, str):
        return interpolate(v)
    if isinstance(v, dict):
        return {k: _interpolate_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_interpolate_value(x) for x in v]
    return v


def load_config(path: Path) -> dict:
    """读取 mcpServers 配置为 {name: config}；文件缺失/损坏返回 {}（不崩）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {name: _interpolate_value(cfg)
            for name, cfg in servers.items() if isinstance(cfg, dict)}


# ── 调用结果格式化 ───────────────────────────────────────────────────
def format_call_result(result) -> str:
    """把 CallToolResult 格式化为文本（isError 前缀 + content 拼接 + structuredContent 兜底）。"""
    prefix = "MCP error: " if getattr(result, "isError", False) else ""
    parts = []
    for content in getattr(result, "content", []):
        ctype = getattr(content, "type", "")
        if ctype == "text":
            parts.append(content.text)
        elif ctype == "image":
            parts.append(f"[image {content.mimeType} — cannot inline]")
        else:
            parts.append(f"[{ctype} content]")
    text = "\n".join(parts)
    if not text and getattr(result, "structuredContent", None) is not None:
        try:
            text = json.dumps(result.structuredContent, ensure_ascii=False)
        except Exception:
            text = str(result.structuredContent)
    if not text:
        text = "(no content)"
    return prefix + text


# ═══════════════════════════════════════════════════════════════════════
#  MCPServerSession：单个 MCP 服务器的长连接会话
# ═══════════════════════════════════════════════════════════════════════

class MCPServerSession:
    """单个 MCP 服务器的长连接会话（后台线程 + asyncio loop + 常驻 ClientSession）。

    start() 起后台线程跑 asyncio 事件循环：建连 → initialize → list_tools →
    list_resources，随后常驻保活（await asyncio.Event().wait()）。stop() 置位退出事件
    让协程自然 unwind（ClientSession / transport 的 async with 退出即清理）。
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config                       # 已插值后的配置
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._exit: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_result: str | None = None
        self._tools: list[dict] = []               # MCP Tool → 规范 dict（含标注）
        self._resources: list[dict] = []           # list_resources 结果缓存

    # ── 生命周期 ────────────────────────────────────────────────────

    def start(self) -> str:
        """启动后台线程并等待连接就绪，返回「发现 N 工具 / M 资源」摘要或错误串。"""
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"mcp-{self.name}", daemon=True)
        self._thread.start()
        timeout = float(os.environ.get("MCP_CONNECT_TIMEOUT", "15"))
        if not self._ready.wait(timeout=timeout):
            return f"MCP server '{self.name}' connect timeout"
        return self._start_result or f"MCP server '{self.name}' ready"

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # 线程内未捕获异常 → 记错，不静默崩线程
            self._mark_start_error(f"MCP server '{self.name}' crashed: {e}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    def _mark_start_error(self, msg: str) -> None:
        if self._start_result is None:
            self._start_result = msg
        self._ready.set()

    async def _serve(self) -> None:
        """建连 + initialize + 发现工具/资源，随后常驻保活。"""
        self._exit = asyncio.Event()
        try:
            async with self._connect() as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    tools = await session.list_tools()
                    self._tools = [self._tool_to_dict(t) for t in tools.tools]
                    try:
                        res = await session.list_resources()
                        self._resources = [self._resource_to_dict(r) for r in res.resources]
                    except Exception:
                        self._resources = []
                    summary = (f"Connected to MCP server '{self.name}'. "
                               f"Discovered {len(self._tools)} tools")
                    if self._resources:
                        summary += f", {len(self._resources)} resources"
                    self._start_result = summary
                    self._ready.set()
                    await self._exit.wait()        # 常驻保活，直到 stop()
        except Exception as e:
            self._mark_start_error(f"MCP server '{self.name}' connect failed: {e}")

    @asynccontextmanager
    async def _connect(self):
        """按配置类型建立传输连接，产出 (read, write) 流。"""
        cfg = self.config
        transport = cfg.get("type", "stdio" if "command" in cfg else "streamable-http")
        if transport == "stdio" or "command" in cfg:
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env") or None,
                cwd=cfg.get("cwd") or str(ROOT_DIR),
            )
            async with stdio_client(params) as (read, write):
                yield read, write
        elif transport == "sse":
            async with sse_client(cfg["url"], headers=cfg.get("headers")) as (read, write, _sid):
                yield read, write
        else:  # streamable-http（远程默认）：headers 经 httpx.AsyncClient 传入
            headers = cfg.get("headers")
            http_client = httpx.AsyncClient(headers=headers) if headers else None
            try:
                async with streamable_http_client(
                    cfg["url"], http_client=http_client,
                ) as (read, write, _sid):
                    yield read, write
            finally:
                if http_client is not None:        # SDK 只管自己创建的 client
                    await http_client.aclose()

    def stop(self) -> None:
        """置位退出事件（让 _serve 自然 unwind 清理），join 线程，重置状态。"""
        if self._loop is not None and self._exit is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._exit.set(), self._loop).result(timeout=5)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._session = None
        self._thread = None
        self._loop = None
        self._tools = []
        self._resources = []

    def restart(self) -> None:
        """stop + start（传输死掉后自愈）。"""
        self.stop()
        self.start()

    # ── 状态查询 ────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """是否已成功初始化（可调用工具/资源）。"""
        return self._session is not None

    def is_alive(self) -> bool:
        """后台线程是否存活。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def tools(self) -> list[dict]:
        return self._tools

    @property
    def tool_names(self) -> list[str]:
        return [t["name"] for t in self._tools]

    @property
    def resources(self) -> list[dict]:
        return self._resources

    def is_destructive(self, tool_name: str) -> bool:
        """工具是否带 destructiveHint 标注（功能③）。"""
        for t in self._tools:
            if t["name"] == tool_name:
                return bool(t.get("destructive"))
        return False

    # ── 同步侧桥接 ──────────────────────────────────────────────────

    def _async_result(self, coro):
        """把协程提交到会话事件循环并同步等待（MCP_CALL_TIMEOUT 秒）。"""
        if self._loop is None:
            raise RuntimeError("event loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        timeout = float(os.environ.get("MCP_CALL_TIMEOUT", "60"))
        return fut.result(timeout=timeout)

    def call_tool(self, tool_name: str, args: dict) -> str:
        """同步调用服务器工具；传输级异常时 restart() 一次并重试（断线自愈）。"""
        try:
            return self._async_result(self._async_call_tool(tool_name, args))
        except Exception as e:
            try:
                self.restart()
                return self._async_result(self._async_call_tool(tool_name, args))
            except Exception as e2:
                return f"MCP error: {e2}"

    async def _async_call_tool(self, tool_name: str, args: dict) -> str:
        if self._session is None:
            raise RuntimeError("session not initialized")
        result = await self._session.call_tool(tool_name, arguments=args or {})
        return format_call_result(result)

    def list_resources_text(self) -> str:
        """格式化资源清单（功能②）。"""
        if not self._resources:
            return f"[mcp:{self.name}] (no resources)"
        lines = []
        for r in self._resources:
            desc = r.get("description")
            suffix = f" — {desc}" if desc else ""
            lines.append(f"- {r['uri']}  {r.get('name', '')}{suffix}")
        return "\n".join(lines)

    def read_resource(self, uri: str) -> str:
        """读取资源内容（功能②）；blob 返回字节摘要。"""
        try:
            return self._async_result(self._async_read_resource(uri))
        except Exception as e:
            return f"MCP error: read_resource({uri}) → {e}"

    async def _async_read_resource(self, uri: str) -> str:
        if self._session is None:
            raise RuntimeError("session not initialized")
        result = await self._session.read_resource(uri)
        parts = []
        for content in result.contents:
            text = getattr(content, "text", None)
            if text is not None:
                parts.append(text)
            else:
                blob = getattr(content, "blob", None)
                n = len(blob) if blob else 0
                parts.append(f"[binary {n} bytes] {content.uri}")
        return "\n".join(parts) if parts else "(empty)"

    # ── MCP 对象 → 规范 dict ────────────────────────────────────────

    @staticmethod
    def _tool_to_dict(tool) -> dict:
        d = {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": dict(tool.inputSchema) if tool.inputSchema
                           else {"type": "object", "properties": {}},
        }
        ann = getattr(tool, "annotations", None)
        if ann is not None:
            if getattr(ann, "readOnlyHint", False):
                d["readOnly"] = True
                d["description"] += " [readOnly]"
            if getattr(ann, "destructiveHint", False):
                d["destructive"] = True
                d["description"] += " [destructive]"
            if getattr(ann, "openWorldHint", False):
                d["description"] += " [openWorld]"
        return d

    @staticmethod
    def _resource_to_dict(r) -> dict:
        return {
            "uri": str(r.uri),
            "name": r.name or "",
            "description": r.description or "",
            "mimeType": r.mimeType or "",
        }


# ═══════════════════════════════════════════════════════════════════════
#  MCPManager：管理已连接的 MCP 服务器集合
# ═══════════════════════════════════════════════════════════════════════

class MCPManager:
    """管理已连接的 MCP 服务器集合：配置发现、长连接维护、动态工具池、委托调用。

    职责（纯 MCP，不感知任务/agent）：
    - connect：按配置文件连接一个服务器并发现其工具/资源
    - assemble_tools：把所有已连接服务器工具 + Resources 合成工具转成 OpenAI 格式
    - assemble_handlers：`mcp__{server}__{tool}` → 委托 session 的闭包
    - maybe_reload：每轮检测配置文件 mtime，增删改自动 reconnect + 死会话清理
    - is_destructive：供 HookSystem.permission_hook 门控破坏性 MCP 工具

    工具名统一带命名空间前缀 mcp__{server}__{tool}，避免与内置工具重名。
    """

    def __init__(self, config_file: Path | None = None):
        self._config_file = Path(config_file) if config_file else MCP_CONFIG
        self._config: dict[str, dict] = {}          # 当前生效配置（{name: cfg}）
        self._config_mtime: float | None = None
        self._clients: dict[str, MCPServerSession] = {}  # 已连接：name → 会话
        self._reload_config()

    # ── 配置 ────────────────────────────────────────────────────────

    def _reload_config(self) -> None:
        self._config = load_config(self._config_file)
        try:
            self._config_mtime = self._config_file.stat().st_mtime
        except Exception:
            self._config_mtime = None

    def _load_config(self) -> dict:
        return self._config

    def available_servers(self) -> list[str]:
        """返回配置文件里可连接的服务器名称列表。"""
        return list(self._config.keys())

    def connected_names(self) -> list[str]:
        """返回当前已连接的服务器名称列表。"""
        return list(self._clients.keys())

    # ── 目录 ────────────────────────────────────────────────────────

    def catalog(self) -> dict[str, list[str]]:
        """返回「服务器 → 工具名列表」目录（已连接读真实工具，未连接空列表）。"""
        return {name: (self._clients[name].tool_names
                       if name in self._clients else [])
                for name in self._config.keys()}

    def catalog_text(self) -> str:
        """把可连接目录格式化为人类/LLM 可读文本；未连接标 "(not connected)"。"""
        lines = []
        for name, tool_names in self.catalog().items():
            if tool_names:
                lines.append(f"  - {name}: {', '.join(tool_names)}")
            else:
                lines.append(f"  - {name}: (not connected)")
        return "\n".join(lines)

    # ── 连接与发现 ──────────────────────────────────────────────────

    def connect(self, name: str) -> str:
        """连接一个 MCP 服务器并发现其工具/资源；失败不登记，返回错误串。"""
        if name in self._clients:
            return f"MCP server '{name}' already connected"
        cfg = self._config.get(name)
        if not cfg:
            available = ", ".join(self._config.keys()) or "(none configured)"
            return f"Unknown server '{name}'. Available: {available}"
        session = MCPServerSession(name, cfg)
        result = session.start()
        if session.ready:
            self._clients[name] = session
            print(f"  \033[31m[mcp] connected: {name} → {session.tool_names}\033[0m")
            return result
        print(f"  \033[31m[mcp] connect failed: {name} → {result}\033[0m")
        return f"MCP error: {result}"

    def connect_all(self) -> int:
        """启动自动加载：连接全部已配置服务器，返回成功连接数量。"""
        count = 0
        for name in list(self._config.keys()):
            result = self.connect(name)
            if not result.startswith("MCP error") and not result.startswith("Unknown"):
                count += 1
        return count

    def disconnect(self, name: str) -> str:
        """断开并移除一个服务器会话。"""
        session = self._clients.pop(name, None)
        if session is None:
            return f"MCP server '{name}' not connected"
        session.stop()
        return f"Disconnected MCP server '{name}'"

    def shutdown(self) -> None:
        """全量断开（退出清理，尽力而为）。"""
        for name in list(self._clients.keys()):
            try:
                self._clients[name].stop()
            except Exception:
                pass
        self._clients.clear()

    # ── 热加载（每轮 mtime 检测） ───────────────────────────────────

    def maybe_reload(self) -> str:
        """检测配置文件 mtime 变更并 reconcile（增删改 + 死会话清理）；无变更返回空串。"""
        try:
            new_mtime = self._config_file.stat().st_mtime
        except Exception:
            new_mtime = None
        if new_mtime == self._config_mtime:
            return ""
        old = self._config
        new = load_config(self._config_file)
        self._config = new
        self._config_mtime = new_mtime
        logs: list[str] = []
        # 1) 被删除或配置变化的服务器 → 断开
        for name in list(self._clients.keys()):
            if name not in new or new[name] != old.get(name):
                self.disconnect(name)
                logs.append(f"  [mcp] disconnected: {name}")
        # 2) 新增或配置变化的服务器 → 连接
        for name, cfg in new.items():
            if name not in self._clients and cfg != old.get(name):
                result = self.connect(name)
                logs.append(f"  [mcp] connect {'ok' if not result.startswith('MCP error') else 'failed'}: {name}")
        # 3) 清理死会话（调用时自愈之外，周期兜底）
        for name in list(self._clients.keys()):
            if not self._clients[name].is_alive():
                self.disconnect(name)
                logs.append(f"  [mcp] dropped dead session: {name}")
        for log in logs:
            print(f"\033[31m{log}\033[0m")
        return "\n".join(logs)

    # ── 工具池组装（每轮现场组装，非缓存） ──────────────────────────

    def assemble_tools(self) -> list[dict]:
        """先把热加载 reconcile 落定，再组装 OpenAI 格式工具池。

        真实工具 + 每服务器有资源时追加 list_resources / read_resource 两个
        Resources 合成只读工具（功能②）。
        """
        self.maybe_reload()
        tools: list[dict] = []
        for server_name, session in self._clients.items():
            safe_server = normalize_mcp_name(server_name)
            for t in session.tools:
                prefixed = f"mcp__{safe_server}__{normalize_mcp_name(t['name'])}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": prefixed,
                        "description": t.get("description", ""),
                        "parameters": t.get("inputSchema", {}),
                    },
                })
            if session.resources:
                tools.append(self._resource_list_tool(safe_server))
                tools.append(self._resource_read_tool(safe_server))
        return tools

    def assemble_handlers(self) -> dict[str, callable]:
        """返回 `mcp__{server}__{tool}` → 闭包的映射，委托给对应会话。"""
        handlers: dict[str, callable] = {}
        for server_name, session in self._clients.items():
            safe_server = normalize_mcp_name(server_name)
            for t in session.tools:
                prefixed = f"mcp__{safe_server}__{normalize_mcp_name(t['name'])}"
                handlers[prefixed] = (
                    lambda *, s=session, tn=t["name"], **kw: s.call_tool(tn, kw))
            if session.resources:
                handlers[f"mcp__{safe_server}__list_resources"] = (
                    lambda *, s=session, **kw: s.list_resources_text())
                handlers[f"mcp__{safe_server}__read_resource"] = (
                    lambda *, s=session, **kw: s.read_resource(kw["uri"]))
        return handlers

    # ── Resources 合成工具定义 ──────────────────────────────────────

    @staticmethod
    def _resource_list_tool(safe_server: str) -> dict:
        return {
            "type": "function",
            "function": {
                "name": f"mcp__{safe_server}__list_resources",
                "description": f"List read-only resources exposed by MCP server "
                               f"'{safe_server}'. [readOnly]",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    @staticmethod
    def _resource_read_tool(safe_server: str) -> dict:
        return {
            "type": "function",
            "function": {
                "name": f"mcp__{safe_server}__read_resource",
                "description": f"Read a resource by URI from MCP server "
                               f"'{safe_server}'. [readOnly]",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "description": "Resource URI to read"},
                    },
                    "required": ["uri"],
                },
            },
        }

    # ── 破坏性查询（供 hooks 门控） ─────────────────────────────────

    def is_destructive(self, qualified: str) -> bool:
        """按 `mcp__{server}__{tool}` 全名查询工具是否带 destructiveHint（功能③）。"""
        if not qualified.startswith("mcp__"):
            return False
        parts = qualified.split("__", 2)
        if len(parts) != 3:
            return False
        _, server_key, tool_key = parts
        for name, session in self._clients.items():
            if normalize_mcp_name(name) == server_key:
                return session.is_destructive(tool_key)
        return False
