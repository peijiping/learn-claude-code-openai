#!/usr/bin/env python3
"""
git_changes.py - 右栏「变更」面板的后端取数（git status / 单文件 diff）

设计见 docs/frontend/19-右侧面板（文件与变更）.md。**叶子模块**：只依赖标准库 +
`logger`，**禁止 import 引擎模块**（会形成循环依赖）。形状照抄 `worktree.py`：
subprocess + 超时 + 逐层吞异常。

════════════════════════════════════════════════════════════════════════
为什么取数在后端而不是前端
════════════════════════════════════════════════════════════════════════

前端（Electron 主进程）当然也能自己 spawn git。放后端的唯一理由是**口径唯一**：
"当前工作空间根是哪个目录"已经由 `paths.WorkspacePaths` 在 Python 侧定义好了，
让 Electron 再推一遍"项目根"必然出现两套理解（软链、`~`、相对路径、多工作空间
都会分叉）。顺带白拿沙箱语义：`workdir` 一律由桥层从会话解析出来，前端传不了
自己的目录。

════════════════════════════════════════════════════════════════════════
两条硬约定
════════════════════════════════════════════════════════════════════════

1. **非 git 仓库不是错误**：返回 `available=False` + `reason` 人话，**不抛异常**。
   "这个文件夹不是仓库"是常态（新建的临时目录就是这样），前端渲染成一个平级
   空态而不是报错。
2. **只做单文件 diff**：整仓 diff 在稍大的仓库上就能卡住几十秒、输出几十 MB。
   前端要的也只是"我点开这一个文件的改动"。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from logger import get_logger

log = get_logger("git_changes")

# git 调用的统一超时（秒）。太大则前端一直转圈，太小则在冷仓库（大 repo 首次
# 读 index）上误报超时 —— 10s 是在本仓库与几个大仓库上试出来的折中。
DEFAULT_GIT_TIMEOUT = 10
# 单文件 diff 的字符上限（默认 200K 字符）。超限**不截断**而是整份报 too_large：
# 半截 diff 会让用户以为"改动就这么点"，而实际上只是我们砍了；宁可不显示。
DEFAULT_DIFF_MAX_CHARS = 200_000
# status 的文件条数上限（防 `-uall` 在未忽略 node_modules 的仓库上产出十万条）
DEFAULT_STATUS_MAX_FILES = 2000

# porcelain v1 里表示"未合并（冲突）"的两字状态码全集
_CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
# 拒绝把这类相对路径交给 git（既是防御性校验，也让"前端传了绝对路径"立刻暴露）
_UNSAFE_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|[\\/])|(^|[\\/])\.\.([\\/]|$)")


def _int_env(key: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def git_timeout() -> int:
    """git 调用超时（秒）。读在调用点而非导入点 —— 配置合并进 os.environ 的
    时机可能晚于本模块被导入。"""
    return _int_env("GIT_CMD_TIMEOUT", DEFAULT_GIT_TIMEOUT, minimum=1)


def diff_max_chars() -> int:
    """单文件 diff 的字符上限。"""
    return _int_env("GIT_DIFF_MAX_CHARS", DEFAULT_DIFF_MAX_CHARS, minimum=1)


def status_max_files() -> int:
    """status 返回的文件条数上限。"""
    return _int_env("GIT_STATUS_MAX_FILES", DEFAULT_STATUS_MAX_FILES, minimum=1)


# ══════════════════════════════════════════════════════════════════
#  git 调用底座
# ══════════════════════════════════════════════════════════════════

def _run_git(workdir, args: list[str], *, timeout: int | None = None,
             allow_exit_one: bool = False) -> tuple[bool, str, str]:
    """执行 git，返回 `(ok, output, reason)`；**绝不抛异常**。

    统一加 `-c core.quotePath=false`：默认配置下 git 会把非 ASCII 文件名转义成
    `\\344\\270\\255` 这种八进制，中文文件名在 diff 头里将完全不可读。

    `encoding="utf-8", errors="replace"`（与 run_bash 同约定）：日志/仓库里混进
    非 UTF-8 字节（老文件、GBK 注释）时不能把整次调用打死。

    `allow_exit_one`：`git diff --no-index` 用**退出码 1 表示"有差异"**，那是
    正常结果而不是失败。
    """
    cmd = ["git", "-c", "core.quotePath=false", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout if timeout is not None else git_timeout(),
        )
    except subprocess.TimeoutExpired:
        return False, "", f"git 超时（超过 {timeout or git_timeout()} 秒）"
    except FileNotFoundError:
        return False, "", "未找到 git 命令"
    except OSError as exc:
        log.warning("git 调用失败 %r: %s", cmd, exc)
        return False, "", "无法执行 git"
    ok = proc.returncode == 0 or (allow_exit_one and proc.returncode == 1)
    if not ok:
        return False, proc.stdout or "", (proc.stderr or "").strip() or "git 执行失败"
    return True, proc.stdout or "", ""


def _unavailable(reason: str) -> dict:
    """非仓库 / git 缺失的**平级**返回（不是错误信封）。"""
    return {
        "available": False,
        "reason": reason,
        "root": "",
        "branch": "",
        "ahead": 0,
        "behind": 0,
        "files": [],
        "truncated": False,
    }


# ══════════════════════════════════════════════════════════════════
#  status
# ══════════════════════════════════════════════════════════════════

def _parse_porcelain_z(raw: str) -> list[dict]:
    """解析 `git status --porcelain=v1 -z` 的记录流。

    格式（实测）：每条记录 `XY<空格>PATH`，以 NUL 结束；**重命名/复制**多一条
    记录 —— 紧跟其后的那条 NUL 字段是**原路径**（`RM new\\0old\\0`）。

    用 `-z` 而不是按行切：文件名里可以有空格、换行、引号，按行切会全部错位；
    这也是唯一不需要处理 git 引号转义的形态。
    """
    fields = [f for f in raw.split("\0")]
    if fields and fields[-1] == "":
        fields.pop()

    files: list[dict] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        x, y = code[0], code[1]
        orig_path = None
        if x in ("R", "C") or y in ("R", "C"):
            # 下一条字段是原路径；缺失（畸形/被外部截断）时不强求
            if i < len(fields):
                orig_path = fields[i]
                i += 1
        untracked = code == "??"
        conflicted = code in _CONFLICT_CODES
        entry = {
            "path": path,
            "index": x,
            "working_dir": y,
            "code": code,
            "untracked": untracked,
            "conflicted": conflicted,
            # 一个文件可以**同时**出现在"已暂存"与"未暂存"两个分组里（暂存后又改），
            # 这两面都由后端算好，前端只管按标记归组。
            "staged": (not untracked) and (not conflicted) and x not in (" ", "?"),
            "has_working_changes": (not untracked) and (not conflicted)
                                   and y not in (" ", "?"),
            "deleted": "D" in code,
        }
        if orig_path:
            entry["orig_path"] = orig_path
        files.append(entry)
    return files


def status(workdir) -> dict:
    """读取工作空间的 git 状态；**绝不抛异常**。

    返回 `{available, reason, root, branch, ahead, behind, files[], truncated}`。
    `files[]` 每项 `{path, index, working_dir, code, untracked, conflicted,
    staged, has_working_changes, deleted, orig_path?}`，`path` 为**仓库相对路径**
    （与 `diff_file` 的入参口径一致）。
    """
    if not str(workdir or "").strip():
        return _unavailable("未指定工作空间")

    ok, out, reason = _run_git(workdir, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        return _unavailable("当前工作空间不是 Git 仓库")

    root = ""
    ok, out, _ = _run_git(workdir, ["rev-parse", "--show-toplevel"])
    if ok:
        root = out.strip()

    branch = ""
    ok, out, _ = _run_git(workdir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if ok:
        branch = out.strip()
        if branch == "HEAD":
            branch = ""  # detached：不展示一个叫 "HEAD" 的分支名

    ahead = behind = 0
    # `@{upstream}` 在没有远端跟踪分支时会失败 —— 那是常态（本地新仓库），静默跳过
    ok, out, _ = _run_git(workdir, ["rev-list", "--left-right", "--count",
                                   "@{upstream}...HEAD"])
    if ok and out.strip():
        parts = out.split()
        if len(parts) >= 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                ahead = behind = 0

    ok, raw, reason = _run_git(
        workdir,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if not ok:
        result = _unavailable(reason or "读取 git 状态失败")
        # 仓库是好的、只是 status 失败：把已拿到的 root/branch 保留，便于排障
        result.update({"available": True, "reason": reason, "root": root,
                       "branch": branch})
        return result

    files = _parse_porcelain_z(raw)
    cap = status_max_files()
    truncated = len(files) > cap
    if truncated:
        files = files[:cap]
    files.sort(key=lambda f: f["path"])
    return {
        "available": True,
        "reason": "",
        "root": root,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "files": files,
        "truncated": truncated,
    }


# ══════════════════════════════════════════════════════════════════
#  单文件 diff
# ══════════════════════════════════════════════════════════════════

def _safe_rel_path(workdir, raw_path) -> str | None:
    """把前端传来的相对路径钉死在仓库内；不合法返回 None。

    路径会作为参数交给 git，所以两道防线都要有：语法上拒绝对路径与 `..` 穿越，
    语义上再用 `resolve()` 确认落点真的在 `workdir` 之内（防软链绕出）。
    """
    text = str(raw_path or "").strip()
    if not text or _UNSAFE_PATH_RE.search(text):
        return None
    try:
        base = Path(workdir).expanduser().resolve()
        resolved = (base / text).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != base and not resolved.is_relative_to(base):
        return None
    return text


def diff_file(workdir, raw_path, *, staged: bool = False) -> dict:
    """取单个文件的 diff（git 原文，前端用 diff2html 渲染）；**绝不抛异常**。

    `path` 必须是**仓库相对路径**（`status()` 的产出即为该口径）。未跟踪文件
    走 `--no-index`（`git diff` 对未跟踪文件什么都不输出），这样"新文件"也能
    看到内容；`--no-index` 用退出码 1 表示"有差异"，属正常结果。

    返回 `{path, staged, available, reason, diff, chars, too_large, binary,
    untracked}`。`too_large` 时 `diff` 为空 —— 见模块头对"不截断"的说明。
    """
    result = {
        "path": str(raw_path or ""),
        "staged": bool(staged),
        "available": True,
        "reason": "",
        "diff": "",
        "chars": 0,
        "too_large": False,
        "binary": False,
        "untracked": False,
    }
    if not str(workdir or "").strip():
        result.update({"available": False, "reason": "未指定工作空间"})
        return result

    rel = _safe_rel_path(workdir, raw_path)
    if rel is None:
        result.update({"available": False, "reason": "路径不在当前工作空间内"})
        return result

    ok, out, reason = _run_git(workdir, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        result.update({"available": False, "reason": "当前工作空间不是 Git 仓库"})
        return result

    # 先判"是否未跟踪"：`git diff` 对未跟踪文件**什么都不输出**，不问这一步就会
    # 把"新文件"静默显示成"没有改动"。用 `ls-files --others` 而不是跑一遍完整
    # status —— 只为这一个判断付一次全仓 status 的代价不划算。
    ok, out, _ = _run_git(
        workdir, ["ls-files", "--others", "--exclude-standard", "--", rel],
    )
    untracked = bool(ok and out.strip())
    result["untracked"] = untracked

    if untracked:
        # `--no-index` 必须给两个真实路径：/dev/null 当"空文件"一侧
        ok, out, reason = _run_git(
            workdir, ["diff", "--no-index", "--", os.devnull, rel],
            allow_exit_one=True,
        )
    else:
        args = ["diff"]
        if staged:
            args.append("--cached")
        args += ["--", rel]
        ok, out, reason = _run_git(workdir, args, allow_exit_one=True)

    if not ok:
        result.update({"available": False, "reason": reason or "读取 diff 失败"})
        return result

    if "Binary files" in out or "GIT binary patch" in out:
        result["binary"] = True

    cap = diff_max_chars()
    result["chars"] = len(out)
    if len(out) > cap:
        result["too_large"] = True
        return result
    result["diff"] = out
    result["reason"] = "" if out.strip() else "没有可显示的改动"
    return result
