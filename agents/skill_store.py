#!/usr/bin/env python3
"""
skill_store.py - 技能 store（设置页「技能」面板的读写门面）

数据布局（docs/frontend/24）：

    ~/.aigent/skills/<name>/SKILL.md        技能本体（保持可移植：能直接拷给别的 Agent）
    ~/.aigent/skills/<name>/<附属文件>       脚本 / 参考文档 / 模板 …
    ~/.aigent/skills_sources.json           **旁路元数据**（启停状态 + 市场来源）

三个刻意的设计决定（改之前先读）：

1. **元数据走旁路文件，不写进 SKILL.md。** 启用/禁用若落到 frontmatter 里，每次
   点开关都要**改写用户手写的技能正文** —— 会与上游更新冲突、会把用户的内容弄脏。
   而且技能目录本身要保持原样（`SKILL.md + 附属文件`），这份可移植性是整个
   Agent Skills 生态能互通的前提。

2. **UI 数据源 = 磁盘扫描，不是 `SkillLoader.SKILL_REGISTRY`。**
   后者在 `_scan_skills()` 里**只收启用的**技能，拿它当列表数据源会让被禁用的技能
   永远看不见、也就无法重新启用 —— 与 `mcp_store` 必须先读原始文件的坑同源
   （docs/frontend/23 §1.4）。所以这里自己扫目录，启停状态另行叠加。

3. **删除必须挡住路径逃逸与符号链接。** 名称虽已过 `validate_name`，这里仍做
   第二道校验（解析后必须落在 skills 根之内），并且**符号链接只 unlink 不跟随**
   —— 否则一个指向 `~/Documents` 的同名链接就能让"删除技能"变成灾难。

错误语义沿用 `store_io`：读严格、旁路宽容、写原子 + 0600。
"""

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from logger import get_logger
from paths import SKILL_SOURCES, SKILLS_DIR
from store_io import is_enabled, read_json_lenient, read_json_strict, write_json_atomic

log = get_logger("skill_store")

# 技能名白名单：字母数字与 `._-`，必须以字母或数字开头。
# 目录名直接用它 → 必须同时挡住 `..`、`/`、前导点（隐藏目录）与空白。
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 安装体量上限（市场安装时由后端自己拉文件，所以这些是硬闸门）
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

MANIFEST_NAME = "SKILL.md"


# ═══════════════════════════════════════════════════════════════════════
#  frontmatter（与 skills.SkillLoader 共用同一份实现 —— 单一出处）
# ═══════════════════════════════════════════════════════════════════════

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter → `(meta, body)`。

    ⚠️ 这是**全项目唯一实现**：`skills.SkillLoader._parse_frontmatter` 已改为转调
    这里。两份实现必然漂移，而漂移的后果是「设置页显示的描述与模型看到的不是同一
    条」—— 这类不一致最难排查。

    解析失败**不抛异常**（返回空 meta），理由：一个 frontmatter 有语法错误的技能
    仍应能被列出并让用户看见问题，而不是从列表里凭空消失。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        import yaml

        meta = yaml.safe_load(parts[1]) or {}
    except Exception:  # noqa: BLE001 - YAML 语法错 / 缺 yaml 都降级成空 meta
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].strip()


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("#").strip()
    return ""


def _tags_of(meta: dict) -> list[str]:
    """`tags` / `keywords` 两种写法都收（字符串按逗号切）。"""
    raw = meta.get("tags")
    if raw is None:
        raw = meta.get("keywords")
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


# ═══════════════════════════════════════════════════════════════════════
#  路径安全
# ═══════════════════════════════════════════════════════════════════════

def safe_relpath(rel: str) -> str | None:
    """把市场返回的相对路径规范化；非法（逃逸 / 绝对 / 空）→ `None`。

    接受的形态：`a/b/c.md`、`./a.md`。拒绝：绝对路径、`..`、NUL、空段。
    反斜杠统一折成正斜杠（Windows 打包的仓库里出现过）。
    """
    if not isinstance(rel, str):
        return None
    s = rel.replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    if not s or s.startswith("/") or "\x00" in s:
        return None
    if re.match(r"^[A-Za-z]:", s):  # Windows 盘符
        return None
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def validate_name(name: str) -> list[str]:
    """技能名校验（目录名 + 系统提示里的标识符）。返回错误文案列表。"""
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


# ═══════════════════════════════════════════════════════════════════════
#  Store
# ═══════════════════════════════════════════════════════════════════════

class SkillStore:
    """`~/.aigent/skills/` + `skills_sources.json` 的门面（无状态，多实例共享同一份磁盘）。"""

    def __init__(self, skills_dir: Path | str | None = None,
                 sources_path: Path | str | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        # 元数据文件名**从常量取**，但目录随 `skills_dir` 走 —— 这样"给一个自定义
        # 技能目录"得到的是一个自洽的 store（它的启停状态就写在旁边），而不是
        # "技能读 A 目录、启停写 B 目录"的错位。默认值下两者完全等价：
        # `AIGENT_HOME/skills` 的父目录就是 `AIGENT_HOME`，与 `SKILL_SOURCES` 同落点
        # （有测试锁住这条等价，改常量时不会悄悄漂移）。
        self.sources_path = (Path(sources_path) if sources_path
                             else self.skills_dir.parent / SKILL_SOURCES.name)

    # ── 读 ────────────────────────────────────────────────────────

    def load_sources(self) -> dict:
        """读旁路元数据（坏文件降级成 `{}`，只记 warning）。"""
        raw = read_json_lenient(self.sources_path, {}, label="skills_sources.json")
        return raw if isinstance(raw, dict) else {}

    def scan(self) -> list[dict]:
        """扫描技能目录 → 全部技能（**含被禁用的**），按名排序。

        每个条目含 frontmatter 解析结果、启停状态、附属文件清单与来源元数据。
        目录不存在时返回空列表（不是错误 —— 新用户就是没有技能）。
        """
        sources = self.load_sources()
        out: list[dict] = []
        if not self.skills_dir.is_dir():
            return out
        try:
            entries = sorted(self.skills_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            log.error("技能目录枚举失败：%s", exc)
            return out
        for d in entries:
            # 只认目录；符号链接**不跟随**（跟随会让扫描跑到任意位置去）
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

    def iter_manifests(self) -> list[tuple[str, Path]]:
        """**只收启用的**技能 → `[(name, SKILL.md 路径)]`，供 `skills.SkillLoader`
        构建系统提示用（它是运行时热路径，不能顺带做展示用的统计）。"""
        out: list[tuple[str, Path]] = []
        for item in self.scan():
            if item["enabled"] and item["has_manifest"]:
                out.append((item["name"], Path(item["manifest"])))
        return out

    def names(self) -> list[str]:
        return [item["name"] for item in self.scan()]

    # ── 写 ────────────────────────────────────────────────────────

    def set_enabled(self, name: str, enabled: bool) -> list[dict]:
        """启用 / 禁用。**只写旁路元数据，绝不碰 SKILL.md。** 技能不存在 → `ValueError`。"""
        name = (name or "").strip()
        with _dir_lock(self.skills_dir):
            if name not in set(self._dir_names()):
                raise ValueError(f"没有名为「{name}」的技能")
            sources = self.load_sources()
            cur = sources.get(name)
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur["enabled"] = 1 if enabled else 0
            cur.setdefault("source", "local")
            sources[name] = cur
            write_json_atomic(self.sources_path, sources)
        return self.scan()

    def remove(self, name: str) -> tuple[list[dict], list[str]]:
        """删除技能（目录 + 元数据）。不存在时**幂等返回**（不报错）。

        安全：解析后必须落在 skills 根之内；符号链接只 `unlink` 不跟随。
        """
        name = (name or "").strip()
        warnings: list[str] = []
        if not name:
            return self.scan(), ["名称不能为空"]
        target = self.skills_dir / name
        with _dir_lock(self.skills_dir):
            if target.is_symlink():
                # 只删链接本身 —— 跟随删会把链接指向的真实目录一起端掉
                try:
                    target.unlink()
                except OSError as exc:
                    raise OSError(f"删除链接失败：{exc}") from exc
                warnings.append(f"「{name}」是指向别处的符号链接，只删除了链接本身")
            elif target.exists():
                root = self.skills_dir.resolve()
                real = target.resolve()
                if real != root and root not in real.parents:
                    raise ValueError(f"「{name}」的落点超出了技能目录，已拒绝删除")
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    raise OSError(f"删除失败：{exc}") from exc
            sources = self.load_sources()
            if sources.pop(name, None) is not None:
                write_json_atomic(self.sources_path, sources)
        return self.scan(), warnings

    def install(self, name: str, files: dict, meta: dict | None = None) -> list[dict]:
        """安装一个技能：把 `files`（`{相对路径: str|bytes}`）写到 `<skills>/<name>/`。

        必须先全量校验、后落盘（校验不过**一个字节都不写**）—— 避免留下半装的技能
        目录，那种状态既不能被正常加载、用户也看不出问题在哪。

        同名目录已存在 → `ValueError`（调用方应先走 `unique_name` 拿到不冲突的名字，
        而不是让这里静默覆盖掉用户可能改过的技能）。
        """
        name = (name or "").strip()
        errors = validate_name(name) + validate_files(files)
        if errors:
            raise ValueError("；".join(errors))

        target = self.skills_dir / name
        with _dir_lock(self.skills_dir):
            if target.exists() or target.is_symlink():
                raise ValueError(f"已存在同名技能「{name}」，请改名后再装")

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
                # 回滚：留下的半成品比装不上更难排查
                shutil.rmtree(target, ignore_errors=True)
                if isinstance(exc, ValueError):
                    raise
                raise OSError(f"写入技能文件失败：{exc}") from exc

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
        """合并写旁路元数据（不改启停、不动技能文件）。"""
        name = (name or "").strip()
        if not name:
            return
        with _dir_lock(self.skills_dir):
            sources = self.load_sources()
            cur = sources.get(name)
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur.update(meta or {})
            sources[name] = cur
            write_json_atomic(self.sources_path, sources)

    # ── 内部 ──────────────────────────────────────────────────────

    def _dir_names(self) -> list[str]:
        if not self.skills_dir.is_dir():
            return []
        try:
            return [p.name for p in self.skills_dir.iterdir()
                    if p.is_dir() and not p.is_symlink()]
        except OSError:
            return []

    def _describe(self, d: Path, meta) -> dict | None:
        """单个技能目录 → UI 条目。整个函数逐字段兜底：任何一项读失败都不能让
        这个技能从列表里消失（"技能不见了"比"描述是空的"难排查得多）。"""
        name = d.name
        manifest = d / MANIFEST_NAME
        meta = meta if isinstance(meta, dict) else {}
        warnings: list[str] = []

        raw = ""
        has_manifest = manifest.is_file()
        if has_manifest:
            try:
                raw = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                has_manifest = False
                warnings.append(f"SKILL.md 读取失败：{exc}")
        else:
            warnings.append(f"缺少 {MANIFEST_NAME}，该技能不会被加载")

        fm, body = parse_frontmatter(raw) if raw else ({}, "")
        desc = str(fm.get("description") or "").strip() or _first_line(body or raw)
        if has_manifest and not str(fm.get("description") or "").strip():
            warnings.append("SKILL.md 的 frontmatter 缺少 description，列表里只能用正文首行代替")

        files, size = _walk_files(d)
        return {
            "name": name,
            "title": str(fm.get("name") or name).strip() or name,
            "description": desc,
            "tags": _tags_of(fm),
            "enabled": is_enabled(meta.get("enabled", 1)),
            "has_manifest": has_manifest,
            "path": str(d),
            "manifest": str(manifest),
            "files": files,
            "file_count": len(files),
            "size_bytes": size,
            "source": str(meta.get("source") or "local"),
            "market_id": meta.get("market_id"),
            "market_name": meta.get("market_name"),
            "publisher": meta.get("publisher"),
            "market_url": meta.get("market_url"),
            "installed_at": meta.get("installed_at"),
            "warnings": warnings,
        }


# ═══════════════════════════════════════════════════════════════════════
#  模块级工具
# ═══════════════════════════════════════════════════════════════════════

def validate_files(files) -> list[str]:
    """安装体校验：类型 / 路径 / 必须有 SKILL.md / 体积。返回错误文案列表。"""
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
        if safe == MANIFEST_NAME:
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
        errors.append(f"技能必须包含 {MANIFEST_NAME}")
    if total > MAX_TOTAL_BYTES:
        errors.append(f"总体积超过上限（{total // 1024} KiB > {MAX_TOTAL_BYTES // 1024} KiB）")
    return errors


def unique_name(base: str, taken: list[str]) -> str:
    """撞名 → `-2`、`-3`…（与 `mcp_market._unique_name` 同策略，前端零参与）。"""
    base = re.sub(r"[^A-Za-z0-9._-]", "-", (base or "").strip()).strip(".-") or "skill"
    if not NAME_RE.match(base):
        base = ("s-" + base)[:64]
    taken_set = {t for t in taken if isinstance(t, str)}
    if base not in taken_set:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in taken_set and NAME_RE.match(cand):
            return cand
    return base


def market_meta(market_id: str, market_name: str = "", publisher: str = "",
                market_url: str = "", source: str = "market") -> dict:
    """构造一条技能市场来源元数据。"""
    return {
        "source": source,
        "market_id": market_id,
        "market_name": market_name,
        "publisher": publisher,
        "market_url": market_url,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_skill_md(name: str, description: str, tags=None, body: str = "") -> str:
    """拼一份新的 SKILL.md（设置页「手动新建技能」用）。

    `name` / `description` 用 JSON 字面量输出而不是直接拼接：description 里出现
    `:`、`#`、引号、换行都是常态（"Use this when: ..." 这类写法很常见），裸拼会
    产出一个 YAML 语法错误的 frontmatter。YAML 的标量是 JSON 的超集，所以
    `key: "..."` 这种写法两边都能解析。
    """
    lines = [
        "---",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
    ]
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if tag_list:
        # 输出 **YAML flow sequence**（JSON 数组本身就是合法 YAML）而不是 `a, b`
        # —— 后者解析回来是一个字符串，任何消费方（含别的 Agent）都得自己再切一次。
        lines.append(f"tags: {json.dumps(tag_list, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append((body or "").strip())
    lines.append("")
    return "\n".join(lines)


def read_skill_text(path: Path | str, limit: int = 200_000) -> str:
    """读技能正文（设置页预览用）。超长截断，绝不上抛。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读取失败：{exc}）"
    if len(text) > limit:
        return text[:limit] + "\n\n…（内容过长，已截断）"
    return text


def _walk_files(d: Path) -> tuple[list[str], int]:
    """技能目录里的**全部**文件（相对路径，**含 SKILL.md**）+ 总体积。

    刻意把 SKILL.md 也算进去：这里给出的 `file_count` 要与市场安装计划页显示的
    文件数**对得上**（那边当然包含 SKILL.md）。"装上之后有 12 个文件、列表却写 11"
    这种差 1 的账最能消耗信任。界面要展示"附属文件"时由一个过滤条件排除它即可。

    `followlinks=False` 是必须的：否则一个指向大目录的链接会让这里扫爆。
    """
    files: list[str] = []
    total = 0
    for root, dirs, names in os.walk(d, followlinks=False):
        dirs[:] = [x for x in sorted(dirs) if not (Path(root) / x).is_symlink()]
        for fn in sorted(names):
            p = Path(root) / fn
            if p.is_symlink():
                continue
            try:
                rel = p.relative_to(d).as_posix()
            except ValueError:
                continue
            files.append(rel)
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return files, total


def _dir_lock(path: Path):
    """复用 `store_io` 的按路径锁 —— 写目录与写元数据必须串行，
    否则「安装」与「删除」并发会留下"目录在、元数据没了"的中间态。"""
    from store_io import _lock_for  # noqa: PLC0415 - 私有工具，仅此处内部复用

    return _lock_for(Path(path))


# 严格读的入口保留给上层（读坏文件要能区分"读失败"与"没有技能"）
def load_sources_strict(path: Path | str | None = None) -> dict:
    """严格读旁路元数据（损坏 → `ValueError`）。设置页回执用它区分错误与空列表。"""
    return read_json_strict(Path(path) if path else SKILL_SOURCES, {}, expect=dict)
