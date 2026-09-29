#!/usr/bin/env python3
"""沙盒执行隔离（agents/sandbox.py）回归测试 —— 2026-09-22。

守护七件事（见 docs/frontend/20）：
1. 模板机制：默认模板落盘幂等（绝不覆盖用户已有文件）、占位符校验拒存坏模板、
   恢复默认模板；
2. 占位符替换：值占位符 + EXTRA_WRITABLE 段展开（多目录 / 空目录）；
3. 后端探测：auto 顺序（darwin→seatbelt / linux→bwrap）、SANDBOX_BACKEND 强制
   指定、开关关闭 / off 静默返回 None、已启用但无后端返回 None；
4. bwrap argv 构造：{{COMMAND}} 整行替换且保持单参数、逐行 shlex 切分；
5. ws_bridge._save_sandbox_enabled：落盘 config.json + 直接覆写 os.environ（热生效），
   且**读不出来就拒绝写**（不许拿 {} 兜底抹掉其他键）；
6. **默认模板的跨平台纯文本不变式**（2026-09-24 补）：网络/pid 隔离行在位、不遮蔽整个
   ~/.aigent、工作区读放行必须晚于敏感读拒绝；
7. Seatbelt 真机探针（darwin 且当前进程不在沙箱内才跑，否则 skip）。

**为什么第 6 条必须有**：2026-09-24 审出的两条 P0 都是"默认模板的文本写错了"
（macOS 把工作区自己读拦死、Linux 缺 `--unshare-net`），而真机探针在 CI / 本机沙箱里
跑不起来 —— 只有这层纯文本断言能拦住它们。加模板行时请同步这里。

集成验证（真实 sandbox-exec 拦截行为）：`TestSeatbeltRealProbe` 已内建；注意**在本应用
自己的会话里跑会被系统拒绝**（嵌套 sandbox_apply EPERM，实测 rc=71），此时该组自动 skip，
需在普通 Terminal（App 之外）跑才有真实结论。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import sandbox as sb  # noqa: E402


class _TempTemplatesMixin(unittest.TestCase):
    """把模板文件重定向到临时目录（绝不触碰真实 ~/.aigent/sandbox）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.seatbelt_path = base / "seatbelt.sb"
        self.bwrap_path = base / "bwrap_args.txt"
        self._patchers = [
            mock.patch.object(sb, "SANDBOX_DIR", base),
            mock.patch.object(sb, "SEATBELT_FILE", self.seatbelt_path),
            mock.patch.object(sb, "BWRAP_FILE", self.bwrap_path),
            mock.patch.object(sb, "_TEMPLATES", {
                "seatbelt": (sb.DEFAULT_SEATBELT_PROFILE, self.seatbelt_path),
                "bwrap": (sb.DEFAULT_BWRAP_ARGS, self.bwrap_path),
            }),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patchers:
            self.addCleanup(p.stop)


class TestTemplateMechanism(_TempTemplatesMixin):
    """默认模板落盘幂等 + 占位符校验 + 恢复默认。"""

    def test_ensure_templates_writes_defaults(self):
        sb.ensure_templates()
        self.assertTrue(self.seatbelt_path.exists())
        self.assertTrue(self.bwrap_path.exists())
        self.assertIn("{{WORKDIR}}", self.seatbelt_path.read_text(encoding="utf-8"))
        self.assertIn("{{COMMAND}}", self.bwrap_path.read_text(encoding="utf-8"))

    def test_ensure_templates_never_overwrites_user_file(self):
        self.seatbelt_path.write_text("(user customized)\n", encoding="utf-8")
        sb.ensure_templates()
        self.assertEqual(
            self.seatbelt_path.read_text(encoding="utf-8"), "(user customized)\n"
        )

    def test_validate_template_rejects_missing_placeholders(self):
        with self.assertRaises(ValueError):
            sb.validate_template("seatbelt", "(version 1)\n(deny network*)\n")
        with self.assertRaises(ValueError):
            sb.validate_template("bwrap", "{{WORKDIR}}\n{{TMPDIR}}\n")

    def test_validate_template_accepts_default(self):
        sb.validate_template("seatbelt", sb.DEFAULT_SEATBELT_PROFILE)
        sb.validate_template("bwrap", sb.DEFAULT_BWRAP_ARGS)

    def test_save_template_rejects_then_accepts(self):
        with self.assertRaises(ValueError):
            sb.save_template("seatbelt", "broken")
        good = sb.DEFAULT_SEATBELT_PROFILE + "\n; extra\n"
        sb.save_template("seatbelt", good)
        self.assertEqual(self.seatbelt_path.read_text(encoding="utf-8"), good)

    def test_reset_template_restores_default(self):
        self.bwrap_path.write_text("garbage-without-placeholders\n", encoding="utf-8")
        content = sb.reset_template("bwrap")
        self.assertEqual(content, sb.DEFAULT_BWRAP_ARGS)
        self.assertEqual(
            self.bwrap_path.read_text(encoding="utf-8"), sb.DEFAULT_BWRAP_ARGS
        )

    def test_read_template_falls_back_to_default_on_error(self):
        # 目录存在但文件不可读（用不存在 + 只读目录模拟读失败过于复杂，
        # 这里验证"文件被删后 read_template 自动补默认"的主链路即可）
        sb.ensure_templates()
        self.seatbelt_path.unlink()
        content = sb.read_template("seatbelt")
        self.assertEqual(content, sb.DEFAULT_SEATBELT_PROFILE)
        self.assertTrue(self.seatbelt_path.exists())


class TestPlaceholderSubstitution(_TempTemplatesMixin):
    """值占位符 + EXTRA_WRITABLE 展开（seatbelt 文本 / bwrap argv 两条路径）。"""

    def test_substitute_common_values(self):
        # 占位符替换会先 resolve 规范化（macOS /var→/private/var、/tmp→/private/tmp
        # 符号链接会让 Seatbelt subpath 匹配落空），期望值同样用 resolve() 计算
        out = sb._substitute_common(
            sb.DEFAULT_SEATBELT_PROFILE,
            workdir=Path("/tmp/ws"), tmpdir="/var/tmp", home=Path("/home/u"),
            extra_writable=[], kind="seatbelt",
        )
        self.assertIn(f'(subpath "{Path("/tmp/ws").resolve()}")', out)
        self.assertIn(f'(subpath "{Path("/var/tmp").resolve()}")', out)
        self.assertIn(f'(subpath "{Path("/home/u/.ssh").resolve()}")', out)
        self.assertNotIn("{{WORKDIR}}", out)

    def test_extra_writable_seatbelt_expansion(self):
        out = sb._expand_extra_writable(
            "seatbelt", [Path("/data/a"), Path("/data/b")])
        self.assertIn('(allow file-write* (subpath "/data/a"))', out)
        self.assertIn('(allow file-write* (subpath "/data/b"))', out)
        self.assertEqual(sb._expand_extra_writable("seatbelt", []), "")

    def test_extra_writable_bwrap_expansion(self):
        out = sb._expand_extra_writable("bwrap", [Path("/data/a")])
        self.assertIn("--bind /data/a /data/a", out)
        self.assertEqual(sb._expand_extra_writable("bwrap", []), "")


class TestBackendSelection(_TempTemplatesMixin):
    """get_backend：开关 / off / auto 顺序 / 强制指定 / 无后端。"""

    def _patch_env(self, enabled="1", backend="auto"):
        return [
            mock.patch.dict(os.environ, {"SANDBOX_ENABLED": enabled,
                                         "SANDBOX_BACKEND": backend}),
        ]

    def test_disabled_returns_none_silently(self):
        for p in self._patch_env(enabled="0"):
            p.start(); self.addCleanup(p.stop)
        self.assertIsNone(sb.get_backend())

    def test_backend_off_returns_none(self):
        for p in self._patch_env(backend="off"):
            p.start(); self.addCleanup(p.stop)
        self.assertIsNone(sb.get_backend())

    @mock.patch.object(sb.SeatbeltBackend, "is_available", return_value=True)
    def test_auto_on_darwin_picks_seatbelt(self, _m):
        for p in self._patch_env():
            p.start(); self.addCleanup(p.stop)
        with mock.patch.object(sb.sys, "platform", "darwin"):
            b = sb.get_backend()
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "seatbelt")

    @mock.patch.object(sb.BubblewrapBackend, "is_available", return_value=True)
    def test_auto_on_linux_picks_bwrap(self, _m):
        for p in self._patch_env():
            p.start(); self.addCleanup(p.stop)
        with mock.patch.object(sb.sys, "platform", "linux"):
            b = sb.get_backend()
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "bwrap")

    @mock.patch.object(sb.SeatbeltBackend, "is_available", return_value=False)
    @mock.patch.object(sb.BubblewrapBackend, "is_available", return_value=False)
    def test_no_backend_returns_none_when_enabled(self, _a, _b):
        for p in self._patch_env():
            p.start(); self.addCleanup(p.stop)
        with mock.patch.object(sb.sys, "platform", "win32"):
            self.assertIsNone(sb.get_backend())

    @mock.patch.object(sb.BubblewrapBackend, "is_available", return_value=True)
    @mock.patch.object(sb.SeatbeltBackend, "is_available", return_value=False)
    def test_forced_backend(self, _s, _b):
        for p in self._patch_env(backend="bwrap"):
            p.start(); self.addCleanup(p.stop)
        b = sb.get_backend()
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "bwrap")

    @mock.patch.object(sb.SeatbeltBackend, "is_available", return_value=False)
    def test_forced_backend_unavailable_returns_none(self, _s):
        for p in self._patch_env(backend="seatbelt"):
            p.start(); self.addCleanup(p.stop)
        self.assertIsNone(sb.get_backend())


class TestBwrapArgvBuild(_TempTemplatesMixin):
    """bwrap argv 构造：{{COMMAND}} 保持单参数、逐行切分、空 EXTRA_WRITABLE。"""

    def test_run_builds_expected_argv(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(sb.subprocess, "run", side_effect=fake_run):
            backend = sb.BubblewrapBackend()
            backend.run("echo hi", cwd=Path("/tmp/ws"), workdir=Path("/tmp/ws"),
                        extra_writable=[Path("/data/x")], timeout=10)
        argv = captured["argv"]
        # 占位符替换会先 resolve 规范化（macOS /tmp→/private/tmp），期望值同步计算
        ws, xd = str(Path("/tmp/ws").resolve()), str(Path("/data/x").resolve())
        self.assertEqual(argv[0], "bwrap")
        # 模板参数逐行展开
        self.assertIn("--ro-bind", argv)
        self.assertIn("/", argv[argv.index("--ro-bind") + 1])
        self.assertIn("--tmpfs", argv)
        # workdir 双写 bind
        wi = argv.index("--bind")
        self.assertEqual(argv[wi + 1], ws)
        self.assertEqual(argv[wi + 2], ws)
        # 额外可写目录展开
        xi = argv.index("--bind", wi + 1)
        self.assertEqual(argv[xi + 1], xd)
        # 命令整行替换且**保持单参数**（含空格也绝不切分）
        ci = argv.index("-c")
        self.assertEqual(argv[ci + 1], "echo hi")
        self.assertEqual(argv[-1], "echo hi")

    def test_run_without_extra_writable(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(sb.subprocess, "run", side_effect=fake_run):
            sb.BubblewrapBackend().run(
                "ls", cwd=Path("/tmp/ws"), workdir=Path("/tmp/ws"),
                extra_writable=[], timeout=10)
        # 模板之外不应再出现任何 --bind（workdir 的那一对除外）
        binds = [i for i, a in enumerate(captured["argv"]) if a == "--bind"]
        self.assertEqual(len(binds), 1)


class TestBlockedMarkers(unittest.TestCase):
    """沙盒拦截特征识别（run_bash 错误提示依据）。"""

    def test_markers(self):
        self.assertTrue(sb.looks_blocked_by_sandbox(
            "touch: /Users/u/x.txt: Operation not permitted"))
        self.assertTrue(sb.looks_blocked_by_sandbox(
            "touch: cannot touch 'x': Read-only file system"))
        self.assertTrue(sb.looks_blocked_by_sandbox(
            "sandbox_exec: compile error: ..."))
        self.assertFalse(sb.looks_blocked_by_sandbox("command not found"))
        self.assertFalse(sb.looks_blocked_by_sandbox(""))


class TestDefaultTemplateInvariants(unittest.TestCase):
    """默认模板的**跨平台纯文本不变式**（不依赖真实 sandbox-exec / bwrap）。

    2026-09-24 审出的两条 P0 都出在这里，且真机探针在这些环境跑不了（CI 无后端、
    应用内会话被禁止嵌套 sandbox_apply）→ 这一层是唯一能拦住它们的防线。
    """

    def test_bwrap_isolates_network_and_pid(self):
        """网络/pid 隔离行必须在位。

        背景：默认模板曾**漏掉 `--unshare-net`**（文档与设置页文案却都写"删该行放行
        网络"），使 Linux 沙盒实际上不断网；同时缺 `--unshare-pid` → `--proc /proc`
        直挂宿主进程表，可 kill 同 uid 进程（含智能体自身）。
        """
        self.assertIn("--unshare-net", sb.DEFAULT_BWRAP_ARGS.splitlines())
        self.assertIn("--unshare-pid", sb.DEFAULT_BWRAP_ARGS.splitlines())

    def test_bwrap_does_not_shadow_whole_aigent(self):
        """不许对整棵 {{HOME}}/.aigent 做 --tmpfs。

        背景：桌面端新建 default 会话的工作区就是 `~/.aigent/projects/default/scratch`
        （paths.default_scratch_paths()）。模板里 `--tmpfs {{HOME}}/.aigent` 排在
        `--bind {{WORKDIR}}` **之后**，会把工作区整个挂空（沙盒里 $PWD 是空目录、
        写入落进 tmpfs 即丢）。敏感面只遮蔽 `~/.aigent/config`。
        """
        lines = [l.strip() for l in sb.DEFAULT_BWRAP_ARGS.splitlines()]
        self.assertNotIn("--tmpfs {{HOME}}/.aigent", lines)
        self.assertIn("--tmpfs {{HOME}}/.aigent/config", lines)
        self.assertIn("--tmpfs {{HOME}}/.ssh", lines)

    def test_seatbelt_reallows_workspace_read_after_sensitive_deny(self):
        """工作区读放行必须**晚于** `~/.aigent` 读拒绝（Seatbelt 后写覆盖先写）。

        背景：原模板只 deny `{{HOME}}/.aigent` 的读、没有任何重放行，而桌面端 default
        会话的工作区正在该树下 → bash 连自己目录都读不了（ls / cat / python 全
        Operation not permitted）。
        """
        workdir = Path("/tmp/sbx-invariant-ws")
        home = Path("/tmp/sbx-invariant-home")
        profile = sb._substitute_common(
            sb.DEFAULT_SEATBELT_PROFILE, workdir=workdir, tmpdir="/tmp",
            home=home, extra_writable=[], kind="seatbelt",
        )
        wd, hm = str(workdir.resolve()), str(home.resolve())
        aigent_deny = profile.rindex(f'(deny file-read* (subpath "{hm}/.aigent")')
        ws_read_allow = profile.rindex(f'(allow file-read* (subpath "{wd}")')
        cred_deny = profile.rindex(f'(deny file-read* (subpath "{hm}/.ssh")')
        self.assertLess(
            aigent_deny, ws_read_allow,
            "工作区读放行必须排在 ~/.aigent 读拒绝之后，否则工作区自身读被拦",
        )
        self.assertLess(
            ws_read_allow, cred_deny,
            "凭证读拒绝必须排在最后（安全优先：工作区恰好落在凭据目录里也仍要拦）",
        )

    def test_seatbelt_keeps_workspace_write_and_dev_loopback(self):
        """回归位：写面与 /dev 回环不能因为改敏感面而丢（曾因 /dev/null 被 deny 导致 git 全挂）。"""
        profile = sb.DEFAULT_SEATBELT_PROFILE
        self.assertIn("(deny file-write*)", profile)
        self.assertIn('(allow file-write* (literal "/dev/null")', profile)
        # 凭证/本应用配置的写面兜底（工作区或额外目录被设成 $HOME 时也拦）
        self.assertIn('(deny file-write* (subpath "{{HOME}}/.ssh")', profile)
        self.assertIn('(subpath "{{HOME}}/.aigent/config"))', profile)
        self.assertIn("(deny network*)", profile)

    def test_block_markers_do_not_false_positive_on_bwrap_substring(self):
        """拦截特征串不含裸 "bwrap"（它同时是路径/输出的常见子串 → 误报成沙盒拦截）。"""
        self.assertNotIn("bwrap", sb.SANDBOX_BLOCK_MARKERS)
        self.assertFalse(sb.looks_blocked_by_sandbox("path /opt/bwrap/bin ok"))


class TestBackendStatus(unittest.TestCase):
    """backend_status：状态行数据（**off 必须回不可用**，否则关掉开关仍显示"生效中"）。"""

    def test_off_reports_unavailable(self):
        with mock.patch.dict(os.environ, {"SANDBOX_BACKEND": "off"}):
            st = sb.backend_status()
        self.assertFalse(st["backend_available"])
        self.assertIsNone(st["backend"])
        self.assertEqual(st["reason"], "off")
        self.assertEqual(st["platform"], sys.platform)

    def test_no_backend_reports_unsupported(self):
        with mock.patch.dict(os.environ, {"SANDBOX_BACKEND": "auto"}), \
             mock.patch.object(sb.SeatbeltBackend, "is_available", return_value=False), \
             mock.patch.object(sb.BubblewrapBackend, "is_available", return_value=False):
            st = sb.backend_status()
        self.assertFalse(st["backend_available"])
        self.assertEqual(st["reason"], "unsupported")

    def test_available_reports_ok(self):
        with mock.patch.dict(os.environ, {"SANDBOX_BACKEND": "seatbelt"}), \
             mock.patch.object(sb.SeatbeltBackend, "is_available", return_value=True):
            st = sb.backend_status()
        self.assertTrue(st["backend_available"])
        self.assertEqual(st["backend"], "seatbelt")
        self.assertEqual(st["reason"], "ok")


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt 真机探针仅 macOS 有意义")
class TestSeatbeltRealProbe(_TempTemplatesMixin):
    """**真机**验证默认 Seatbelt 模板的两条不变式（P0-1 的正反两面）。

    用临时模板目录（mixin）跑**默认模板内容**，探针工作区落在真实 `~/.aigent` 之下
    （复刻桌面端 default 会话的 workdir 位置），跑完即清理。

    若当前进程已在沙箱内（本应用自己的会话就是这样），`sandbox-exec` 无法嵌套应用
    profile（`sandbox_apply: Operation not permitted`, rc=71）→ 自动 skip，别误判成失败。
    """

    def setUp(self):
        super().setUp()
        self.probe_root = Path.home() / ".aigent" / "_sandbox_probe"
        self.workdir = self.probe_root / "ws"
        self.workdir.mkdir(parents=True, exist_ok=True)
        # 工作区之外的"敏感"文件（与工作区同在 ~/.aigent 树下）：必须仍不可读
        self.outside_secret = self.probe_root / "outside_secret.txt"
        self.outside_secret.write_text("top-secret", encoding="utf-8")

        def _cleanup():
            import shutil as _sh
            _sh.rmtree(self.probe_root, ignore_errors=True)

        self.addCleanup(_cleanup)

    def _run(self, command, cwd=None):
        backend = sb.SeatbeltBackend()
        if not backend.is_available():
            self.skipTest("当前环境无 sandbox-exec")
        return backend.run(command, cwd=cwd or self.workdir, workdir=self.workdir,
                           extra_writable=[self.workdir], timeout=30)

    def _check_nesting_allowed(self, r):
        if "sandbox_apply" in (r.stderr or ""):
            self.skipTest("当前进程已在沙箱内，无法嵌套应用 Seatbelt profile"
                          "（请在 App 之外的普通 Terminal 跑本组）")

    def test_workspace_under_aigent_is_readable(self):
        """工作区在 ~/.aigent 下时，bash 必须能读自己目录（修前必失败）。"""
        r = self._run("echo hi > probe.txt && cat probe.txt && ls")
        self._check_nesting_allowed(r)
        self.assertEqual(r.returncode, 0,
                         f"工作区读被打死（P0-1 复发）: {r.stdout!r} {r.stderr!r}")
        self.assertIn("hi", r.stdout)

    def test_outside_workspace_read_still_denied(self):
        """反向不变式：工作区之外的 ~/.aigent 内容仍不可读（收窄敏感面不能变成放行）。"""
        r = self._run(f"cat {self.outside_secret}")
        self._check_nesting_allowed(r)
        self.assertNotEqual(r.returncode, 0, "工作区外的 ~/.aigent 文件竟可读")
        self.assertNotIn("top-secret", r.stdout)

    def test_write_outside_workspace_denied(self):
        """写越界仍被拦（$HOME 下不属于工作区的路径）。"""
        r = self._run(f"echo x > {Path.home()}/.sbx_probe_escape")
        self._check_nesting_allowed(r)
        self.assertNotEqual(r.returncode, 0, "工作区外写入竟成功")
        self.assertTrue(sb.looks_blocked_by_sandbox(r.stderr or ""))

    def test_network_denied(self):
        """网络默认断开（curl 应失败）。"""
        r = self._run("curl -sS -m 5 https://example.com")
        self._check_nesting_allowed(r)
        self.assertNotEqual(r.returncode, 0, "沙盒内竟然能出网")


class TestWsBridgeSaveEnabled(unittest.TestCase):
    """ws_bridge._save_sandbox_enabled：写 config.json + 覆写 os.environ 热生效。

    另加三条**失败语义**（2026-09-24 加固）：config.json 读不出来就**拒绝写**
    （绝不能拿 {} 兜底后回写 —— 会把用户其他键整份抹掉），env 只在落盘成功后覆写。
    """

    def _ws_bridge(self):
        try:
            import ws_bridge
        except ImportError as e:  # websockets/openai 缺失的环境只跳过本组
            self.skipTest(f"ws_bridge 依赖不可用: {e}")
        return ws_bridge

    def test_save_writes_config_and_env(self):
        ws_bridge = self._ws_bridge()
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            cfg.write_text(json.dumps({"OTHER_KEY": "x"}), encoding="utf-8")
            with mock.patch.object(ws_bridge, "CONFIG_FILE", cfg), \
                 mock.patch.dict(os.environ, {"SANDBOX_ENABLED": "1"}):
                ws_bridge._save_sandbox_enabled(False)
                data = json.loads(cfg.read_text(encoding="utf-8"))
                self.assertEqual(data["SANDBOX_ENABLED"], "0")
                self.assertEqual(data["OTHER_KEY"], "x")  # 不动其他键
                self.assertEqual(os.environ.get("SANDBOX_ENABLED"), "0")
                ws_bridge._save_sandbox_enabled(True)
                self.assertEqual(os.environ.get("SANDBOX_ENABLED"), "1")

    def test_corrupt_config_refused_not_wiped(self):
        """config.json 坏掉时拒绝覆写：抛错、文件原样、env 不动（数据灾难防线）。"""
        ws_bridge = self._ws_bridge()
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            broken = "{ this is not json"
            cfg.write_text(broken, encoding="utf-8")
            with mock.patch.object(ws_bridge, "CONFIG_FILE", cfg), \
                 mock.patch.dict(os.environ, {"SANDBOX_ENABLED": "1"}):
                with self.assertRaises(ValueError):
                    ws_bridge._save_sandbox_enabled(False)
                # env 断言必须在 patch.dict 内 —— 否则键已被恢复移除，拿不到"原值"
                self.assertEqual(os.environ.get("SANDBOX_ENABLED"), "1")
            self.assertEqual(cfg.read_text(encoding="utf-8"), broken)

    def test_non_dict_config_refused(self):
        """顶层不是 JSON 对象也拒绝覆写（原先会静默变成"只剩 SANDBOX_ENABLED"）。"""
        ws_bridge = self._ws_bridge()
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            cfg.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
            with mock.patch.object(ws_bridge, "CONFIG_FILE", cfg), \
                 mock.patch.dict(os.environ, {"SANDBOX_ENABLED": "1"}):
                with self.assertRaises(ValueError):
                    ws_bridge._save_sandbox_enabled(False)
            self.assertEqual(json.loads(cfg.read_text(encoding="utf-8")),
                             ["not", "a", "dict"])

    def test_write_failure_keeps_env_unchanged(self):
        """落盘失败 → 抛错且 env 不覆写（避免"内存里关着、磁盘上开着"的假一致）。"""
        ws_bridge = self._ws_bridge()
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.json"
            cfg.write_text(json.dumps({"OTHER_KEY": "x"}), encoding="utf-8")
            with mock.patch.object(ws_bridge, "CONFIG_FILE", cfg), \
                 mock.patch.dict(os.environ, {"SANDBOX_ENABLED": "1"}), \
                 mock.patch.object(Path, "write_text",
                                   side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ws_bridge._save_sandbox_enabled(False)
                self.assertEqual(os.environ.get("SANDBOX_ENABLED"), "1")


if __name__ == "__main__":
    unittest.main()
