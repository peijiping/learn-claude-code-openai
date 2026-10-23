#!/usr/bin/env python3
"""配置文件目录收口（`~/.aigent/*.json` → `~/.aigent/config/*.json`）守护测试 —— 2026-09-22。

守护四件事：
1. `config.migrate_config_dir()` 的搬迁/回收语义（幂等 / 目标已存在时的残件处理 /
   备份变体 / 降级路径）—— 口径是**顶层只允许出现目录**，配置文件一个都不留；
2. `config.config_path()` 是配置文件的唯一落点（只接裸文件名，拒绝 `../x.json`）；
3. 各模块**路径常量**确实落在 `CONFIG_DIR` 下（防某处漏改回顶层 —— 这类漏改
   不会报错，只会静默读写一个不存在的位置，用户看到的是"配置没生效"）；
4. **源码静态扫描**：`agents/*.py` 里不得出现 `AIGENT_HOME / "<配置文件>.json"`
   （2026-09-22 实事故：旧运行时把 providers.json 物化到了顶层，过了 1 个多小时
   才被发现。常量口径测试挡不住"某处新写的顶层路径"，所以再补一道源码扫描）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from config import (  # noqa: E402
    AIGENT_HOME,
    CONFIG_DIR,
    CONFIG_FILE,
    CONFIG_FILENAMES,
    CREDENTIALS_FILE,
    config_path,
    is_config_path,
    migrate_config_dir,
)

# 迁移清单（与 config.CONFIG_FILENAMES / config._LEGACY_CONFIG_FILES 同口径；测试里
# 显式列出，防止实现侧悄悄删掉清单项而测试仍绿）
_LEGACY_NAMES = (
    "config.json",
    "credentials.json",
    "llmconfig.json",
    "providers.json",
    "permissions.json",
)


class _TempHomeTest(unittest.TestCase):
    """所有用例都在临时目录里造 ~/.aigent，绝不触碰真实用户配置。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="aigent-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def _write(self, name: str, content: str = "{}\n") -> Path:
        p = self.home / name
        p.write_text(content, encoding="utf-8")
        return p


class TestMigrateConfigDir(_TempHomeTest):
    def test_moves_all_known_files_and_keeps_content(self):
        payloads = {
            "config.json": '{"LOG_LEVEL": "DEBUG"}\n',
            "credentials.json": '{"OPENAI_API_KEY": "sk-x"}\n',
            "llmconfig.json": '{"version": 2}\n',
            "providers.json": '{"version": 1}\n',
            "permissions.json": '{"default_mode": "default"}\n',
        }
        for name, body in payloads.items():
            self._write(name, body)

        moved = migrate_config_dir(self.home)

        self.assertEqual(len(moved), len(_LEGACY_NAMES))
        for name, body in payloads.items():
            dst = self.home / "config" / name
            self.assertTrue(dst.is_file(), f"{name} 应已落位 config/")
            # 内容逐字节一致（搬迁不是"重新生成"）
            self.assertEqual(dst.read_text(encoding="utf-8"), body)
            # 旧位置不再留副本
            self.assertFalse((self.home / name).exists(), f"{name} 旧位置应已清空")

    def test_second_run_is_noop(self):
        self._write("config.json")
        self.assertEqual(len(migrate_config_dir(self.home)), 1)
        # 幂等：文件已在目标位，第二次无动作、不报错
        self.assertEqual(migrate_config_dir(self.home), [])

    def test_existing_target_with_same_content_clears_stray_copy(self):
        """目标已在且内容一致 → 顶层残件直接清理（等价副本，删了不丢信息）。

        口径（2026-09-22 用户明确要求）：所有配置文件**只允许出现在 config/ 下**，
        顶层不得留任何残件 —— 旧实现是"跳过且不删源"，会在顶层留一个永久孤儿
        （实际发生过：旧运行时物化的 providers.json 一直躺在顶层）。
        """
        payload = '{"default_mode": "same"}\n'
        self._write("permissions.json", payload)
        (self.home / "config").mkdir()
        (self.home / "config" / "permissions.json").write_text(payload, encoding="utf-8")

        moved = migrate_config_dir(self.home)

        self.assertEqual(moved, [], "等价副本不产生新文件")
        self.assertFalse(
            (self.home / "permissions.json").exists(),
            "顶层不得残留配置文件",
        )
        self.assertEqual(
            (self.home / "config" / "permissions.json").read_text(encoding="utf-8"), payload
        )

    def test_existing_target_with_different_content_archives_stray_copy(self):
        """目标已在且内容不同 → 顶层那份归档为 `<name>.stale-<ts>` 收进 config/。

        现行版本（config/ 下那份）绝不被旧文件覆盖；旧内容留证可查，不静默丢弃。
        """
        self._write("providers.json", '{"version": 1, "src": "top"}\n')
        (self.home / "config").mkdir()
        (self.home / "config" / "providers.json").write_text('{"version": 2}\n', encoding="utf-8")

        moved = migrate_config_dir(self.home)

        self.assertFalse((self.home / "providers.json").exists(), "顶层不得残留")
        self.assertEqual(
            (self.home / "config" / "providers.json").read_text(encoding="utf-8"),
            '{"version": 2}\n',
            "新位置是生效版本，绝不能被旧文件覆盖",
        )
        stales = sorted(p for p in (self.home / "config").iterdir() if ".stale-" in p.name)
        self.assertEqual(len(stales), 1, "旧内容必须留证")
        self.assertEqual(stales[0].name.split(".stale-")[0], "providers.json")
        self.assertEqual(stales[0].read_text(encoding="utf-8"), '{"version": 1, "src": "top"}\n')
        self.assertEqual([p.name for p in moved], [stales[0].name], "返回值应指向归档落点")

    def test_reclaim_is_idempotent(self):
        """连续两次迁移：不产生第二份归档、不报错（幂等）。"""
        self._write("permissions.json", "old\n")
        (self.home / "config").mkdir()
        (self.home / "config" / "permissions.json").write_text("new\n", encoding="utf-8")

        first = migrate_config_dir(self.home)
        second = migrate_config_dir(self.home)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        stales = [p for p in (self.home / "config").iterdir() if ".stale-" in p.name]
        self.assertEqual(len(stales), 1)

    def test_provider_catalog_backups_move_along(self):
        self._write("providers.json")
        self._write("providers.json.bak-20260920-182528")
        self._write("providers.json.bak-20260101-000000")

        moved = migrate_config_dir(self.home)

        names = sorted(p.name for p in moved)
        self.assertEqual(names, [
            "providers.json",
            "providers.json.bak-20260101-000000",
            "providers.json.bak-20260920-182528",
        ])
        self.assertFalse(any(p.name.startswith("providers.json") for p in self.home.iterdir()
                             if p.is_file()))

    def test_credentials_mode_tightened_to_0600(self):
        cred = self._write("credentials.json", '{"OPENAI_API_KEY": "sk-x"}\n')
        os.chmod(cred, 0o644)

        migrate_config_dir(self.home)

        mode = stat.S_IMODE((self.home / "config" / "credentials.json").stat().st_mode)
        self.assertEqual(mode, 0o600, "密钥文件落位后必须收紧到 0600")

    def test_falls_back_to_copy_when_rename_fails(self):
        """跨设备 / 权限受限导致 rename 失败时，退化为 copy2 + 删源（不丢配置）。"""
        self._write("config.json", '{"LOG_LEVEL": "DEBUG"}\n')

        with mock.patch.object(Path, "rename", side_effect=OSError("cross-device link")):
            moved = migrate_config_dir(self.home)

        self.assertEqual(len(moved), 1)
        self.assertEqual(
            (self.home / "config" / "config.json").read_text(encoding="utf-8"),
            '{"LOG_LEVEL": "DEBUG"}\n',
        )
        self.assertFalse((self.home / "config.json").exists(), "降级路径也必须清掉旧位置")

    def test_empty_home_creates_nothing(self):
        """全新环境（无旧文件）：不建目录、不返回 —— 目录骨架由 load()/ensure_dirs() 负责。"""
        self.assertEqual(migrate_config_dir(self.home), [])
        self.assertFalse((self.home / "config").exists())


class TestConfigPathConstants(unittest.TestCase):
    """路径常量口径：配置文件必须都在 CONFIG_DIR 下（防漏改）。"""

    def test_user_config_files_live_under_config_dir(self):
        self.assertEqual(CONFIG_DIR, AIGENT_HOME / "config")
        self.assertEqual(CONFIG_FILE, CONFIG_DIR / "config.json")
        self.assertEqual(CREDENTIALS_FILE, CONFIG_DIR / "credentials.json")

    def test_llm_config_and_provider_catalog_live_under_config_dir(self):
        import llm_config

        self.assertEqual(llm_config.LLM_CONFIG_FILE, CONFIG_DIR / "llmconfig.json")
        self.assertEqual(llm_config.PROVIDER_CATALOG_FILE, CONFIG_DIR / "providers.json")

    def test_permission_store_defaults_to_config_dir(self):
        from permission import PermissionStore

        self.assertEqual(PermissionStore().path, CONFIG_DIR / "permissions.json")


class TestConfigPathGuard(unittest.TestCase):
    """`config_path()` = 配置文件的唯一落点（只接裸文件名，其余一律拒绝）。"""

    def test_every_known_filename_lands_under_config_dir(self):
        self.assertEqual(tuple(CONFIG_FILENAMES), _LEGACY_NAMES)
        for name in CONFIG_FILENAMES:
            self.assertEqual(config_path(name), CONFIG_DIR / name)

    def test_non_bare_names_are_rejected(self):
        for bad in ("../permissions.json", "/tmp/permissions.json", "sub/permissions.json",
                    "..", ".", ""):
            with self.assertRaises(ValueError, msg=f"{bad!r} 必须被拒绝"):
                config_path(bad)

    def test_is_config_path(self):
        self.assertTrue(is_config_path(CONFIG_DIR / "permissions.json"))
        # 顶层旧路径 / 相对文件名都不是合法落点
        self.assertFalse(is_config_path(AIGENT_HOME / "permissions.json"))
        self.assertFalse(is_config_path(Path("permissions.json")))


# 例外白名单：permission.py 的敏感路径黑名单**故意**引用迁移前的顶层旧位置
# （防御"搬迁失败 / 用户手工复制一份"变成可读后门），它不是写入路径。
_TOP_LEVEL_REF_ALLOWLIST = {"permission.py"}
_TOP_LEVEL_REF_RE = re.compile(
    r"AIGENT_HOME\s*/\s*[\"']("
    + "|".join(name.replace(".", r"\.") for name in _LEGACY_NAMES)
    + r")[\"']"
)


class TestNoModulePinsConfigFilesAtTopLevel(unittest.TestCase):
    """静态扫描：`agents/*.py` 不得出现 `AIGENT_HOME / "<配置文件>.json"`。

    这是本事故（顶层冒出 providers.json）唯一能提前拦住的一层 —— 常量口径测试
    只覆盖已知模块，新写的顶层路径它看不见。
    """

    def test_only_deny_list_references_legacy_top_level_paths(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(AGENTS_DIR.glob("*.py")):
            hits = _TOP_LEVEL_REF_RE.findall(path.read_text(encoding="utf-8"))
            if hits and path.name not in _TOP_LEVEL_REF_ALLOWLIST:
                offenders[path.name] = hits
        self.assertEqual(
            offenders, {},
            "配置文件路径必须由 config.config_path() 给出，不得自拼 AIGENT_HOME / \"xxx.json\"",
        )

    def test_allowlist_is_actually_used(self):
        """白名单不能是死条款：permission.py 里应仍有旧顶层路径的黑名单兜底。"""
        hits = _TOP_LEVEL_REF_RE.findall(
            (AGENTS_DIR / "permission.py").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(hits), 3, "顶层旧路径的敏感拦截兜底不该被删掉")


if __name__ == "__main__":
    unittest.main()
