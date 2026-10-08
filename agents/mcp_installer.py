#!/usr/bin/env python3
r"""
mcp_installer.py - MCP 本地包安装器（npm registry → `~/.aigent/mcp/pkgs/`）

背景（docs/frontend/23 §本地安装）：原先只有"市场条目 → `npx -y pkg@ver`"一条路，
包由 npm 在**首次连接时**隐式拉取，缓存落在 `~/.npm/_npx/<hash>/` —— 不在应用
的收口体系里，没有"装了什么、什么版本、什么时候装的"这个概念，也无法卸载。
本模块提供显式安装：一个包一个专属目录，装完把 `command` 指向包内 bin 的绝对路径。

── 安全模型（本模块存在的全部理由，改之前先读）────────────────────────────

安装第三方包 = 在本机执行陌生代码。这里按攻击面逐条设防，**全部是默认行为**：

| 攻击面 | 对策 |
| --- | --- |
| 路径穿越 / 参数注入（`../../x`、`--prefix=/etc`、`-g`） | `SPEC_RE` 白名单正则；名字必须以字母数字开头，`:`/`/`/`\` 全不在字符集内 |
| 非 registry 依赖形态（`git+https:`、`https://…tgz`、`file:`、`npm:alias`） | 同上正则天然挡掉（不含 `:` 与 `//`） |
| 浮动版本（`latest`/`^1`/`~1.2`/`1.x`） | 只收精确 `X.Y.Z[-pre][+build]`；缺版本时向 registry 问一次**并钉死** |
| `postinstall` 等生命周期脚本 RCE | 强制 `--ignore-scripts`（CLI flag 优先级高于任何 `.npmrc` / env） |
| 用户 `.npmrc` 把 registry 偷换成镜像或私服 | 强制 `--registry=<配置值>`，且该值在确认弹窗**明文展示**，绝不静默读取 |
| 包 `bin` 字段写 `../../..` 逃逸出安装目录 | 装后对 bin 做 `realpath` 包含检查，越界即拒 |
| 供应链篡改 / 装后被本地替换 | resolve 时记 `dist.integrity`，装后与 `package-lock.json` 对账，不符即失败 |
| 装到一半崩溃 / 关窗口 | 先落 `.install-incomplete` 哨兵，成功才删；带哨兵的目录对 UI 是"未完成" |
| 卸载时路径逃逸 | slug 必须单段 + 父目录 `realpath` 包含检查，才允许 `rmtree` |

三条**刻意不做**（避免被当成遗漏）：
  1. **不隔离 `.npmrc`**（不传 `--userconfig`）—— 会连带杀掉用户的 proxy 配置，
     而 proxy 本身就是设计上的 MITM，隔离与否并不改变这一事实。
  2. **不进沙盒跑 npm** —— 要联网、要写 `~/.aigent`，Seatbelt/bwrap 那套模板会挡死。
  3. **不做 PyPI 本地安装** —— 那条路要落 `uv tool install` / `pip --target`，
     与"统一 uv 管理、禁止擅自装包"的口径冲突，需单独定规矩。

── 可测试性 ────────────────────────────────────────────────────────────

所有子进程调用都走可注入的 `runner`（签名 `(argv, cwd, timeout) -> (rc, out, err)`），
测试里换成假实现就**永远不会真的去下包**。这是本模块唯一的测试接缝，务必保留。

── 平台 ────────────────────────────────────────────────────────────────

bin 解析按 POSIX（`node_modules/.bin/<name>` 符号链接）实现。Windows 上是 `.cmd`
shim，`StdioServerParameters` 无法直接执行，故本模块在 Windows 上会明确报不支持，
而不是装完了才发现起不来。
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from logger import get_logger
from paths import MCP_PKGS_DIR

log = get_logger("mcp_installer")


# ═══════════════════════════════════════════════════════════════════════
#  常量与配置
# ═══════════════════════════════════════════════════════════════════════

# 官方 registry。**默认强制走这里**：镜像能同时篡改元数据与哈希，
# 安全性降一档，必须由用户显式配置（且该值会显示在确认弹窗里）。
DEFAULT_REGISTRY = "https://registry.npmjs.org/"
# 允许用户改的 registry 只接受 https（公司私服也应是 https；http 等于明网投毒）
_REGISTRY_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(/.*)?$")

# 安装目录里的两个自有文件（与 npm 的 package.json / node_modules 并列）
META_FILENAME = "aigent-meta.json"
SENTINEL_FILENAME = ".install-incomplete"

# 包名与规格：**唯一的安全入口**。不匹配即拒，不做任何"容错修正"。
_PKG_CHARS = r"[A-Za-z0-9][A-Za-z0-9._~-]*"
_SEMVER = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
SPEC_RE = re.compile(
    rf"^(?P<name>(?:@{_PKG_CHARS}/{_PKG_CHARS})|(?:{_PKG_CHARS}))@(?P<version>{_SEMVER})$")
# 只有名字、没有版本（`plan()` 补问 latest 之前必须先过这一关）。
#
# ⚠️ 为什么必须有它：`plan()` 在缺版本时会先发一次 `npm view <name>@latest`，
# 此刻 `<name>` 是**还没经过 SPEC_RE 的裸字符串**。若跳过校验，`--prefix=/etc`
# 这种"包名"会被 npm 当成命令行选项吃掉 —— argv 数组挡不住以 `-` 开头的参数，
# 那是真正的注入点。校验后名字恒以字母数字或 `@` 开头，选项注入即不可能。
NAME_ONLY_RE = re.compile(rf"^(?:@{_PKG_CHARS}/{_PKG_CHARS}|{_PKG_CHARS})$")
NAME_MAX = 214  # npm 对包名的长度上限

LOG_TAIL_CHARS = 4000


class InstallError(Exception):
    """可预期的业务失败（spec 非法、版本不存在、校验不符…）。

    `ws_bridge` 捕获它 → `{ok: False, error: str(exc)}`，**不上抛**（分发链无兜底）。
    """


# 子进程执行器：`(argv, cwd, timeout) -> (returncode, stdout, stderr)`。
# 抛 `subprocess.TimeoutExpired` 表示超时（由 `_exec` 统一转成中性失败结果）。
Runner = Callable[[Sequence[str], "str | None", int], "tuple[int, str, str]"]


def npm_path() -> str:
    """npm 可执行文件路径。`MCP_NPM_PATH` 可覆盖（打包后 PATH 可能与开发机不同）。"""
    configured = (os.environ.get("MCP_NPM_PATH") or "").strip()
    if configured:
        return configured
    return shutil.which("npm") or "npm"


def registry() -> str:
    """安装用的 registry。默认官方源；用户配置非法时**退回官方源**并记警告。"""
    raw = (os.environ.get("MCP_NPM_REGISTRY") or "").strip() or DEFAULT_REGISTRY
    if not _REGISTRY_RE.match(raw):
        log.warning("MCP_NPM_REGISTRY 非法（%r），已退回官方源", raw)
        return DEFAULT_REGISTRY
    return raw if raw.endswith("/") else raw + "/"


def resolve_timeout() -> int:
    return int(os.environ.get("MCP_RESOLVE_TIMEOUT", "60"))


def install_timeout() -> int:
    return int(os.environ.get("MCP_INSTALL_TIMEOUT", "180"))


# ═══════════════════════════════════════════════════════════════════════
#  规格校验 / 目录命名
# ═══════════════════════════════════════════════════════════════════════

def parse_spec(spec: str) -> tuple[str, str]:
    """`name@X.Y.Z` → `(name, version)`。**任何非精确版本的形态都拒。**

    拒绝清单（都是真实可用的 npm 语法，正因为可用才必须显式拒）：
      `pkg`（浮动）· `pkg@latest` · `pkg@^1.0.0` · `pkg@1.x` · `pkg@1.0`
      `git+https://…` · `https://…tgz` · `file:../x` · `github:u/r` · `npm:alias@1`
      `../../etc/passwd` · `--prefix=/etc` · `@scope/../../x`
    """
    text = (spec or "").strip()
    if not text:
        raise InstallError("安装规格不能为空（应为 包名@精确版本）")
    if len(text) > NAME_MAX + 64:
        raise InstallError("安装规格过长")
    m = SPEC_RE.match(text)
    if not m:
        raise InstallError(
            f"不支持的安装规格 {text!r}：只接受 包名@精确版本（如 "
            f"@scope/pkg@1.2.3）。git/URL/file 依赖、latest、^/~/x 这类浮动版本一律拒收")
    name, version = m.group("name"), m.group("version")
    if len(name) > NAME_MAX:
        raise InstallError(f"包名过长（上限 {NAME_MAX} 字符）")
    return name, version


def parse_name(name: str) -> str:
    """只校验包名（不含版本）。`plan()` 在补问 latest 之前必须先过这一关。"""
    text = (name or "").strip()
    if not text:
        raise InstallError("包名不能为空")
    if len(text) > NAME_MAX:
        raise InstallError(f"包名过长（上限 {NAME_MAX} 字符）")
    if not NAME_ONLY_RE.match(text):
        raise InstallError(f"非法的包名：{text!r}（只允许 npm 包名字符，"
                           f"可选 @scope/ 前缀；不接受任何以 - 开头的内容）")
    return text


def pkg_slug(name: str, version: str) -> str:
    """`@scope/pkg@1.2.3` → `scope__pkg@1.2.3`（去掉 `@` 前缀，`/` 折成 `__`）。

    斜杠只可能出现在 scope 分隔处，故换掉它即可让 slug 恒为**单段路径**——
    这是卸载路径安全检查的前提（`Path(slug).name == slug`）。
    """
    raw = f"{name.lstrip('@').replace('/', '__')}@{version}"
    slug = re.sub(r"[^A-Za-z0-9._@+-]", "-", raw).strip(" .-")
    if not slug:
        raise InstallError(f"无法为 {name}@{version} 生成安装目录名")
    return slug


def pkg_dir_for(name: str, version: str) -> Path:
    """该规格的安装目录。**恒在 `MCP_PKGS_DIR` 之下**（由 slug 单段性保证）。"""
    slug = pkg_slug(name, version)
    if Path(slug).name != slug or "/" in slug or "\\" in slug:
        raise InstallError(f"安装目录名非法：{slug!r}")
    return MCP_PKGS_DIR / slug


def _assert_inside(target: Path, root: Path) -> Path:
    """确保 `target` 落在 `root` 之下（`realpath` 比较，挡符号链接逃逸）。"""
    real_root = Path(os.path.realpath(root))
    real_target = Path(os.path.realpath(target))
    if real_target != real_root and real_root not in real_target.parents:
        raise InstallError(f"路径越界：{target} 不在 {root} 之下")
    return real_target


# ═══════════════════════════════════════════════════════════════════════
#  子进程出口
# ═══════════════════════════════════════════════════════════════════════

def _kill_group(proc: subprocess.Popen) -> None:
    """杀掉**整个进程组**：npm 会 fork 子进程，只杀直接子进程会留孤儿。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _default_runner(argv: Sequence[str], cwd: "str | None",
                    timeout: int) -> "tuple[int, str, str]":
    """默认执行器：独立进程组 + 硬超时，超时整组杀。"""
    proc = subprocess.Popen(
        list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            out, err = "", ""
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
    return proc.returncode, out or "", err or ""


def _exec(argv: Sequence[str], *, cwd: "str | None" = None, timeout: int = 60,
          runner: "Runner | None" = None) -> dict:
    """统一执行出口。**

    **绝不上抛**：进程起不来、超时、被信号打死，一律降级成结果字典，
    由调用方决定是"业务失败"还是"可以继续"。业务级错误另走 `InstallError`。
    """
    fn = runner or _default_runner
    argv = [str(a) for a in argv]
    try:
        rc, out, err = fn(argv, str(cwd) if cwd else None, timeout)
    except subprocess.TimeoutExpired as exc:
        tail = _tail(getattr(exc, "output", "") or "")
        log.warning("命令超时（%ss）：%s", timeout, " ".join(argv[:3]))
        return {"rc": -1, "out": "", "err": f"命令超时（{timeout}s）\n{tail}",
                "timeout": True, "spawn_error": False}
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return {"rc": -1, "out": "", "err": f"无法执行命令：{exc}",
                "timeout": False, "spawn_error": True}
    return {"rc": int(rc), "out": out or "", "err": err or "",
            "timeout": False, "spawn_error": False}


def _tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…（已截断）\n" + text[-limit:]


def _npm_base() -> list[str]:
    return [npm_path(), "--registry", registry()]


# ═══════════════════════════════════════════════════════════════════════
#  resolve：问 registry 要真值（不落盘）
# ═══════════════════════════════════════════════════════════════════════

def _view_json(spec: str, *, runner: "Runner | None", timeout: int) -> dict:
    """`npm view <spec> --json` → dict。取不到/解析不了都抛 `InstallError`。"""
    res = _exec([*_npm_base(), "view", spec, "--json"],
                timeout=timeout, runner=runner)
    if res["spawn_error"]:
        raise InstallError(f"找不到 npm：{res['err']}。请先安装 Node.js，"
                           f"或在配置里指定 MCP_NPM_PATH")
    if res["timeout"]:
        raise InstallError(f"查询 registry 超时（{timeout}s）：{spec}")
    if res["rc"] != 0:
        detail = _tail(res["err"]) or _tail(res["out"])
        low = detail.lower()
        if "e404" in low or "not found" in low or "no match" in low:
            raise InstallError(f"registry 上没有这个包或版本：{spec}")
        raise InstallError(f"查询 registry 失败（退出码 {res['rc']}）：{detail}")
    try:
        data = json.loads(res["out"])
    except (ValueError, TypeError) as exc:
        raise InstallError(f"registry 返回的内容无法解析：{exc}") from exc
    if isinstance(data, list):
        # 极少数情况（多版本匹配）会返回数组，取最后一个（npm 按升序给）
        if not data:
            raise InstallError(f"registry 没有返回任何版本信息：{spec}")
        data = data[-1]
    if not isinstance(data, dict):
        raise InstallError(f"registry 返回的结构异常：{type(data).__name__}")
    return data


def _latest_version(name: str, *, runner: "Runner | None", timeout: int) -> str:
    """问 latest dist-tag 并**钉死**成精确版本（绝不把浮动版本写进配置）。"""
    data = _view_json(f"{name}@latest", runner=runner, timeout=timeout)
    version = str(data.get("version") or "").strip()
    if not re.fullmatch(_SEMVER, version):
        raise InstallError(f"无法确定 {name} 的精确版本（registry 返回 {version!r}）")
    return version


def _dry_run_dep_count(spec: str, *, runner: "Runner | None", timeout: int) -> "int | None":
    """`npm install --dry-run` 数一遍整棵依赖树（**尽力而为**，失败返回 None）。

    为什么值得多花一次网络往返：一个可信的包拖进一个恶意传递依赖是供应链攻击的
    标准手法，只报"直接依赖 3 个"会给出虚假的安心感。
    """
    tmp = tempfile.mkdtemp(prefix="aigent-mcp-resolve-")
    try:
        res = _exec(
            [*_npm_base(), "install", spec, "--prefix", tmp, "--dry-run",
             "--json", "--ignore-scripts", "--no-audit", "--no-fund"],
            timeout=timeout, runner=runner)
        if res["rc"] != 0:
            return None
        data = json.loads(res["out"] or "{}")
        if isinstance(data, dict):
            added = data.get("added")
            if isinstance(added, int):
                return added
            packages = data.get("packages")
            if isinstance(packages, dict):
                return len(packages)
        return None
    except (InstallError, ValueError, TypeError, OSError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bin_names_from_view(view: dict) -> list[str]:
    """从 `npm view` 的 `bin` 字段推出可执行名候选（仅用于展示，不用于执行）。

    真正的可执行入口以**装完之后**读包内 `package.json` + 查 `.bin/` 为准
    （见 `_resolve_bin`）—— 这里只是让确认弹窗在下载之前就能告诉用户"会得到哪个命令"。
    """
    raw = view.get("bin")
    if isinstance(raw, str):
        return [str(view.get("name") or "").split("/")[-1]] if raw.strip() else []
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys() if str(k).strip()]
    return []


def plan(name: str, version: str = "", *, runner: "Runner | None" = None) -> dict:
    """把 `name@version` 解析成**安装计划**（供确认弹窗展示，**不落盘、不下载**）。

    返回结构见模块末尾 `_plan_ok` / `_plan_fail`。要点：
      · `version` 为空 → 向 registry 问 latest 并钉死（`pinned_from_latest=True`）；
      · `dep_count` 来自 dry-run，失败为 `None`（UI 显示"未知"，不编数字）；
      · `has_scripts` 为真时 UI **必须**红字列出 `scripts`，并由用户显式开启才执行。
    """
    name = (name or "").strip()
    version = (version or "").strip()
    if not name:
        return _plan_fail("包名不能为空")

    warnings: list[str] = []
    pinned_from_latest = False
    timeout = resolve_timeout()

    try:
        parse_name(name)          # ⚠️ 必须在任何 npm 调用之前（选项注入的唯一防线）
        if not version:
            version = _latest_version(name, runner=runner, timeout=timeout)
            pinned_from_latest = True
            warnings.append(f"该条目未声明版本，已钉死为 registry 当前 latest：{version}")
        else:
            parse_spec(f"{name}@{version}")       # 走同一套正则复核
        spec = f"{name}@{version}"
        view = _view_json(spec, runner=runner, timeout=timeout)
    except InstallError as exc:
        return _plan_fail(str(exc))

    resolved_version = str(view.get("version") or version).strip()
    if resolved_version != version:
        warnings.append(
            f"registry 返回的版本是 {resolved_version}（你填的是 {version}），"
            f"将以 registry 为准")
        version = resolved_version
        spec = f"{name}@{version}"

    dist = view.get("dist") if isinstance(view.get("dist"), dict) else {}
    integrity = str(dist.get("integrity") or "").strip()
    shasum = str(dist.get("shasum") or "").strip()
    if not integrity and not shasum:
        warnings.append("registry 没给完整性哈希，无法做装后对账（安全性降一档）")

    scripts = view.get("scripts")
    scripts = {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}
    # 只关心会在安装期执行的三个钩子
    install_hooks = {k: v for k, v in scripts.items()
                     if k in ("preinstall", "install", "postinstall")}

    direct_deps = view.get("dependencies")
    direct_dep_count = len(direct_deps) if isinstance(direct_deps, dict) else 0

    if view.get("deprecated"):
        warnings.append(f"该版本已被作者标记弃用：{view['deprecated']}")

    dep_count = _dry_run_dep_count(spec, runner=runner, timeout=timeout)

    bins = _bin_names_from_view(view)
    if not bins:
        warnings.append("该包没有声明 bin，装完也无法作为 stdio 服务启动")

    try:
        target_dir = pkg_dir_for(name, version)
    except InstallError as exc:
        return _plan_fail(str(exc))

    already = None
    meta = _read_meta(target_dir)
    if meta and meta.get("name") == name and meta.get("version") == version:
        already = {"slug": target_dir.name, "dir": str(target_dir),
                   "command": meta.get("command") or "",
                   "installed_at": meta.get("installed_at") or ""}

    return {
        "ok": True,
        "spec": spec,
        "name": name,
        "version": version,
        "slug": target_dir.name,
        "dir": str(target_dir),
        "registry": registry(),
        "integrity": integrity,
        "shasum": shasum,
        "tarball": str(dist.get("tarball") or ""),
        "description": str(view.get("description") or ""),
        "direct_dep_count": direct_dep_count,
        "dep_count": dep_count,
        "scripts": scripts,
        "install_hooks": install_hooks,
        "has_scripts": bool(install_hooks),
        "bins": bins,
        "default_bin": _pick_default_bin(name, bins),
        "pinned_from_latest": pinned_from_latest,
        "already_installed": already,
        "warnings": warnings,
        "error": "",
    }


def _plan_fail(msg: str) -> dict:
    return {"ok": False, "spec": "", "name": "", "version": "", "slug": "", "dir": "",
            "registry": registry(), "integrity": "", "shasum": "", "tarball": "",
            "description": "", "direct_dep_count": 0, "dep_count": None,
            "scripts": {}, "install_hooks": {}, "has_scripts": False,
            "bins": [], "default_bin": "", "pinned_from_latest": False,
            "already_installed": None, "warnings": [], "error": msg}


def plan_fail(msg: str) -> dict:
    """公开的失败计划（桥层兜底用）。形状与 `plan()` 成功路径**完全同构** ——
    前端只解析一种结构，不必为"意外异常"再写一条降级分支。"""
    return _plan_fail(msg)


def install_fail(msg: str) -> dict:
    """公开的失败安装回执（形状与 `install()` 成功路径同构）。"""
    return _install_fail(msg)


def _pick_default_bin(name: str, bins: Sequence[str]) -> str:
    """多个 bin 时优先选与包短名同名的那个（`npx pkg` 的默认行为）。"""
    short = name.split("/")[-1]
    for b in bins:
        if b == short:
            return b
    return bins[0] if bins else ""


# ═══════════════════════════════════════════════════════════════════════
#  install：真正下载（唯一会动网络的写操作）
# ═══════════════════════════════════════════════════════════════════════

def install(name: str, version: str, *, bin_name: str = "",
            allow_scripts: bool = False,
            runner: "Runner | None" = None) -> dict:
    """把包装进 `~/.aigent/mcp/pkgs/<slug>/`，返回可写进配置的 `command`。

    强制参数（**不要因为"某个包装不上"就删掉其中任何一个**）：
      `--ignore-scripts`  除 `allow_scripts=True` 外恒在（生命周期脚本 = 装包期 RCE）
      `--registry <配置>`  命令行 flag 优先级高于 `.npmrc` / env，堵住静默换源
      `--prefix <目标>`    一个包一个专属目录，卸载 = 删这个目录
      `--no-audit --no-fund` 少两个无关的网络往返，缩短暴露窗口
    """
    timeout = install_timeout()
    started = time.time()

    try:
        parse_spec(f"{name}@{version}")
    except InstallError as exc:
        return _install_fail(str(exc))
    try:
        target = pkg_dir_for(name, version)
    except InstallError as exc:
        return _install_fail(str(exc))
    _assert_inside(target, MCP_PKGS_DIR)

    warnings: list[str] = []
    if os.name == "nt":
        return _install_fail("Windows 暂不支持本地安装（bin 是 .cmd shim，无法直接执行）")

    # 目录名冲突保护：slug 是把 `/` 折成 `__` 得来的，理论上有极小概率与另一个
    # 包名撞车。撞了必须响亮失败，绝不能把别人的包当自己的用。
    existing_meta = _read_meta(target)
    if existing_meta and (existing_meta.get("name") != name
                          or existing_meta.get("version") != version):
        return _install_fail(
            f"目录名冲突：{target.name} 已被 {existing_meta.get('name')}"
            f"@{existing_meta.get('version')} 占用，请先卸载它")

    if target.exists() and existing_meta and not (target / SENTINEL_FILENAME).exists():
        command = str(existing_meta.get("command") or "")
        if command and Path(command).exists():
            return {
                "ok": True, "reused": True, "slug": target.name, "dir": str(target),
                "command": command, "args": [], "bins": existing_meta.get("bins") or [],
                "bin": existing_meta.get("bin") or "", "version": version,
                "integrity": existing_meta.get("integrity") or "",
                "registry": existing_meta.get("registry") or registry(),
                "dep_count": existing_meta.get("dep_count"),
                "elapsed_ms": 0, "scripts": existing_meta.get("scripts") or {},
                "scripts_allowed": bool(existing_meta.get("scripts_allowed")),
                "log": "", "warnings": ["该包已在本机安装，直接复用"], "error": "",
            }

    if not allow_scripts:
        # 先问一次这个包到底声明了什么安装钩子 —— 不为了拦下它，而是为了在
        # 装完之后能如实告诉用户"这次跳过了哪些脚本"。
        try:
            view = _view_json(f"{name}@{version}", runner=runner,
                              timeout=resolve_timeout())
            hooks = view.get("scripts") if isinstance(view.get("scripts"), dict) else {}
            skipped = {k: v for k, v in hooks.items()
                       if k in ("preinstall", "install", "postinstall")}
            if skipped:
                warnings.append(
                    "该包声明了安装期脚本，本次已全部跳过（--ignore-scripts）："
                    + "、".join(sorted(skipped)))
        except InstallError:
            pass

    target.mkdir(parents=True, exist_ok=True)
    sentinel = target / SENTINEL_FILENAME
    try:
        sentinel.write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as exc:
        return _install_fail(f"无法写入安装目录：{exc}")

    argv = [*_npm_base(), "install", f"{name}@{version}",
            "--prefix", str(target),
            "--no-audit", "--no-fund", "--save-exact", "--loglevel=error"]
    if not allow_scripts:
        argv.append("--ignore-scripts")

    res = _exec(argv, timeout=timeout, runner=runner)
    elapsed = int((time.time() - started) * 1000)
    log_tail = _tail("\n".join(x for x in (res["out"], res["err"]) if x))

    if res["spawn_error"]:
        return _install_fail(f"找不到 npm：{res['err']}。请先安装 Node.js，"
                             f"或在配置里指定 MCP_NPM_PATH", target=target)
    if res["timeout"]:
        return _install_fail(f"安装超时（{timeout}s），已中止。{log_tail}", target=target)
    if res["rc"] != 0:
        return _install_fail(f"安装失败（退出码 {res['rc']}）：{log_tail}", target=target)

    # ── 装后校验（三件事，缺一不可）──────────────────────────────────
    lock = _read_lock_integrity(target, name)
    meta_integrity = ""
    try:
        view = _view_json(f"{name}@{version}", runner=runner, timeout=resolve_timeout())
        dist = view.get("dist") if isinstance(view.get("dist"), dict) else {}
        meta_integrity = str(dist.get("integrity") or "").strip()
    except InstallError as exc:
        warnings.append(f"装后未取到 registry 哈希，跳过对账：{exc}")
    if lock and meta_integrity and lock != meta_integrity:
        return _install_fail(
            "完整性校验不符：安装得到的哈希与 registry 声明的不一致，"
            "可能是该版本在两次请求之间被重新发布。为安全起见已中止，请重试",
            target=target)
    if not lock:
        # 对账没跑成要**说出来** —— 静默降级等于让人以为有这道防线（这正是修复前的
        # 实况：固定 key 取值永远取空，而回执里一个字的提示都没有）。
        warnings.append(
            "装后未能从 package-lock.json 读到该包的哈希，本次跳过了对账"
            "（npm 下载时已按 registry 声明校验过 tarball）")

    try:
        command, bins, chosen = _resolve_bin(name, target, bin_name)
    except InstallError as exc:
        return _install_fail(str(exc), target=target)

    meta = {
        "spec": f"{name}@{version}",
        "name": name,
        "version": version,
        "registry": registry(),
        "integrity": lock or meta_integrity,
        "bin": chosen,
        "bins": bins,
        "command": command,
        "scripts_allowed": bool(allow_scripts),
        "scripts": _collect_scripts(target, name),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dep_count": _count_installed(request_dir=target),
    }
    try:
        _write_meta(target, meta)
        sentinel.unlink(missing_ok=True)
    except OSError as exc:
        return _install_fail(f"安装完成但无法写入元数据：{exc}", target=target)

    log.info("MCP 本地包已安装：%s → %s", meta["spec"], command)
    return {
        "ok": True, "reused": False, "slug": target.name, "dir": str(target),
        "command": command, "args": [], "bins": bins, "bin": chosen,
        "version": version, "integrity": meta["integrity"],
        "registry": meta["registry"], "dep_count": meta["dep_count"],
        "elapsed_ms": elapsed, "scripts": meta["scripts"],
        "scripts_allowed": bool(allow_scripts),
        "log": log_tail, "warnings": warnings, "error": "",
    }


def _install_fail(msg: str, *, target: "Path | None" = None) -> dict:
    """失败回执。**清掉哨兵并删掉半成品目录**，不留垃圾给下一次安装。"""
    if target is not None:
        try:
            if (target / SENTINEL_FILENAME).exists():
                shutil.rmtree(target, ignore_errors=True)
                log.info("已清理未完成的安装目录：%s", target)
        except OSError as exc:
            log.warning("清理未完成安装失败：%s: %s", type(exc).__name__, exc)
    return {"ok": False, "reused": False, "slug": "", "dir": "", "command": "",
            "args": [], "bins": [], "bin": "", "version": "", "integrity": "",
            "registry": registry(), "dep_count": None, "elapsed_ms": 0,
            "scripts": {}, "scripts_allowed": False, "log": "",
            "warnings": [], "error": msg}


# ═══════════════════════════════════════════════════════════════════════
#  bin 解析（装后，带逃逸检查）
# ═══════════════════════════════════════════════════════════════════════

def _resolve_bin(name: str, target: Path, prefer: str = "") -> tuple[str, list[str], str]:
    """算出可执行的**绝对路径**，并确保它没有跑出安装目录。

    npm 会在 `<prefix>/node_modules/.bin/` 下为每个 bin 建符号链接。这里解析
    符号链接后再做包含检查 —— 恶意 `package.json` 的 `bin` 字段可以写
    `../../../../bin/sh`，只查链接存在与否是挡不住的。
    """
    pkg_json = target / "node_modules" / name / "package.json"
    if not pkg_json.is_file():
        raise InstallError(f"安装后找不到包目录：{pkg_json}")
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"无法读取已安装包的 package.json：{exc}") from exc

    raw_bin = data.get("bin")
    names: list[str] = []
    if isinstance(raw_bin, str) and raw_bin.strip():
        names = [name.split("/")[-1]]
    elif isinstance(raw_bin, dict):
        names = [str(k) for k in raw_bin.keys() if str(k).strip()]
    if not names:
        raise InstallError("该包没有声明 bin，无法作为 stdio MCP 服务启动。"
                           "若它需要别的启动方式，请改用手动配置")

    bin_dir = target / "node_modules" / ".bin"
    available = [b for b in names if (bin_dir / b).exists()]
    if not available:
        raise InstallError(
            f"包里声明了 bin（{', '.join(names)}），但安装目录下没有对应的可执行文件。"
            f"该包可能依赖安装期脚本（本次已跳过），可勾选“允许安装期脚本”重试")

    chosen = prefer if prefer in available else _pick_default_bin(name, available)
    command = str(_assert_inside(bin_dir / chosen, target))
    if not os.access(command, os.X_OK):
        raise InstallError(f"可执行文件没有执行权限：{command}")
    return command, available, chosen


def _read_lock_integrity(target: Path, name: str) -> str:
    """从 `package-lock.json` 取该包的完整性哈希（npm 在下载时逐字节验过）。

    ⚠️ **不能假设 key 就是 `node_modules/<name>`** —— 实测（npm 10.9.7 / macOS）：
    `npm install <pkg> --prefix <dir>` 会把 key 记成
    `../../private/tmp/…/node_modules/<pkg>`（npm 把 `--prefix` 当 **global prefix**
    处理，于是拿相对它自己的路径记账）。原先按固定 key 取值 → **永远取空** →
    「装后对账」这道防线静默空转。这里是真机验证时才抓到的。

    所以按**路径后缀**匹配，并在多个候选里取**层级最浅**的那个：顶层包必须优先于
    某个依赖内部的同名嵌套副本（`node_modules/cliui/node_modules/string-width`）。
    """
    lock = target / "package-lock.json"
    if not lock.is_file():
        return ""
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, dict):
        return ""
    suffix = f"node_modules/{name}"
    best_integrity = ""
    best_depth: int | None = None
    for key, entry in packages.items():
        if not isinstance(entry, dict):
            continue
        integrity = str(entry.get("integrity") or "").strip()
        if not integrity:
            continue
        parts = [p for p in str(key).replace("\\", "/").split("/") if p not in ("", ".")]
        while parts and parts[0] == "..":
            parts.pop(0)
        tail = "/".join(parts)
        if tail != suffix and not tail.endswith("/" + suffix):
            continue
        if best_depth is None or len(parts) < best_depth:
            best_depth, best_integrity = len(parts), integrity
    return best_integrity


def _collect_scripts(target: Path, name: str) -> dict:
    """记录该包声明了哪些脚本（审计用；本次是否执行看 `scripts_allowed`）。"""
    pkg_json = target / "node_modules" / name / "package.json"
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(k): str(v) for k, v in scripts.items()}


def _count_installed(request_dir: Path) -> int:
    """数一遍 `node_modules` 下的包（含传递依赖），给 UI 一个真实的规模感。"""
    root = request_dir / "node_modules"
    if not root.is_dir():
        return 0
    n = 0
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name == ".bin":
            continue
        if entry.name.startswith("@"):
            n += sum(1 for sub in entry.iterdir() if sub.is_dir())
        else:
            n += 1
    return n


# ═══════════════════════════════════════════════════════════════════════
#  元数据 / 列表 / 卸载 / 复核
# ═══════════════════════════════════════════════════════════════════════

def _read_meta(target: Path) -> "dict | None":
    path = target / META_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_meta(target: Path, meta: dict) -> None:
    path = target / META_FILENAME
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def list_packages() -> list[dict]:
    """扫 `MCP_PKGS_DIR` 下所有安装目录（给设置页「本地包」区块）。

    带 `.install-incomplete` 哨兵的目录标记为 `incomplete` —— 那是一次没跑完的
    安装，UI 要显示成"未完成/可删除"，**绝不能**当成可用包。
    """
    out: list[dict] = []
    if not MCP_PKGS_DIR.is_dir():
        return out
    for entry in sorted(MCP_PKGS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        meta = _read_meta(entry) or {}
        incomplete = (entry / SENTINEL_FILENAME).exists()
        command = str(meta.get("command") or "")
        out.append({
            "slug": entry.name,
            "name": str(meta.get("name") or ""),
            "version": str(meta.get("version") or ""),
            "spec": str(meta.get("spec") or ""),
            "dir": str(entry),
            "command": command,
            "bin": str(meta.get("bin") or ""),
            "bins": list(meta.get("bins") or []),
            "registry": str(meta.get("registry") or ""),
            "integrity": str(meta.get("integrity") or ""),
            "dep_count": meta.get("dep_count"),
            "scripts_allowed": bool(meta.get("scripts_allowed")),
            "installed_at": str(meta.get("installed_at") or ""),
            "size_bytes": _dir_size(entry),
            "status": "incomplete" if incomplete else (
                "ok" if command and Path(command).exists() else "broken"),
        })
    out.sort(key=lambda x: x.get("installed_at") or "", reverse=True)
    return out


def remove(slug: str) -> dict:
    """删掉一个安装目录。**只允许删 `MCP_PKGS_DIR` 的直接子目录**。"""
    text = (slug or "").strip()
    if not text or text in (".", "..") or Path(text).name != text \
            or "/" in text or "\\" in text:
        raise InstallError(f"非法的包标识：{slug!r}")
    target = MCP_PKGS_DIR / text
    if os.path.realpath(target) == os.path.realpath(MCP_PKGS_DIR):
        raise InstallError("拒绝删除安装根目录")
    real = _assert_inside(target, MCP_PKGS_DIR)
    if not real.is_dir():
        return {"ok": True, "slug": text, "freed_bytes": 0, "msg": "该包已不存在"}
    real_dir = Path(real)
    freed = _dir_size(real_dir)
    shutil.rmtree(real_dir)
    log.info("MCP 本地包已卸载：%s（释放 %d 字节）", text, freed)
    return {"ok": True, "slug": text, "freed_bytes": freed, "msg": "已卸载"}


def verify(slug: str) -> dict:
    """按需复核：哈希是否还对得上、bin 是否还在目录内。

    只在用户点「重新校验」时跑 —— 每次连接都做一遍全树哈希会拖慢启动，
    而这里要防的是"装完之后被人动过"，那是低频事件。
    """
    text = (slug or "").strip()
    if not text or Path(text).name != text or "/" in text or "\\" in text:
        raise InstallError(f"非法的包标识：{slug!r}")
    target = MCP_PKGS_DIR / text
    _assert_inside(target, MCP_PKGS_DIR)
    meta = _read_meta(target)
    if not meta:
        return {"ok": False, "slug": text, "errors": ["这个目录没有安装元数据"],
                "checked": {}}

    errors: list[str] = []
    name = str(meta.get("name") or "")
    version = str(meta.get("version") or "")

    if (target / SENTINEL_FILENAME).exists():
        errors.append("这次安装没有完成（存在未完成哨兵），建议卸载后重装")

    lock = _read_lock_integrity(target, name)
    recorded = str(meta.get("integrity") or "")
    if recorded and lock and lock != recorded:
        errors.append("package-lock 里的哈希与安装时记录的不一致（文件可能被改动过）")
    elif not lock:
        errors.append("package-lock.json 里找不到这个包的哈希，无法核对完整性")

    command = str(meta.get("command") or "")
    if not command:
        errors.append("元数据里没有记录可执行文件路径")
    elif not Path(command).exists():
        errors.append(f"可执行文件不存在：{command}")
    else:
        try:
            _assert_inside(Path(command), target)
        except InstallError:
            errors.append(f"可执行文件跑到安装目录外面去了：{command}")

    return {
        "ok": not errors,
        "slug": text,
        "errors": errors,
        "checked": {"name": name, "version": version, "registry": meta.get("registry"),
                    "integrity": recorded, "lock_integrity": lock, "command": command},
    }
