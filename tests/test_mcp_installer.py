#!/usr/bin/env python3
"""MCP 本地包安装器（agents/mcp_installer.py）回归测试 —— 2026-10-07。

守护七件事（见 docs/frontend/23 §本地安装）：

1. **规格白名单是唯一入口**：`parse_spec` 只放行 `包名@精确版本`。`latest`、`^`/`~`、
   `1.x`、`1.0`、`git+https:`、`https://…tgz`、`file:`、`github:`、`npm:alias`、
   `../../x`、`--prefix=/etc` 全部必须被拒 —— 这些**都是合法的 npm 语法**，
   正因如此才必须显式拒，不能指望 npm 自己报错。
2. **包名先于任何 npm 调用被校验**：`plan()` 在缺版本时会先发一次 `npm view
   <name>@latest`，那一刻名字还没过 SPEC_RE。若不先过 `parse_name`，
   `--prefix=/etc` 会被 npm 当成命令行**选项**吃掉（argv 数组挡不住 `-` 开头的参数）
   —— 这是本模块唯一的真实注入点。
3. **安装参数强制项**：`--ignore-scripts` 与 `--registry` 必须在 argv 里；
   `allow_scripts=True` 时前者才消失。这两条是"生命周期脚本 RCE"与"静默换源"
   的唯二防线，掉了不会有任何报错。
4. **装后校验**：`package-lock.json` 的哈希与 registry 声明不符 → 安装判定失败
   （供应链篡改 / 版本在两次请求之间被重发）。
5. **bin 逃逸**：包 `package.json` 的 `bin` 指向安装目录之外时必须拒
   （恶意包写 `../../../../bin/sh` 是真实手法，只查"链接存在"挡不住）。
6. **失败不留垃圾**：安装失败要把半成品目录与哨兵一起清掉。
7. **列表 / 卸载 / 复核的路径安全**：带哨兵的目录显示为 `incomplete`；卸载只接受
   单段 slug、拒绝安装根；复核能发现哈希对不上。

⚠️ 全程用临时目录（`MCP_PKGS_DIR` 被 patch），**绝不碰真实的 `~/.aigent`**；
**绝不真的执行 npm** —— 所有子进程调用走注入的假 runner。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import mcp_installer as mi  # noqa: E402

VERSION = "1.2.3"
INTEGRITY = "sha512-AAAA1111BBBB2222"


def _split_spec(spec: str) -> tuple[str, str]:
    """`@scope/pkg@1.2.3` / `pkg@latest` → `(name, version)`（假的 registry 用）。"""
    if spec.startswith("@"):
        name, _, ver = spec[1:].rpartition("@")
        return "@" + name, ver
    name, _, ver = spec.rpartition("@")
    return name, ver


class FakeNpm:
    """假 npm：只做两件事 —— 返回可预测的 `view` JSON，和**模拟装出来的目录结构**。

    `install()` 的装后校验读的是真实文件（`package-lock.json` + 包的
    `node_modules/<name>/package.json` + `.bin/`），所以假 runner 必须真的把这些
    造出来，否则测不到校验逻辑。`lock_integrity` / `bin_target` / `omit_bin_link`
    是可调旋钮，用来构造"哈希不符"、"bin 逃逸"、"声明了 bin 却没有可执行文件"。

    ⚠️ argv 形态是 `[npm, --registry, <url>, <子命令>, <spec>, ...]` —— 别按下标
    硬编码，用 `index()` 找子命令（两个占位参数把下标全部推后了两位）。
    """

    def __init__(self, *, version=VERSION, integrity=INTEGRITY,
                 lock_integrity=None, bin_name="demo-server",
                 bin_target=None, omit_bin_link=False, scripts=None, dep_count=7,
                 dry_run_fail=False, install_rc=0, install_err="", view_error=""):
        self.version = version
        self.integrity = integrity
        self.lock_integrity = integrity if lock_integrity is None else lock_integrity
        self.bin_name = bin_name
        self.bin_target = bin_target
        self.omit_bin_link = omit_bin_link
        self.scripts = scripts or {}
        self.dep_count = dep_count
        self.dry_run_fail = dry_run_fail
        self.install_rc = install_rc
        self.install_err = install_err
        self.view_error = view_error
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if "view" in argv:
            return self._view(argv[argv.index("view") + 1])
        if "install" in argv:
            return self._install(argv, argv[argv.index("install") + 1])
        return 1, "", f"unexpected argv: {argv}"

    def _view(self, spec):
        if self.view_error:
            return 1, "", self.view_error
        name, _ = _split_spec(spec)
        return 0, json.dumps({
            "name": name,
            "version": self.version,
            "description": "测试包",
            "bin": {self.bin_name: "bin/cli.js"},
            "scripts": self.scripts,
            "dependencies": {"left-pad": "^1.0.0"},
            "dist": {"integrity": self.integrity, "shasum": "abc123",
                     "tarball": "https://registry.npmjs.org/x/-/x.tgz"},
        }), ""

    def _install(self, argv, spec):
        if "--dry-run" in argv:
            if self.dry_run_fail:
                return 1, "", "network hiccup"
            return 0, json.dumps({"added": self.dep_count}), ""
        if self.install_rc != 0:
            return self.install_rc, "", self.install_err or "boom"
        prefix = Path(argv[argv.index("--prefix") + 1])
        name, _ = _split_spec(spec)
        pkg = prefix / "node_modules" / name
        (pkg / "bin").mkdir(parents=True, exist_ok=True)
        exe = pkg / "bin" / "cli.js"
        exe.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        os.chmod(exe, 0o755)
        (pkg / "package.json").write_text(json.dumps({
            "name": name, "version": self.version,
            "bin": {self.bin_name: "bin/cli.js"}, "scripts": self.scripts,
        }), encoding="utf-8")
        if not self.omit_bin_link:
            bin_dir = prefix / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            # 相对链接：`.bin/x` → `../<name>/bin/cli.js`（npm 的真实布局）
            (bin_dir / self.bin_name).symlink_to(
                self.bin_target or f"../{name}/bin/cli.js")
        (prefix / "package-lock.json").write_text(json.dumps({
            "name": "aigent-mcp-pkg", "lockfileVersion": 3,
            # ⚠️ key 用**真机实测的形态**（`--prefix` 会被 npm 当 global prefix 处理，
            # 于是它按相对自己的路径记账）—— 写成 `node_modules/<name>` 会让测试
            # 通过而线上永远取空，那正是修复前实况。
            "packages": {f"../..{prefix}/node_modules/{name}": {
                "version": self.version, "integrity": self.lock_integrity}},
        }), encoding="utf-8")
        return 0, "added 5 packages in 2s", ""


class InstallerTestBase(unittest.TestCase):
    """把 `MCP_PKGS_DIR` 指向临时目录，并清掉可能影响结果的 registry 配置。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aigent-mcp-test-"))
        self.pkgs = self.tmp / "pkgs"
        self.pkgs.mkdir()
        self._patches = [
            mock.patch.object(mi, "MCP_PKGS_DIR", self.pkgs),
            mock.patch.dict(os.environ, {"MCP_NPM_REGISTRY": mi.DEFAULT_REGISTRY}),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for p in reversed(self._patches):
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
#  1 / 2：规格与包名白名单
# ═══════════════════════════════════════════════════════════════════════

class TestSpecWhitelist(unittest.TestCase):

    BAD_SPECS = [
        "", "   ", "pkg", "pkg@", "pkg@latest", "pkg@next",
        "pkg@^1.0.0", "pkg@~1.2.3", "pkg@1.x", "pkg@1.0", "pkg@>=1",
        "pkg@*", "pkg@1.0.0 || 2.0.0",
        "git+https://github.com/a/b.git",
        "https://example.com/x.tgz", "http://example.com/x.tgz",
        "file:../local", "file:/etc/passwd", "github:user/repo",
        "npm:alias@1.0.0", "link:../x", "workspace:*",
        "../../etc/passwd@1.0.0", "../../../x", "--prefix=/etc@1.0.0",
        "-g@1.0.0", "@/x@1.0.0", "@scope/@1.0.0",
        "a/b@1.0.0", "a/b/c@1.0.0", "@a/b/c@1.0.0",
        "pkg@1.0.0; rm -rf /", "pkg@1.0.0 && curl evil",
        "pkg@1.0.0\n--prefix=/etc", "pkg@1.0.0`id`",
        ".hidden@1.0.0", "_under@1.0.0", "@scope/.hidden@1.0.0",
    ]

    GOOD_SPECS = {
        "pkg@1.0.0": ("pkg", "1.0.0"),
        "@scope/pkg@1.2.3": ("@scope/pkg", "1.2.3"),
        "mcp-server-time@2024.12.6": ("mcp-server-time", "2024.12.6"),
        "pkg@1.0.0-beta.1": ("pkg", "1.0.0-beta.1"),
        "pkg@1.0.0-rc.1.2": ("pkg", "1.0.0-rc.1.2"),
        "pkg@1.0.0+build.5": ("pkg", "1.0.0+build.5"),
        "@a/b-c.d_e@0.0.1": ("@a/b-c.d_e", "0.0.1"),
    }

    def test_bad_specs_rejected(self):
        for spec in self.BAD_SPECS:
            with self.subTest(spec=spec):
                with self.assertRaises(mi.InstallError):
                    mi.parse_spec(spec)

    def test_good_specs_accepted(self):
        for spec, want in self.GOOD_SPECS.items():
            with self.subTest(spec=spec):
                self.assertEqual(mi.parse_spec(spec), want)

    def test_name_only_rejects_option_injection(self):
        """`plan()` 在补问 latest 之前只校验包名 —— 那一刻的 <name> 直接进 argv。"""
        for bad in ["--prefix=/etc", "-g", "", "  ", "a/b", "../x", "--registry=x",
                    "pkg@1.0.0"]:
            with self.subTest(name=bad):
                with self.assertRaises(mi.InstallError):
                    mi.parse_name(bad)
        for good in ["pkg", "@scope/pkg", "mcp-server-time", "@a/b_c.d-e"]:
            with self.subTest(name=good):
                self.assertEqual(mi.parse_name(good), good)

    def test_overlong_name_rejected(self):
        with self.assertRaises(mi.InstallError):
            mi.parse_name("a" * (mi.NAME_MAX + 1))


class TestSlugAndDir(unittest.TestCase):

    def test_slug_is_single_segment(self):
        for name, version in [("pkg", "1.0.0"), ("@scope/pkg", "1.2.3"),
                              ("@a/b", "0.0.1-beta.1")]:
            slug = mi.pkg_slug(name, version)
            with self.subTest(slug=slug):
                self.assertEqual(Path(slug).name, slug)
                self.assertNotIn("/", slug)

    def test_slug_shape(self):
        self.assertEqual(mi.pkg_slug("@scope/pkg", "1.2.3"), "scope__pkg@1.2.3")
        self.assertEqual(mi.pkg_slug("pkg", "1.2.3"), "pkg@1.2.3")

    def test_dir_always_under_pkgs_root(self):
        with mock.patch.object(mi, "MCP_PKGS_DIR", Path("/tmp/pkgs")):
            for name in ["pkg", "@scope/pkg", "../../evil"]:
                try:
                    d = mi.pkg_dir_for(name, "1.0.0")
                except mi.InstallError:
                    continue          # 非法名被拒也是正确行为
                self.assertTrue(str(d).startswith("/tmp/pkgs/"), d)


# ═══════════════════════════════════════════════════════════════════════
#  plan：解析真值（不落盘）
# ═══════════════════════════════════════════════════════════════════════

class TestPlan(InstallerTestBase):

    def test_plan_reports_truth_and_writes_nothing(self):
        fake = FakeNpm(scripts={"postinstall": "node build.js"})
        res = mi.plan("demo-server", VERSION, runner=fake)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["spec"], f"demo-server@{VERSION}")
        self.assertEqual(res["integrity"], INTEGRITY)
        self.assertEqual(res["registry"], mi.DEFAULT_REGISTRY)
        self.assertEqual(res["direct_dep_count"], 1)
        self.assertEqual(res["dep_count"], 7)
        self.assertEqual(res["bins"], ["demo-server"])
        self.assertTrue(res["has_scripts"])
        self.assertEqual(res["install_hooks"], {"postinstall": "node build.js"})
        # 纯解析：一个文件都不许落盘
        self.assertEqual(list(self.pkgs.iterdir()), [])

    def test_plan_pins_latest_when_version_missing(self):
        fake = FakeNpm()
        res = mi.plan("demo-server", "", runner=fake)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["version"], VERSION)
        self.assertTrue(res["pinned_from_latest"])
        self.assertTrue(any("latest" in w for w in res["warnings"]))
        # 必须**钉死**：`@latest` 只允许出现在"问版本"那一次上；之后所有触达 registry
        # 的调用（view / dry-run / install）都只能是精确版本。
        latest_calls = [c for c in fake.calls if any("demo-server@latest" in a for a in c)]
        pinned_calls = [c for c in fake.calls
                        if any(f"demo-server@{VERSION}" in a for a in c)]
        self.assertEqual(len(latest_calls), 1, "只允许一次 latest 查询（用于钉死版本）")
        self.assertTrue(pinned_calls, "钉死之后必须都走精确版本")
        self.assertEqual(res["spec"], f"demo-server@{VERSION}")

    def test_plan_rejects_injection_before_calling_npm(self):
        fake = FakeNpm()
        res = mi.plan("--prefix=/etc", "", runner=fake)
        self.assertFalse(res["ok"])
        self.assertEqual(fake.calls, [], "非法包名绝不能触达 npm")

    def test_plan_reports_missing_version_without_crashing(self):
        fake = FakeNpm(view_error="npm error code E404\nnpm error 404 Not Found")
        res = mi.plan("@nope/definitely-missing", VERSION, runner=fake)
        self.assertFalse(res["ok"])
        self.assertIn("没有这个包或版本", res["error"])

    def test_plan_survives_npm_absent(self):
        def boom(argv, cwd, timeout):
            raise FileNotFoundError("npm: command not found")
        res = mi.plan("demo-server", VERSION, runner=boom)
        self.assertFalse(res["ok"])
        self.assertIn("找不到 npm", res["error"])

    def test_plan_survives_timeout(self):
        import subprocess

        def slow(argv, cwd, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        res = mi.plan("demo-server", VERSION, runner=slow)
        self.assertFalse(res["ok"])
        self.assertIn("超时", res["error"])

    def test_dep_count_degrades_to_none(self):
        """dry-run 失败不能拖垮整个计划 —— 显示"未知"好过编一个数字。"""
        fake = FakeNpm(dry_run_fail=True)
        res = mi.plan("demo-server", VERSION, runner=fake)
        self.assertTrue(res["ok"], res["error"])
        self.assertIsNone(res["dep_count"])


# ═══════════════════════════════════════════════════════════════════════
#  install：强制参数 + 装后校验 + bin 逃逸 + 失败清理
# ═══════════════════════════════════════════════════════════════════════

class TestInstall(InstallerTestBase):

    def _argv_for(self, fake, kind="install"):
        """取那条真实的子命令调用（排除 `--dry-run`）。用 `in` 找子命令，不按下标。"""
        for call in fake.calls:
            if kind in call and "--dry-run" not in call:
                return call
        return []

    def test_install_forces_safety_flags(self):
        fake = FakeNpm()
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertTrue(res["ok"], res["error"])
        argv = self._argv_for(fake)
        self.assertTrue(argv, "应当有一次真实安装调用")
        self.assertIn("--ignore-scripts", argv)
        self.assertIn("--registry", argv)
        self.assertEqual(argv[argv.index("--registry") + 1], mi.DEFAULT_REGISTRY)
        self.assertIn("--prefix", argv)
        self.assertIn("--no-audit", argv)
        self.assertIn("--no-fund", argv)
        self.assertEqual(argv[argv.index("--prefix") + 1], res["dir"])

    def test_install_drops_ignore_scripts_only_when_explicitly_allowed(self):
        fake = FakeNpm()
        res = mi.install("demo-server", VERSION, allow_scripts=True, runner=fake)
        self.assertTrue(res["ok"], res["error"])
        argv = self._argv_for(fake)
        self.assertTrue(argv, "应当有一次真实安装调用")
        self.assertNotIn("--ignore-scripts", argv)
        self.assertTrue(res["scripts_allowed"])

    def test_install_writes_config_ready_command(self):
        fake = FakeNpm()
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertTrue(res["ok"], res["error"])
        command = Path(res["command"])
        # command 是 `.bin/<name>` 符号链接**解析后**的真实路径 —— 存储解析结果
        # 是为了让"可执行文件还在不在安装目录里"这个复核恒可做（见 verify）。
        self.assertTrue(command.exists(), command)
        self.assertEqual(command.name, "cli.js")
        self.assertTrue(str(command).startswith(str(Path(res["dir"]).resolve())))
        self.assertTrue(os.access(command, os.X_OK))
        # 哨兵必须已被清掉、元数据必须已落盘
        d = Path(res["dir"])
        self.assertFalse((d / mi.SENTINEL_FILENAME).exists())
        meta = json.loads((d / mi.META_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(meta["spec"], f"demo-server@{VERSION}")
        self.assertEqual(meta["integrity"], INTEGRITY)
        self.assertEqual(meta["command"], res["command"])

    def test_install_detects_integrity_mismatch(self):
        fake = FakeNpm(lock_integrity="sha512-TAMPERED")
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertFalse(res["ok"])
        self.assertIn("完整性校验不符", res["error"])
        # 失败必须连目录一起清掉，不留半成品
        self.assertFalse((self.pkgs / f"demo-server@{VERSION}").exists())

    def test_install_rejects_bin_escaping_target(self):
        fake = FakeNpm(bin_name="evil", bin_target="/bin/sh")
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertFalse(res["ok"])
        self.assertIn("越界", res["error"])

    def test_install_failure_cleans_half_done_dir(self):
        fake = FakeNpm(install_rc=1, install_err="EACCES boom")
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertFalse(res["ok"])
        self.assertIn("安装失败", res["error"])
        self.assertEqual(list(self.pkgs.iterdir()), [])

    def test_install_rejects_missing_bin(self):
        """包声明了 bin，但 `.bin/` 下没有对应文件（依赖被跳过的安装期脚本）。"""
        fake = FakeNpm(bin_name="declared-but-absent", omit_bin_link=True)
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertFalse(res["ok"])
        self.assertIn("没有对应的可执行文件", res["error"])

    def test_install_reuses_existing_without_second_download(self):
        fake = FakeNpm()
        first = mi.install("demo-server", VERSION, runner=fake)
        self.assertTrue(first["ok"], first["error"])
        n_calls = len(fake.calls)
        second = mi.install("demo-server", VERSION, runner=fake)
        self.assertTrue(second["ok"], second["error"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(fake.calls), n_calls, "复用不该再调 npm")
        self.assertEqual(second["command"], first["command"])

    def test_install_refuses_slug_collision(self):
        """slug 是把 `/` 折成 `__` 得来的，撞车必须响亮失败，不能张冠李戴。"""
        d = self.pkgs / mi.pkg_slug("@a/b", VERSION)
        d.mkdir(parents=True)
        (d / mi.META_FILENAME).write_text(json.dumps({
            "name": "@a/b", "version": VERSION, "command": "/nonexistent/cmd",
        }), encoding="utf-8")
        fake = FakeNpm()
        res = mi.install("a__b", VERSION, runner=fake)
        self.assertFalse(res["ok"])
        self.assertIn("目录名冲突", res["error"])


# ═══════════════════════════════════════════════════════════════════════
#  list / remove / verify
# ═══════════════════════════════════════════════════════════════════════

class TestListRemoveVerify(InstallerTestBase):

    def _make_pkg(self, slug, *, command=None, sentinel=False, integrity=INTEGRITY,
                  lock_integrity=None):
        d = self.pkgs / slug
        (d / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
        exe = d / "node_modules" / ".bin" / "cli"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(exe, 0o755)
        (d / mi.META_FILENAME).write_text(json.dumps({
            "spec": f"demo@{VERSION}", "name": "demo", "version": VERSION,
            "command": command if command is not None else str(exe),
            "integrity": integrity, "installed_at": "2026-10-07T00:00:00+00:00",
            "registry": mi.DEFAULT_REGISTRY,
        }), encoding="utf-8")
        (d / "package-lock.json").write_text(json.dumps({"packages": {
            "node_modules/demo": {"integrity": lock_integrity or integrity}}}),
            encoding="utf-8")
        if sentinel:
            (d / mi.SENTINEL_FILENAME).write_text("2026-10-07T00:00:00+00:00",
                                                  encoding="utf-8")
        return d

    def test_list_marks_incomplete_and_broken(self):
        self._make_pkg("demo@" + VERSION)
        self._make_pkg("half@" + VERSION, sentinel=True)
        self._make_pkg("gone@" + VERSION, command="/nonexistent/x")
        by_slug = {p["slug"]: p for p in mi.list_packages()}
        self.assertEqual(by_slug["demo@" + VERSION]["status"], "ok")
        self.assertEqual(by_slug["half@" + VERSION]["status"], "incomplete")
        self.assertEqual(by_slug["gone@" + VERSION]["status"], "broken")
        self.assertGreater(by_slug["demo@" + VERSION]["size_bytes"], 0)

    def test_list_ignores_temp_dirs(self):
        (self.pkgs / "_resolve_tmp").mkdir()
        (self.pkgs / ".hidden").mkdir()
        self.assertEqual(mi.list_packages(), [])

    def test_remove_deletes_only_direct_child(self):
        d = self._make_pkg("demo@" + VERSION)
        res = mi.remove("demo@" + VERSION)
        self.assertTrue(res["ok"])
        self.assertFalse(d.exists())
        self.assertGreater(res["freed_bytes"], 0)

    def test_remove_rejects_escape_attempts(self):
        for bad in ["..", ".", "", "  ", "a/b", "/etc", "../outside", "x\\y"]:
            with self.subTest(slug=bad):
                with self.assertRaises(mi.InstallError):
                    mi.remove(bad)

    def test_remove_is_idempotent(self):
        res = mi.remove("never-installed@1.0.0")
        self.assertTrue(res["ok"])
        self.assertEqual(res["freed_bytes"], 0)

    def test_verify_ok_then_detects_tamper(self):
        self._make_pkg("demo@" + VERSION)
        self.assertTrue(mi.verify("demo@" + VERSION)["ok"])

        d = self._make_pkg("tampered@" + VERSION, lock_integrity="sha512-OTHER")
        res = mi.verify("tampered@" + VERSION)
        self.assertFalse(res["ok"])
        self.assertTrue(any("哈希" in e for e in res["errors"]))

    def test_verify_flags_out_of_tree_command(self):
        self._make_pkg("escapee@" + VERSION, command="/bin/sh")
        res = mi.verify("escapee@" + VERSION)
        self.assertFalse(res["ok"])
        self.assertTrue(any("安装目录外面" in e for e in res["errors"]))


class TestLockIntegrityKeyShape(InstallerTestBase):
    """`_read_lock_integrity` 必须认得 npm 的两种 key 形态。

    这是真机验证抓到的缺陷：`npm install --prefix <dir>` 产生的 key 是
    `../../private/tmp/.../node_modules/<pkg>` 而不是 `node_modules/<pkg>`，
    原先按固定 key 取 → 永远取空 → 「装后对账」静默空转（回执里还一个字都不提）。
    """

    def _lock(self, packages: dict):
        self.pkgs.mkdir(parents=True, exist_ok=True)
        (self.pkgs / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")

    def test_plain_key(self):
        self._lock({"node_modules/cowsay": {"integrity": "sha512-A"}})
        self.assertEqual(mi._read_lock_integrity(self.pkgs, "cowsay"), "sha512-A")

    def test_relative_prefixed_key(self):
        """真机形态：`--prefix` 被 npm 当 global prefix → key 带 `../..` 前缀。"""
        self._lock({"../../private/tmp/x/node_modules/cowsay": {"integrity": "sha512-B"}})
        self.assertEqual(mi._read_lock_integrity(self.pkgs, "cowsay"), "sha512-B")

    def test_scoped_name(self):
        self._lock({"../..@/x/node_modules/@scope/pkg": {"integrity": "sha512-C"}})
        self.assertEqual(mi._read_lock_integrity(self.pkgs, "@scope/pkg"), "sha512-C")

    def test_prefers_shallowest_candidate(self):
        """顶层包必须优先于某个依赖内部的同名嵌套副本。"""
        self._lock({
            "../../x/node_modules/cliui/node_modules/cowsay": {"integrity": "sha512-NESTED"},
            "../../x/node_modules/cowsay": {"integrity": "sha512-TOP"},
        })
        self.assertEqual(mi._read_lock_integrity(self.pkgs, "cowsay"), "sha512-TOP")

    def test_missing_and_broken_lock(self):
        self.assertEqual(mi._read_lock_integrity(self.pkgs, "cowsay"), "")
        (self.pkgs / "package-lock.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(mi._read_lock_integrity(self.pkgs, "cowsay"), "")

    def test_install_warns_when_reconciliation_cannot_run(self):
        """对账没跑成必须**说出来** —— 静默降级等于让人以为有这道防线。"""
        fake = FakeNpm()

        def no_integrity(argv, spec):
            rc, out, err = FakeNpm._install(fake, argv, spec)
            if rc == 0:
                prefix = Path(argv[argv.index("--prefix") + 1])
                (prefix / "package-lock.json").write_text(
                    json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
            return rc, out, err

        fake._install = no_integrity
        res = mi.install("demo-server", VERSION, runner=fake)
        self.assertTrue(res["ok"], res["error"])
        self.assertTrue(any("跳过了对账" in w for w in res["warnings"]), res["warnings"])


if __name__ == "__main__":
    unittest.main()
