#!/usr/bin/env python3
"""
plugin_store.py - 插件 store（设置页「插件」面板的读写门面 + 组件清单）

采用 **Claude Code 插件规范**（docs/frontend/25）—— 好处是"兼容即市场"：
`.claude-plugin/marketplace.json`（市场目录）与 `.claude-plugin/plugin.json`
（插件清单）都是既定事实标准，官方市场 300+ 插件可以零改造直接装上。

数据布局：

    ~/.aigent/plugins/<name>/.claude-plugin/plugin.json   插件清单（唯一必需文件）
    ~/.aigent/plugins/<name>/skills/<skill>/SKILL.md      贡献的技能
    ~/.aigent/plugins/<name>/commands/*.md                贡献的斜杠命令
    ~/.aigent/plugins/<name>/agents/*.md                  贡献的子智能体
    ~/.aigent/plugins/<name>/hooks/hooks.json             钩子
    ~/.aigent/plugins/<name>/.mcp.json                    贡献的 MCP 服务器
    ~/.aigent/plugins_sources.json                        **旁路元数据**（启停 + 来源）

⚠️ **本期运行时只接「技能」这一路贡献**（`skills.SkillLoader` 会扫描**已启用**
插件的 `skills/*/SKILL.md`，并以 `<插件名>:<技能名>` 命名空间化，与 Claude Code
的命名规则一致）。commands / agents / hooks / MCP 服务器**只做清单展示**，
UI 与文档都必须如实标注"本期未接入运行时"，不能让人以为装上就能用。

⚠️ 因此：安装确认页要展示的"将要生效的东西"里，技能是真生效的、其余不是 ——
这个区分必须写在界面上（docs/frontend/25 §2.4）。
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from logger import get_logger
from paths import PLUGIN_SOURCES, PLUGINS_DIR
from store_io import is_enabled, read_json_lenient, write_json_atomic
from skill_store import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, safe_relpath

log = get_logger("plugin_store")

MANIFEST_DIR = ".claude-plugin"
MANIFEST_NAME = "plugin.json"
MCP_FILE = ".mcp.json"
HOOKS_REL = "hooks/hooks.json"

# 插件名即目录名 → 与技能同一条白名单（多挡 `/` 与 `..`）
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 组件类型（供 UI 统一渲染）
COMPONENT_KEYS = ("skills", "commands", "agents", "hooks", "mcp_servers", "lsp_servers")

COMPONENT_LABEL = {
    "skills": "技能",
    "commands": "命令",
    "agents": "子智能体",
    "hooks": "钩子",
    "mcp_servers": "MCP 服务器",
    "lsp_servers": "语言服务器",
}

# **本期真正接入运行时的组件类型**（其余只展示，见模块 docstring）
WIRED_COMPONENTS = ("skills",)


def validate_name(name: str) -> list[str]:
    name = name or ""
    if not name.strip():
        return ["名称不能为空"]
    if name != name.strip():
        return ["名称首尾不能有空白字符"]
    if not NAME_RE.match(name):
        return ["名称只能用字母、数字与 . _ -，且必须以字母或数字开头（上限 64 字符）"]
    if name.lower().endswith("."):
        return ["名称不能以 . 结尾（Windows 上无法创建该目录）"]
    return []


class PluginStore:
    """`~/.aigent/plugins/` + `plugins_sources.json` 的门面。"""

    def __init__(self, plugins_dir: Path | str | None = None,
                 sources_path: Path | str | None = None):
        self.plugins_dir = Path(plugins_dir) if plugins_dir else PLUGINS_DIR
        # 同 `SkillStore`：元数据文件名从常量取、目录随 `plugins_dir` 走，
        # 保证"自定义插件目录"也拿到自洽的 store。默认值下与 `PLUGIN_SOURCES` 等价。
        self.sources_path = (Path(sources_path) if sources_path
                             else self.plugins_dir.parent / PLUGIN_SOURCES.name)

    # ── 读 ────────────────────────────────────────────────────────

    def load_sources(self) -> dict:
        raw = read_json_lenient(self.sources_path, {}, label="plugins_sources.json")
        return raw if isinstance(raw, dict) else {}

    def scan(self) -> list[dict]:
        """扫描插件目录 → 全部插件（**含被禁用的**），按名排序。"""
        sources = self.load_sources()
        out: list[dict] = []
        if not self.plugins_dir.is_dir():
            return out
        try:
            entries = sorted(self.plugins_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            log.error("插件目录枚举失败：%s", exc)
            return out
        for d in entries:
            if d.is_symlink() or not d.is_dir():
                continue
            item = self._describe(d, sources.get(d.name))
            if item is not None:
                out.append(item)
        return out

    def get(self, name: str) -> dict | None:
        for item in self.scan():
            if item["name"] == name:
                return item
        return None

    def names(self) -> list[str]:
        return [item["name"] for item in self.scan()]

    def iter_skill_manifests(self) -> list[tuple[str, Path]]:
        """**已启用**插件贡献的技能 → `[( '<插件>:<技能>', SKILL.md 路径)]`。

        命名空间用 `:`（与 Claude Code 的 `plugin:skill` 规则一致）：插件之间的技能
        重名是常态（各家都喜欢叫 `code-review`），不加前缀必然互相覆盖。
        前缀里的 `:` 不可能出现在技能名里（`NAME_RE` 不含 `:`），所以不会歧义。
        """
        out: list[tuple[str, Path]] = []
        for plugin in self.scan():
            if not plugin["enabled"] or not plugin["has_manifest"]:
                continue
            root = Path(plugin["path"]) / "skills"
            for skill in plugin["components"].get("skills", []):
                manifest = root / skill / "SKILL.md"
                if manifest.is_file():
                    out.append((f"{plugin['name']}:{skill}", manifest))
        return out

    # ── 写 ────────────────────────────────────────────────────────

    def set_enabled(self, name: str, enabled: bool) -> list[dict]:
        """启用 / 禁用。只写旁路元数据 —— 插件目录本体保持原样。"""
        name = (name or "").strip()
        with _dir_lock(self.plugins_dir):
            if name not in set(self._dir_names()):
                raise ValueError(f"没有名为「{name}」的插件")
            sources = self.load_sources()
            cur = sources.get(name)
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur["enabled"] = 1 if enabled else 0
            cur.setdefault("source", "local")
            sources[name] = cur
            write_json_atomic(self.sources_path, sources)
        return self.scan()

    def remove(self, name: str) -> tuple[list[dict], list[str]]:
        """删除插件（目录 + 元数据）。不存在时幂等返回。

        符号链接只 `unlink` 不跟随（同 skill_store：跟随删会端掉链接指向的真实目录）。
        """
        name = (name or "").strip()
        warnings: list[str] = []
        if not name:
            return self.scan(), ["名称不能为空"]
        target = self.plugins_dir / name
        with _dir_lock(self.plugins_dir):
            if target.is_symlink():
                try:
                    target.unlink()
                except OSError as exc:
                    raise OSError(f"删除链接失败：{exc}") from exc
                warnings.append(f"「{name}」是符号链接，只删除了链接本身")
            elif target.exists():
                root = self.plugins_dir.resolve()
                real = target.resolve()
                if real != root and root not in real.parents:
                    raise ValueError(f"「{name}」的落点超出了插件目录，已拒绝删除")
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    raise OSError(f"删除失败：{exc}") from exc
            sources = self.load_sources()
            if sources.pop(name, None) is not None:
                write_json_atomic(self.sources_path, sources)
        return self.scan(), warnings

    def install(self, name: str, files: dict, meta: dict | None = None) -> list[dict]:
        """安装插件：把 `files` 写到 `<plugins>/<name>/`。

        先全量校验、后落盘；失败回滚（半装的插件比装不上更难排查）。
        同名目录已存在 → `ValueError`。
        """
        name = (name or "").strip()
        errors = validate_name(name) + validate_files(files)
        if errors:
            raise ValueError("；".join(errors))

        target = self.plugins_dir / name
        with _dir_lock(self.plugins_dir):
            if target.exists() or target.is_symlink():
                raise ValueError(f"已存在同名插件「{name}」，请改名后再装")
            written: list[Path] = []
            try:
                target.mkdir(parents=True, exist_ok=False)
                for rel, content in files.items():
                    safe = safe_relpath(rel)
                    if safe is None:
                        raise ValueError(f"非法文件路径：{rel}")
                    dest = target / safe
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, bytes):
                        dest.write_bytes(content)
                    else:
                        dest.write_text(str(content), encoding="utf-8")
                    written.append(dest)
            except (OSError, ValueError) as exc:
                shutil.rmtree(target, ignore_errors=True)
                if isinstance(exc, ValueError):
                    raise
                raise OSError(f"写入插件文件失败：{exc}") from exc

            record = dict(meta or {})
            record.setdefault("source", "market")
            record.setdefault("enabled", 1)
            record["installed_at"] = record.get("installed_at") or datetime.now(
                timezone.utc).isoformat(timespec="seconds")
            record["file_count"] = len(written)
            sources = self.load_sources()
            sources[name] = record
            write_json_atomic(self.sources_path, sources)
        return self.scan()

    def update_meta(self, name: str, meta: dict) -> None:
        name = (name or "").strip()
        if not name:
            return
        with _dir_lock(self.plugins_dir):
            sources = self.load_sources()
            cur = sources.get(name)
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur.update(meta or {})
            sources[name] = cur
            write_json_atomic(self.sources_path, sources)

    # ── 内部 ──────────────────────────────────────────────────────

    def _dir_names(self) -> list[str]:
        if not self.plugins_dir.is_dir():
            return []
        try:
            return [p.name for p in self.plugins_dir.iterdir()
                    if p.is_dir() and not p.is_symlink()]
        except OSError:
            return []

    def _describe(self, d: Path, meta) -> dict | None:
        """插件目录 → UI 条目。逐字段兜底：清单坏了也要能看见这个插件（并给出原因）。"""
        name = d.name
        meta = meta if isinstance(meta, dict) else {}
        warnings: list[str] = []
        manifest_path = d / MANIFEST_DIR / MANIFEST_NAME

        manifest: dict = {}
        has_manifest = manifest_path.is_file()
        if has_manifest:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    manifest, has_manifest = {}, False
                    warnings.append("plugin.json 顶层不是 JSON 对象")
            except (OSError, ValueError) as exc:
                manifest, has_manifest = {}, False
                warnings.append(f"plugin.json 解析失败：{exc}")
        else:
            warnings.append(f"缺少 {MANIFEST_DIR}/{MANIFEST_NAME}，不是有效的 Claude Code 插件")

        components = _inventory(d, manifest)
        if not has_manifest:
            for key in components:
                components[key] = []

        return {
            "name": name,
            "display_name": str(manifest.get("displayName") or manifest.get("name") or name),
            "description": str(manifest.get("description") or "").strip(),
            "version": str(manifest.get("version") or "").strip(),
            "author": _author_of(manifest),
            "homepage": str(manifest.get("homepage") or "").strip(),
            "has_manifest": has_manifest,
            "enabled": is_enabled(meta.get("enabled", 1)),
            "path": str(d),
            "manifest_path": str(manifest_path),
            "components": components,
            "component_counts": {k: len(v) for k, v in components.items()},
            "wired": list(WIRED_COMPONENTS),
            "source": str(meta.get("source") or "local"),
            "market_id": meta.get("market_id"),
            "market_name": meta.get("market_name"),
            "market_url": meta.get("market_url"),
            "publisher": meta.get("publisher"),
            "repo": meta.get("repo"),
            "installed_at": meta.get("installed_at"),
            "warnings": warnings,
        }


# ═══════════════════════════════════════════════════════════════════════
#  组件清单
# ═══════════════════════════════════════════════════════════════════════

def _inventory(d: Path, manifest: dict) -> dict:
    """扫描插件目录 → 六类组件清单（磁盘发现 + 清单声明取并集）。

    ⚠️ 磁盘发现这一段**刻意与 `inventory_of_files` 同规则**（那份作用于"相对路径 →
    内容"的映射，市场侧远程抓取只有文件清单、没有真实目录）。两处必须同步改，
    否则会出现「安装确认页说没有技能、装完却多出一个技能」这种自相矛盾。
    这里把磁盘读成同一张映射再转调，就是为了**只有一份规则**。
    """
    files: dict[str, str] = {}
    for root, dirs, names in os.walk(d, followlinks=False):
        # **目录要排序**：os.walk 的顺序是文件系统给的，不稳定 → 组件清单顺序会飘，
        # 界面上"技能列表怎么换了个顺序"会让人以为配置变了。
        dirs[:] = [x for x in sorted(dirs) if not (Path(root) / x).is_symlink()]
        for fn in names:
            p = Path(root) / fn
            if p.is_symlink():
                continue
            try:
                rel = p.relative_to(d).as_posix()
            except ValueError:
                continue
            # 内容不重要 —— inventory_of_files 只看路径与两个 JSON 的键。
            # 有体积上限的文件才读，避免把大二进制资产读进内存。
            text = ""
            if rel in (HOOKS_REL, "hooks.json", MCP_FILE,
                       f"{MANIFEST_DIR}/{MANIFEST_NAME}"):
                try:
                    if p.stat().st_size < 512 * 1024:
                        text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
            files[rel] = text
    return inventory_of_files(files, manifest)


def inventory_of_files(files: dict, manifest: dict) -> dict:
    """按**相对路径**推导组件清单（本地目录与远程抓取**共用同一份规则**）。

    ⚠️ 为什么取「磁盘发现 ∪ 清单声明」而不是只信清单：实测插件清单经常只写一半
    （有的只声明 `skills` 不写 `commands`，有的这几个字段根本不写）。只信清单会让
    安装确认页显示的"将要生效的东西"比实际少 —— 那恰恰是安全闸门最不能出错的地方。
    """
    out: dict[str, list[str]] = {k: [] for k in COMPONENT_KEYS}

    def _add(kind: str, value) -> None:
        if value is None:
            return
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if isinstance(v, dict):
                v = v.get("name") or v.get("Source") or ""
            text = str(v or "").strip()
            if text and text not in out[kind]:
                out[kind].append(text)

    paths = [p for p in files if isinstance(p, str)]

    for rel in paths:
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            _add("skills", parts[1])
        elif len(parts) == 2 and parts[0] in ("commands", "agents") \
                and parts[1].endswith(".md"):
            _add(parts[0], parts[1][:-3])
        elif rel == "SKILL.md":  # 插件根目录直接就是技能（少见但合法）
            _add("skills", ".")

    hooks_text = files.get(HOOKS_REL)
    if hooks_text is None:
        hooks_text = files.get("hooks.json")
    hooks = _parse_json_text(hooks_text)
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            n = len(entries) if isinstance(entries, list) else 1
            _add("hooks", f"{event}（{n} 条）")
    elif isinstance(hooks, list):
        _add("hooks", f"{len(hooks)} 条")

    mcp = _parse_json_text(files.get(MCP_FILE))
    if isinstance(mcp, dict) and isinstance(mcp.get("mcpServers"), dict):
        for sname in mcp["mcpServers"]:
            _add("mcp_servers", sname)

    # 清单声明（并集）
    _add("skills", manifest.get("skills"))
    _add("commands", manifest.get("commands"))
    _add("agents", manifest.get("agents"))
    if manifest.get("hooks"):
        _add("hooks", "（清单声明）")
    for key, kind in (("mcpServers", "mcp_servers"), ("lspServers", "lsp_servers")):
        declared = manifest.get(key)
        if isinstance(declared, dict):
            for sname in declared:
                _add(kind, sname)
        else:
            _add(kind, declared)

    return out


def _parse_json_text(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def list_files(d: Path | str) -> list[dict]:
    """列插件目录里的全部文件（相对路径 + 字节数），供「查看文件」展示。

    与 `_inventory` 同一条走法（不跟随符号链接、不跟着链接进目录）。
    """
    root = Path(d)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for cur, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [x for x in sorted(dirs) if not (Path(cur) / x).is_symlink()]
        for fn in sorted(names):
            p = Path(cur) / fn
            if p.is_symlink():
                continue
            try:
                rel = p.relative_to(root).as_posix()
                size = p.stat().st_size
            except (OSError, ValueError):
                continue
            out.append({"path": rel, "size": size})
    return out


def _author_of(manifest: dict) -> str:
    author = manifest.get("author")
    if isinstance(author, str):
        return author.strip()
    if isinstance(author, dict):
        return str(author.get("name") or "").strip()
    return ""


def validate_files(files) -> list[str]:
    """插件安装体校验：类型 / 路径 / **必须有清单** / 体积。"""
    errors: list[str] = []
    if not isinstance(files, dict) or not files:
        return ["安装内容为空"]
    if len(files) > MAX_FILES:
        errors.append(f"文件数超过上限（{len(files)} > {MAX_FILES}）")
    total = 0
    has_manifest = False
    for rel, content in files.items():
        safe = safe_relpath(rel)
        if safe is None:
            errors.append(f"非法文件路径：{rel}")
            continue
        if safe == f"{MANIFEST_DIR}/{MANIFEST_NAME}":
            has_manifest = True
        if isinstance(content, bytes):
            size = len(content)
        elif isinstance(content, str):
            size = len(content.encode("utf-8"))
        else:
            errors.append(f"文件内容必须是文本或字节：{safe}")
            continue
        if size > MAX_FILE_BYTES:
            errors.append(f"单个文件超过上限（{safe} > {MAX_FILE_BYTES // 1024} KiB）")
        total += size
    if not has_manifest:
        errors.append(f"插件必须包含 {MANIFEST_DIR}/{MANIFEST_NAME}")
    if total > MAX_TOTAL_BYTES:
        errors.append(f"总体积超过上限（{total // 1024} KiB > {MAX_TOTAL_BYTES // 1024} KiB）")
    return errors


def unique_name(base: str, taken: list[str]) -> str:
    """撞名 → `-2`、`-3`…（与 `skill_store.unique_name` 同策略）。"""
    base = re.sub(r"[^A-Za-z0-9._-]", "-", (base or "").strip()).strip(".-") or "plugin"
    if not NAME_RE.match(base):
        base = ("p-" + base)[:64]
    taken_set = {t for t in taken if isinstance(t, str)}
    if base not in taken_set:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in taken_set and NAME_RE.match(cand):
            return cand
    return base


def market_meta(market_id: str, market_name: str = "", publisher: str = "",
                market_url: str = "", repo: str = "", source: str = "market") -> dict:
    """构造一条插件市场来源元数据。"""
    return {
        "source": source,
        "market_id": market_id,
        "market_name": market_name,
        "publisher": publisher,
        "market_url": market_url,
        "repo": repo,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _dir_lock(path: Path):
    from store_io import _lock_for  # noqa: PLC0415

    return _lock_for(Path(path))
