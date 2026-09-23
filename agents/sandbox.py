#!/usr/bin/env python3
"""
sandbox.py - 沙盒执行隔离（执行层的"绝对墙"，与权限管控策略层互补）

定位（2026-09-22 沙盒功能，见 docs/frontend/19）：
    权限管控（permission.py）= 策略层：决定"允不允许、要不要问用户"，靠静态
    分析工具调用参数（可被变量拼接/脚本中转绕过）。
    沙盒（本模块）= 执行层：进程真跑起来时 OS 层面实际能碰什么。接入点在
    ToolRegistry.run_bash（唯一子进程/网络风险面）；文件工具已有 safe_path
    硬墙（进程内写文件不 spawn 子进程，沙盒管不到也不需要管）。

设计核心 —— **用户可编辑的持久化模板**：
    ~/.aigent/sandbox/seatbelt.sb     macOS Seatbelt profile 模板
    ~/.aigent/sandbox/bwrap_args.txt  Linux bubblewrap 参数模板（每行一个参数）

    首次访问自动写入默认模板（幂等，绝不覆盖用户已有文件）。模板支持占位符，
    执行时按会话动态替换（解决"静态文件 vs workdir 每会话不同"的矛盾）：

        {{WORKDIR}}          本会话主工作区
        {{EXTRA_WRITABLE}}   会话批准的额外可写目录（逐目录展开成一段）
        {{TMPDIR}}           系统临时目录
        {{HOME}}             用户主目录
        {{COMMAND}}          仅 bwrap：要执行的命令（整行替换，保持单参数）

    策略细节（网络放行、敏感读屏蔽）全部体现在模板里 —— 用户编辑即改策略；
    config.json 只留总开关 SANDBOX_ENABLED 与后端选择 SANDBOX_BACKEND。

平台矩阵：
    macOS   SeatbeltBackend（sandbox-exec，系统自带零依赖）
    Linux   BubblewrapBackend（bwrap，需安装 bubblewrap；userns 不可用自动降级）
    其他    无后端 → 裸跑 + 每进程一次 warning（诚实降级，不 brick 产品）
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from logger import get_logger
from config import SANDBOX_DIR

log = get_logger("sandbox")

# ── 模板文件落点 ─────────────────────────────────────────────────
SEATBELT_FILE = SANDBOX_DIR / "seatbelt.sb"
BWRAP_FILE = SANDBOX_DIR / "bwrap_args.txt"

# 各模板**必需**的占位符（保存校验 + 执行前校验共用）。
# seatbelt 不需要 COMMAND（命令经 argv 传入，不进 profile 文本）。
REQUIRED_PLACEHOLDERS = {
    "seatbelt": ("{{WORKDIR}}", "{{EXTRA_WRITABLE}}", "{{TMPDIR}}", "{{HOME}}"),
    "bwrap": ("{{WORKDIR}}", "{{EXTRA_WRITABLE}}", "{{TMPDIR}}", "{{HOME}}", "{{COMMAND}}"),
}

DEFAULT_SEATBELT_PROFILE = """\
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "{{WORKDIR}}") (subpath "{{TMPDIR}}") (subpath "/private/tmp"))
{{EXTRA_WRITABLE}}
; /dev 设备回环（git/很多 CLI 依赖 /dev/null、/dev/tty、/dev/fd/*）
(allow file-write* (literal "/dev/null") (literal "/dev/tty") (subpath "/dev/fd"))
(deny network*)
(allow network* (local ip))
(deny file-read* (subpath "{{HOME}}/.ssh") (subpath "{{HOME}}/.aigent"))
"""

DEFAULT_BWRAP_ARGS = """\
--die-with-parent
--new-session
--unshare-ipc
--dev-bind /dev /dev
--proc /proc
--ro-bind / /
--bind {{WORKDIR}} {{WORKDIR}}
{{EXTRA_WRITABLE}}
--tmpfs /tmp
--tmpfs {{TMPDIR}}
--tmpfs {{HOME}}/.ssh
--tmpfs {{HOME}}/.aigent
--
/bin/bash
-c
{{COMMAND}}
"""

# 模板种类 → (默认内容, 文件落点)
_TEMPLATES = {
    "seatbelt": (DEFAULT_SEATBELT_PROFILE, SEATBELT_FILE),
    "bwrap": (DEFAULT_BWRAP_ARGS, BWRAP_FILE),
}


# ── 配置读取（每次使用时读 env，支持设置页热切换）──────────────────
def sandbox_enabled() -> bool:
    """SANDBOX_ENABLED 是否开启（默认 "1"；设置页保存后 ws_bridge 直接覆写 env）。"""
    return os.environ.get("SANDBOX_ENABLED", "1") != "0"


def backend_preference() -> str:
    """SANDBOX_BACKEND 配置：auto / seatbelt / bwrap / off（默认 auto）。"""
    return (os.environ.get("SANDBOX_BACKEND") or "auto").strip().lower()


# ── 模板读写与占位符 ─────────────────────────────────────────────
def validate_template(kind: str, content: str) -> None:
    """校验模板包含必需占位符，缺失抛 ValueError（ws_bridge 保存前调用，拒存）。"""
    missing = [p for p in REQUIRED_PLACEHOLDERS[kind] if p not in content]
    if missing:
        raise ValueError(
            f"沙盒模板缺少必需占位符：{', '.join(missing)}；"
            f"可在设置页「恢复默认模板」找回"
        )


def ensure_templates() -> None:
    """确保 SANDBOX_DIR 与两个默认模板存在（幂等，绝不覆盖用户已有文件）。"""
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    for default, path in _TEMPLATES.values():
        if not path.exists():
            try:
                path.write_text(default, encoding="utf-8")
                log.info("已生成沙盒默认模板: %s", path)
            except OSError as e:
                log.error("沙盒默认模板写入失败: %s (%s)", path, e)


def read_template(kind: str) -> str:
    """读模板内容；不存在先补默认；读盘失败兜底返回默认内容（不让沙盒彻底失效）。"""
    ensure_templates()
    default, path = _TEMPLATES[kind]
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        log.error("沙盒模板读取失败，使用默认内容: %s (%s)", path, e)
        return default


def save_template(kind: str, content: str) -> None:
    """保存用户编辑的模板（保存前校验必需占位符，缺失抛 ValueError 拒存）。"""
    validate_template(kind, content)
    ensure_templates()
    _TEMPLATES[kind][1].write_text(content, encoding="utf-8")


def reset_template(kind: str) -> str:
    """恢复默认模板（设置页「恢复默认模板」按钮），返回重写后的内容。"""
    default, path = _TEMPLATES[kind]
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(default, encoding="utf-8")
    log.info("已恢复沙盒默认模板: %s", path)
    return default


def _expand_extra_writable(kind: str, dirs: list[Path]) -> str:
    """把额外可写目录列表展开成对应模板语法的文本段（空列表 → 空串）。"""
    lines: list[str] = []
    for d in dirs:
        d = str(d)
        if kind == "seatbelt":
            lines.append(f'(allow file-write* (subpath "{d}"))')
        else:
            lines.append(f"--bind {shlex.quote(d)} {shlex.quote(d)}")
    return "\n".join(lines)


def _substitute_common(text: str, workdir: Path, tmpdir: str, home: Path,
                       extra_writable: list[Path], kind: str) -> str:
    """替换值占位符 + EXTRA_WRITABLE 段展开（COMMAND 由各后端自行处理）。

    所有路径先 `.resolve()` 规范化：macOS 的 /var→/private/var 等符号链接会让
    Seatbelt 的 (subpath ...) 匹配落空（表现为工作区内写入也被拦）。
    """
    workdir = Path(workdir).resolve()
    home = Path(home).resolve()
    tmpdir = str(Path(tmpdir).resolve())
    extra_writable = [Path(d).resolve() for d in extra_writable]
    text = text.replace("{{WORKDIR}}", str(workdir))
    text = text.replace("{{TMPDIR}}", tmpdir)
    text = text.replace("{{HOME}}", str(home))
    text = text.replace("{{EXTRA_WRITABLE}}",
                        _expand_extra_writable(kind, extra_writable))
    return text


def _check_placeholders(kind: str, content: str) -> bool:
    """执行前模板体检：缺必需占位符 = 模板被改坏 → 上层降级裸跑 + error log。"""
    return all(p in content for p in REQUIRED_PLACEHOLDERS[kind])


# ── 沙盒拦截特征（run_bash 错误提示用）────────────────────────────
SANDBOX_BLOCK_MARKERS = ("Operation not permitted", "Read-only file system",
                         "sandbox_exec", "bwrap")


def looks_blocked_by_sandbox(stderr: str) -> bool:
    """stderr 是否命中沙盒拦截特征（给模型的错误提示判断用）。"""
    return any(m in (stderr or "") for m in SANDBOX_BLOCK_MARKERS)


# ── 后端抽象与平台探测 ───────────────────────────────────────────
class SandboxBackend:
    """沙盒后端协议：is_available() + run()。run 返回与 subprocess.run 同构的
    CompletedProcess（text=True，stdout/stderr 已解码），供 run_bash 统一消费。"""

    name = "base"

    def is_available(self) -> bool:
        return False

    def run(self, command: str, cwd: Path, workdir: Path,
            extra_writable: list[Path], timeout: int) -> subprocess.CompletedProcess:
        raise NotImplementedError


class SeatbeltBackend(SandboxBackend):
    """macOS Seatbelt（sandbox-exec）。allow-by-default + 定向 deny 模板。"""

    name = "seatbelt"

    def is_available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None

    def run(self, command, cwd, workdir, extra_writable, timeout):
        profile = read_template("seatbelt")
        if not _check_placeholders("seatbelt", profile):
            raise RuntimeError("沙盒模板缺少必需占位符")
        profile = _substitute_common(profile, workdir, tempfile.gettempdir(),
                                     Path.home(), extra_writable, "seatbelt")
        # profile 写临时文件执行，即用即删（sandbox-exec 只接受文件，不接受 stdin）
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sb", encoding="utf-8", delete=False
        )
        try:
            tmp.write(profile)
            tmp.close()
            argv = ["sandbox-exec", "-f", tmp.name, "/bin/bash", "-c", command]
            return subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


class BubblewrapBackend(SandboxBackend):
    """Linux bubblewrap（bwrap）。ro-bind 全根 + 定向 bind/tmpfs 模板。"""

    name = "bwrap"

    _probe_ok: bool | None = None  # 试跑探测结果缓存（每进程一次）

    def is_available(self) -> bool:
        if sys.platform != "linux" or shutil.which("bwrap") is None:
            return False
        if BubblewrapBackend._probe_ok is None:
            # userns 被 disabled（常见于容器内）时 bwrap 直接失败 → 不可用
            try:
                r = subprocess.run(
                    ["bwrap", "--ro-bind", "/", "/", "/bin/true"],
                    capture_output=True, timeout=10,
                )
                BubblewrapBackend._probe_ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                BubblewrapBackend._probe_ok = False
            if not BubblewrapBackend._probe_ok:
                log.warning("bwrap 存在但试跑失败（userns 可能被禁用），沙盒不可用")
        return BubblewrapBackend._probe_ok

    def run(self, command, cwd, workdir, extra_writable, timeout):
        template = read_template("bwrap")
        if not _check_placeholders("bwrap", template):
            raise RuntimeError("沙盒模板缺少必需占位符")
        tmpdir = str(Path(tempfile.gettempdir()).resolve())
        home = str(Path.home().resolve())
        workdir = str(Path(workdir).resolve())
        argv: list[str] = ["bwrap"]
        for raw in template.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "{{EXTRA_WRITABLE}}" in line:
                # 占位符独占一行 → 逐目录展开成 --bind（空目录集则什么都不加）。
                # 直接进 argv，不做 shell 引号处理（shlex 只用于文本模板行）。
                for d in extra_writable:
                    d = str(Path(d).resolve())
                    argv.extend(["--bind", d, d])
            elif "{{COMMAND}}" in line:
                # 命令整行替换且**保持单参数**（bash -c 的语义要求）
                argv.append(command)
            else:
                # 值占位符行：替换后按 shell 规则切分成多个 argv（支持引号路径）
                line = (line.replace("{{WORKDIR}}", workdir)
                            .replace("{{TMPDIR}}", tmpdir)
                            .replace("{{HOME}}", home))
                argv.extend(shlex.split(line))
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )


_BACKENDS = {"seatbelt": SeatbeltBackend, "bwrap": BubblewrapBackend}

_warned_no_backend = False  # 每进程只警告一次，不刷屏


def get_backend() -> SandboxBackend | None:
    """按配置与平台返回可用后端；None = 不沙盒（未启用 / off / 平台不支持）。

    「已启用但后端不可用」时裸跑并**每进程一次** warning（诚实降级），
    「未启用」时静默裸跑（用户主动关闭，无需打扰）。
    """
    global _warned_no_backend
    if not sandbox_enabled() or backend_preference() == "off":
        return None
    pref = backend_preference()
    if pref in _BACKENDS:
        backend = _BACKENDS[pref]()
        if backend.is_available():
            return backend
    else:  # auto：按平台顺序探测
        for kind in ("seatbelt", "bwrap"):
            backend = _BACKENDS[kind]()
            if backend.is_available():
                return backend
    if not _warned_no_backend:
        _warned_no_backend = True
        log.warning(
            "沙盒已启用但当前环境无可用后端（平台=%s, SANDBOX_BACKEND=%s），"
            "命令将不受隔离地执行", sys.platform, pref,
        )
    return None


def backend_status() -> dict:
    """设置页状态行数据：平台、探测到的后端名、是否可用。"""
    pref = backend_preference()
    if pref in _BACKENDS:
        backend = _BACKENDS[pref]()
        available = backend.is_available()
        detected = backend.name if available else None
    else:
        detected, available = None, False
        for kind in ("seatbelt", "bwrap"):
            backend = _BACKENDS[kind]()
            if backend.is_available():
                detected, available = backend.name, True
                break
    return {
        "platform": sys.platform,
        "backend": detected,
        "backend_available": available,
    }
