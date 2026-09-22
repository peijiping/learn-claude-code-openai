#!/usr/bin/env python3
"""配置文件目录收口（`~/.aigent/*.json` → `~/.aigent/config/*.json`）守护测试 —— 2026-09-22。

守护三件事：
1. `config.migrate_config_dir()` 的搬迁语义（幂等 / 不覆盖 / 备份变体 / 降级路径）；
2. 各模块**路径常量**确实落在 `CONFIG_DIR` 下（防某处漏改回顶层 —— 这类漏改
   不会报错，只会静默读写一个不存在的位置，用户看到的是"配置没生效"）；
3. `PermissionStore` 默认落点（权限规则文件是本次迁移的触发需求）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
import os
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
    CREDENTIALS_FILE,
    migrate_config_dir,
)

# 迁移清单（与 config._LEGACY_CONFIG_FILES 同口径；测试里显式列出，
# 防止实现侧悄悄删掉清单项而测试仍绿）
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

    def test_existing_target_wins_and_source_is_kept(self):
        """目标已存在 → 跳过且**不删源**（不可逆操作宁可留给用户自己清理）。"""
        self._write("permissions.json", '{"default_mode": "old"}\n')
        (self.home / "config").mkdir()
        (self.home / "config" / "permissions.json").write_text(
            '{"default_mode": "new"}\n', encoding="utf-8"
        )

        moved = migrate_config_dir(self.home)

        self.assertEqual(moved, [])
        self.assertEqual(
            json.loads((self.home / "config" / "permissions.json").read_text())["default_mode"],
            "new",
            "新位置是生效版本，绝不能被旧文件覆盖",
        )
        self.assertTrue((self.home / "permissions.json").exists(), "旧文件不得被删除")

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


if __name__ == "__main__":
    unittest.main()
