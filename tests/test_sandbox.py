#!/usr/bin/env python3
"""沙盒执行隔离（agents/sandbox.py）回归测试 —— 2026-09-22。

守护五件事（见 docs/frontend/20）：
1. 模板机制：默认模板落盘幂等（绝不覆盖用户已有文件）、占位符校验拒存坏模板、
   恢复默认模板；
2. 占位符替换：值占位符 + EXTRA_WRITABLE 段展开（多目录 / 空目录）；
3. 后端探测：auto 顺序（darwin→seatbelt / linux→bwrap）、SANDBOX_BACKEND 强制
   指定、开关关闭 / off 静默返回 None、已启用但无后端返回 None；
4. bwrap argv 构造：{{COMMAND}} 整行替换且保持单参数、逐行 shlex 切分；
5. ws_bridge._save_sandbox_enabled：落盘 config.json + 直接覆写 os.environ（热生效）。

集成验证（真实 sandbox-exec 拦截行为）在 macOS 本机手动执行：
    sandbox-mod.run 经 run_bash：写工作区外 / 读 ~/.ssh / 网络默认拦截。
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


class TestWsBridgeSaveEnabled(unittest.TestCase):
    """ws_bridge._save_sandbox_enabled：写 config.json + 覆写 os.environ 热生效。"""

    def test_save_writes_config_and_env(self):
        try:
            import ws_bridge
        except ImportError as e:  # websockets/openai 缺失的环境只跳过本组
            self.skipTest(f"ws_bridge 依赖不可用: {e}")
            return
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


if __name__ == "__main__":
    unittest.main()
