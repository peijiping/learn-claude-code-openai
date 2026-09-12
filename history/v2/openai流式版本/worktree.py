#!/usr/bin/env python3
"""
worktree.py - WorktreeManager（git worktree 隔离工作区管理）

整合自 s18 课程（worktree isolation），并以「类」方式重构进自有代码库。

与 s18 的三处关键差异：
1. **解耦任务**：不再往 Task 加 worktree 字段，不靠 claim_task 自动切换 cwd；
   关联靠「把子智能体/队友的 cwd 指向某个 worktree」这一**上下文语义**完成。
2. **子智能体 + 团队智能体都支持**：本类只负责工作区生命周期；
   谁把 cwd 指进来（sub_agent(workdir=...) / spawn_teammate(worktree=...)），
   谁就在该 worktree 里作业。
3. **运行时供给**：`.venv` / `.env` 等被 gitignore 的运行时文件不会出现在
   worktree 检出里，导致 agent 无法运行/测试。本类在创建 worktree 时用**软链接**
   把主仓库的这类文件指进 worktree（单点数据源、常新、零 IO）。

所有 worktree 一律建在 WORKTREE_DIR（paths.py）下；git 命令在仓库根 ROOT_DIR 执行。
"""

import os
import re
import json
import time
import fnmatch
import subprocess
from pathlib import Path

from paths import WORKTREE_DIR, ROOT_DIR

# worktree 名称合法性正则：仅允许字母/数字/点/下划线/连字符，长度 1-64
VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

# ───────────────────────────────────────────────────────────────
#  技术栈 → 软链供给项 注册表（可扩展）
#  每项: (stack 标签, [检测点文件/glob], [要软链进 worktree 的顶层相对项], 说明)
#  - 检测：在仓库根用 fnmatch 匹配检测点文件（支持 *.csproj、*.sh 等通配），
#    命中该栈即采纳其供给项（多个栈命中取并集）。
#  - 软链项必须是【单层安全名】（符合 _source_ok），不得含 '/'。
#  - 依赖缓存在用户全局目录（~/.m2、~/.gradle、~/.nuget、GOPATH、cpan、R 库等）
#    的栈，供给项为空——检测命中后仅基线 .env 会被链入，无需软链仓库根级项。
# ───────────────────────────────────────────────────────────────
STACK_PROVISIONS = [
    # ── Python ──
    ("python", ["requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
                "setup.py", "setup.cfg", "tox.ini", ".python-version",
                "poetry.lock", "uv.lock"], [".venv", ".pixi"], "虚拟环境"),
    # ── Node / JS / TS（涵盖前端框架、React Native、NestJS 等，以 package.json 为锚）──
    ("node/typescript", ["package.json", "package-lock.json", "yarn.lock",
                         "pnpm-lock.yaml", "bun.lockb", ".npmrc", ".yarn",
                         "lerna.json", "turbo.json", "nest-cli.json"],
     ["node_modules", ".npmrc", ".yarnrc.yml", ".pnp.cjs", ".pnp.js", ".yarn"],
     "npm/yarn/pnpm/bun 依赖与配置"),
    # ── Go（GOPATH/pkg 为全局缓存，无需软链）──
    ("go", ["go.mod"], ["vendor"], "vendor 模式依赖目录"),
    # ── Rust ──
    ("rust", ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml"], ["target"], "target 构建缓存"),
    # ── Java：Maven（依赖在 ~/.m2 全局；.mvn 为 Maven wrapper 与仓库级配置）──
    ("java-maven", ["pom.xml", ".mvn"], [".mvn"], "Maven wrapper/配置"),
    # ── Java/Kotlin/Scala 等：Gradle（依赖在 ~/.gradle 全局；.gradle 为项目级缓存）──
    ("java-gradle", ["build.gradle", "build.gradle.kts", "settings.gradle",
                     "settings.gradle.kts", "gradle.properties",
                     "gradle/libs.versions.toml"], [".gradle"], "Gradle 项目级缓存"),
    # ── C / C++：CMake / Make / Meson / Autotools ──
    ("c-cpp", ["CMakeLists.txt", "*.cmake", "Makefile", "makefile", "meson.build",
               "configure.ac", "CMakePresets.json"],
     ["build", "cmake-build-debug", "cmake-build-release", "bd"], "常见构建输出目录"),
    # ── C/C++：Conan / vcpkg 依赖管理 ──
    ("c-cpp-conan-vcpkg", ["conanfile.txt", "conanfile.py", "vcpkg.json",
                           "vcpkg-configuration.json"],
     ["vcpkg_installed", ".conan"], "conan/vcpkg 仓库级依赖"),
    # ── Swift / Xcode / Objective-C ──
    ("swift", ["Package.swift", "Podfile", "Podfile.lock", "*.xcodeproj",
               "*.xcworkspace"], ["Pods", ".build"], "CocoaPods/SPM 依赖"),
    # ── Ruby / Rails ──
    ("ruby", ["Gemfile", "Gemfile.lock", "Rakefile", "*.gemspec", "config.ru"],
     [".bundle"], "Bundler 配置"),
    # ── PHP：Composer ──
    ("php", ["composer.json", "composer.lock"], ["vendor"], "Composer 依赖目录"),
    # ── PowerShell 脚本 ──
    ("powershell", ["*.ps1", "*.psm1", "*.psd1"], [], "脚本无需软链，仅基线 .env"),
    # ── Shell / Bash / Zsh 脚本 ──
    ("shell-script", ["*.sh", "*.bash", "*.zsh", ".zshrc", ".bashrc"], [], "脚本无需软链，仅基线 .env"),
    # ── .NET / C# / F# / VB.NET：NuGet ──
    ("dotnet", ["*.csproj", "*.fsproj", "*.vbproj", "*.sln", "global.json",
                "Directory.Build.props", "paket.dependencies"], ["packages"],
     "NuGet 仓库级 packages 目录"),
    # ── Dart / Flutter ──
    ("dart-flutter", ["pubspec.yaml", "pubspec.lock"], [".dart_tool"], "Dart 本地工具缓存"),
    # ── Elixir / Mix ──
    ("elixir", ["mix.exs", "mix.lock"], ["_build", "deps"], "Elixir 构建/依赖"),
    # ── Erlang：rebar3 / erlang.mk ──
    ("erlang", ["rebar.config", "erlang.mk"], ["_build"], "rebar3 构建缓存"),
    # ── Haskell：Cabal / Stack ──
    ("haskell", ["*.cabal", "cabal.project", "stack.yaml", "stack.yaml.lock"],
     [".stack-work", ".cabal-sandbox"], "Stack/Cabal 构建缓存"),
    # ── Clojure ──
    ("clojure", ["deps.edn", "project.clj", "bb.edn"], [".cpcache"], "CLI 缓存"),
    # ── Scala ──
    ("scala", ["build.sbt", "*.sc"], [".bsp", ".bloop", ".metals"], "sbt/BSP/Metals 工具缓存"),
    # ── Groovy ──
    ("groovy", ["build.groovy", "Jenkinsfile"], [], "脚本无需软链"),
    # ── Perl ──
    ("perl", ["cpanfile", "Makefile.PL", "Build.PL", "*.pm"], [], "依赖在全局 cpan，无需软链"),
    # ── Lua：LuaRocks ──
    ("lua", ["*.rockspec", "rockspec"], ["lua_modules"], "LuaRocks 仓库级模块"),
    # ── R ──
    ("r", ["DESCRIPTION", "*.Rproj"], [], "依赖在全局 R 库，无需软链"),
    # ── Julia ──
    ("julia", ["Project.toml"], [], "依赖在 ~/.julia 全局，无需软链"),
    # ── D：dub ──
    ("d", ["dub.json", "dub.sdl"], [], "依赖在全局 ~/.dub，无需软链"),
    # ── Crystal ──
    ("crystal", ["shard.yml"], ["lib", "_cache"], "shards 仓库级依赖"),
    # ── Nim ──
    ("nim", ["*.nimble"], ["nimcache"], "Nim 构建缓存"),
    # ── Zig ──
    ("zig", ["build.zig", "build.zig.zon"], [".zig-cache"], "Zig 构建缓存"),
    # ── OCaml：opam / esy / dune ──
    ("ocaml", ["dune-project", "*.opam", "esy.json"], ["_opam", "_build"], "opam/esy 本地依赖"),
    # ── Terraform ──
    ("terraform", ["*.tf", "*.tfvars", ".terraform.lock.hcl"], [".terraform"],
     "Terraform 提供方缓存"),
    # ── Docker ──
    ("docker", ["Dockerfile", "docker-compose.yml", "compose.yaml"],
     [], "镜像/构建产物为全局，无需软链"),
]

# 基线供给：几乎所有项目都要的配置文件（自动检测模式下恒软链）
_BASELINE_PROVISIONS = [".env"]

# 已知栈名（用于校验 create(stack=...) 入参）
STACK_NAMES = {label for label, _, _, _ in STACK_PROVISIONS}


class WorktreeManager:
    """管理 git worktree 隔离工作区的生命周期。

    职责（纯工作区，不感知任务 / agent）：
    - create：校验名称 → git worktree add → 运行时供给（软链 .venv/.env）→ 记事件
    - list_all / keep：查询与保留
    - remove：带未提交/未推送安全检查的移除
    - resolve / exists：把 worktree 名称解析为路径，供子智能体/队友做 cwd
    - 事件日志 events.jsonl 落 WORKTREE_DIR 下，供审计/复盘
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        repo_dir: Path | None = None,
        symlink_items: list[str] | None = None,
        git_timeout: int | None = None,
    ):
        """
        参数（默认值优先读 .env，其次硬编码默认，符合 AGENTS.md 可调参数约定）：
            base_dir:     所有 worktree 的挂载根（默认 WORKTREE_DIR）
            repo_dir:     执行 git 命令的仓库根（默认 ROOT_DIR）
            symlink_items:创建后要软链进 worktree 的运行时源相对名列表
                          （默认 .env 的 WORKTREE_SYMLINKS，逗号分隔；缺省 ['.venv', '.env']）
            git_timeout:  git 命令超时秒数（默认 .env 的 WORKTREE_GIT_TIMEOUT；缺省 30）
        """
        self.base_dir = base_dir if base_dir is not None else WORKTREE_DIR
        self.repo_dir = repo_dir if repo_dir is not None else ROOT_DIR
        # git worktree 必须在仓库根执行；ROOT_DIR=Path.cwd() 会随启动目录漂移，
        # 故用 `git rev-parse --show-toplevel` 解析真实仓库根，保证 .venv/.env
        # 等运行时供给源始终定位到仓库根而非启动目录。
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.repo_dir, capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                self.repo_dir = Path(r.stdout.strip())
        except Exception:
            pass
        if symlink_items is None:
            raw = os.environ.get("WORKTREE_SYMLINKS")
            # 显式配置（.env 的 WORKTREE_SYMLINKS 或构造参数）＝完整清单，
            # 交给 create 时走「显式模式」；缺省 None → 每次 create 时按技术栈自动检测。
            symlink_items = ([s.strip() for s in raw.split(",") if s.strip()]
                             if raw else None)
        self.symlink_items = symlink_items
        if git_timeout is None:
            git_timeout = int(os.environ.get("WORKTREE_GIT_TIMEOUT", "30"))
        self.git_timeout = git_timeout

        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.events_file = self.base_dir / "events.jsonl"

    # ═══════════════════════════════════════════════════════════
    #  内部工具
    # ═══════════════════════════════════════════════════════════

    def _wt_path(self, name: str) -> Path:
        """返回 worktree 名称对应的磁盘路径（base_dir/name）。"""
        return self.base_dir / name

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        """去重并保持元素先后顺序。"""
        return list(dict.fromkeys(items))

    def _detect_provisions(self) -> tuple[str | None, list[str]]:
        """按仓库根检测文件自动识别技术栈，返回 (stack_label, 供给项)。

        - 用 fnmatch 对仓库根顶层条目匹配各栈检测点（支持 *.csproj、*.sh 等通配）；
        - 命中多个栈时供给项取并集，label 用「, 」连接；
        - 未命中任何栈返回 (None, [])，由调用方用基线 .env 兜底。
        """
        try:
            names = [e.name for e in self.repo_dir.iterdir()]
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            names = []
        matched, items = [], []
        for label, detectors, provisions, _note in STACK_PROVISIONS:
            if any(fnmatch.fnmatch(n, pat) for n in names for pat in detectors):
                matched.append(label)
                items.extend(provisions)
        if not matched:
            return None, []
        return ", ".join(matched), self._dedupe(items)

    def _resolve_provision_items(self, stack: str | None) -> tuple[list[str], str | None]:
        """根据 stack 参数 / 显式配置 / 自动检测，解析本次要软链的供给项。

        返回 (items, stack_label)；items 已含基线 .env（显式配置模式除外）。
        """
        if stack:
            items = []
            for label, _det, provisions, _note in STACK_PROVISIONS:
                if label == stack:
                    items.extend(provisions)
            if not items and stack not in STACK_NAMES:
                raise ValueError(f"Unknown stack '{stack}'. "
                                 f"Known: {', '.join(sorted(STACK_NAMES))}")
            return self._dedupe(_BASELINE_PROVISIONS + items), stack
        if self.symlink_items is not None:
            # 显式模式：完整清单直接使用，不自动检测、不加基线
            return self.symlink_items, None
        label, detected = self._detect_provisions()
        return self._dedupe(_BASELINE_PROVISIONS + detected), label

    def validate_name(self, name: str) -> str | None:
        """校验 worktree 名称。非法时返回错误信息，合法返回 None。"""
        if not name:
            return "Worktree name cannot be empty"
        if name == "." or name == "..":
            return f"'{name}' is not a valid worktree name"
        if not VALID_WT_NAME.match(name):
            return (f"Invalid worktree name '{name}': "
                    "only letters, digits, dots, underscores, dashes (1-64 chars)")
        return None

    def _run_git(self, args: list[str]) -> tuple[bool, str]:
        """在仓库根执行 git 命令，返回 (是否成功, 输出)。"""
        try:
            r = subprocess.run(
                ["git"] + args, cwd=self.repo_dir,
                capture_output=True, text=True, timeout=self.git_timeout,
            )
            out = (r.stdout + r.stderr).strip()
            out = out[:5000] if out else "(no output)"
            return r.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, "Error: git timeout"

    def log_event(self, event_type: str, worktree_name: str, **extra) -> None:
        """将生命周期事件（create/remove/keep/provision）追加写入 events.jsonl。"""
        event = {"type": event_type, "worktree": worktree_name,
                 "ts": time.time(), **extra}
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.events_file, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _count_worktree_changes(self, path: Path) -> tuple[int, int]:
        """统计某个 worktree 内的未提交文件数与未推送提交数。

        返回 (files, commits)；统计出错时返回 (-1, -1) 表示无法判定。
        """
        try:
            r1 = subprocess.run(["git", "status", "--porcelain"],
                                cwd=path, capture_output=True, text=True,
                                timeout=10)
            files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
            r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                                cwd=path, capture_output=True, text=True,
                                timeout=10)
            commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
            return files, commits
        except Exception:
            return -1, -1

    def _source_ok(self, item: str) -> bool:
        """校验软链源相对名是否安全：非空、非 . / ..、无路径穿越、仅安全字符。

        仅用单层相对名（如 .venv / .env），杜绝把任意主机路径链进 worktree。
        """
        if not item or item == "." or item == "..":
            return False
        if "/" in item or "\\" in item:
            return False
        return bool(VALID_WT_NAME.match(item))

    def _provision_runtime(self, wt_path: Path, items: list[str]) -> list[str]:
        """运行时供给：把主仓库的运行时源（.venv/.env 等）软链进 worktree。

        `items` 为本次要软链的顶层相对名清单（由 create 决定：显式配置 /
        技术栈自动检测 / stack 强制指定）。源不存在或目标已存在时跳过，绝不覆盖。

        返回值：本工作区本次实际建立的软链列表（用于写入事件日志）。
        """
        linked = []
        for item in items:
            # 安全校验：仅相对安全名，并且不在 base_dir 内作为源（避免自指）
            if not self._source_ok(item):
                continue
            src = self.repo_dir / item
            dst = wt_path / item
            # 源不存在、或目标已存在（可能是跟踪文件/用户文件）时跳过，绝不覆盖
            if not src.exists() or dst.exists() or dst.is_symlink():
                continue
            try:
                os.symlink(str(src), str(dst))
                linked.append(item)
            except OSError:
                continue
        return linked

    # ═══════════════════════════════════════════════════════════
    #  公开接口：生命周期
    # ═══════════════════════════════════════════════════════════

    def exists(self, name: str) -> bool:
        """判断 worktree 是否已创建（目录是否存在且名称合法）。"""
        return self._wt_path(name).exists()

    def resolve(self, name: str) -> Path:
        """把 worktree 名称解析为路径（base_dir/name）。

        不做存在性校验，由调用方（如派发子智能体/队友前）自行判空。
        """
        return self._wt_path(name)

    def create(self, name: str, base: str = "HEAD", stack: str | None = None) -> str:
        """创建带专属分支的 git worktree，可选地指定基线（默认 HEAD）。

        命令 `git worktree add <路径> -b wt/<name> <base>`：
        - 在 WORKTREE_DIR/<name> 检出一份工作副本；
        - 从 <base> 新建分支 wt/<name>，实现与主目录的隔离。
        创建成功后做运行时供给（软链 .venv/.env 等），供 agent 在内部运行/测试。

        stack：可选，强制指定技术栈标签（须命中 STACK_PROVISIONS）；缺省时
        - 若显式配置了 WORKTREE_SYMLINKS → 走显式清单；
        - 否则自动检测仓库根技术栈，软链对应运行时。
        """
        err = self.validate_name(name)
        if err:
            return f"Error: {err}"
        path = self._wt_path(name)
        if path.exists():
            return f"Worktree '{name}' already exists at {path}"
        try:
            items, stack_label = self._resolve_provision_items(stack)
        except ValueError as e:
            return f"Error: {e}"
        ok, result = self._run_git(
            ["worktree", "add", str(path), "-b", f"wt/{name}", base]
        )
        if not ok:
            return f"Git error: {result}"
        linked = self._provision_runtime(path, items)
        self.log_event("create", name, provision=linked, stack=stack_label)
        provision_note = (f", provisioned: {', '.join(linked)}" if linked else ", no provision")
        stack_note = f", stack: {stack_label}" if stack_label else ""
        print(f"  \033[33m[worktree] created: {name} at {path}"
              f" (branch: wt/{name}){stack_note}{provision_note}\033[0m")
        return (f"Worktree '{name}' created at {path} (branch: wt/{name})"
                f"{stack_note}{provision_note}")

    def list_all(self) -> str:
        """列出所有 worktree：name + 分支 + 路径 + 供给侧概览。"""
        entries = []
        for p in sorted(self.base_dir.glob("*/")):
            if not p.is_dir():
                continue
            # 真正的 git worktree 目录内必有 `.git`（文件或目录）作为标记；
            # 以此过滤掉共享目录、事件文件等非 worktree 项
            if (p / ".git").exists():
                entries.append(f"  {p.name}: {p}")
        if not entries:
            return "No worktrees."
        lines = [f"Worktrees under {self.base_dir}:"]
        lines.extend(entries)
        return "\n".join(lines)

    def remove(self, name: str, discard_changes: bool = False) -> str:
        """移除 worktree。除非 discard_changes=True，否则拒绝移除有未提交变更的 worktree。"""
        err = self.validate_name(name)
        if err:
            return err
        path = self._wt_path(name)
        if not path.exists():
            return f"Worktree '{name}' not found"
        if not discard_changes:
            files, commits = self._count_worktree_changes(path)
            if files < 0:
                return (f"Cannot verify worktree '{name}' status. "
                        "Use discard_changes=true to force removal.")
            if files > 0 or commits > 0:
                return (f"Worktree '{name}' has {files} uncommitted file(s) "
                        f"and {commits} unpushed commit(s). "
                        "Use discard_changes=true to force removal, "
                        "or keep_worktree to preserve for review.")
        ok1, _ = self._run_git(["worktree", "remove", str(path), "--force"])
        if not ok1:
            return f"Failed to remove worktree directory for '{name}'"
        self._run_git(["branch", "-D", f"wt/{name}"])
        self.log_event("remove", name)
        print(f"  \033[33m[worktree] removed: {name}\033[0m")
        return f"Worktree '{name}' removed"

    def keep(self, name: str) -> str:
        """保留 worktree 供人工复核，分支不会删除。"""
        err = self.validate_name(name)
        if err:
            return err
        self.log_event("keep", name)
        print(f"  \033[36m[worktree] kept: {name}\033[0m")
        return f"Worktree '{name}' kept for review (branch: wt/{name})"