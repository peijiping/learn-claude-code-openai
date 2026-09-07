#!/usr/bin/env python3
"""
config.py - 应用配置管理（用户级配置目录 ~/.aigent）

将配置从「项目根 .env」迁移到主流「用户级配置目录」模式（对齐
Claude Code ~/.claude、Codex ~/.codex 的做法）：

    ~/.aigent/config.json         用户级非敏感配置（扁平键，键名与 .env 一致）
    ~/.aigent/credentials.json    密钥（OPENAI_API_KEY 等，权限 0600）
    [项目根]/.aigent/config.json  项目级覆盖（可选，加入 .gitignore）
    .env                          遗留兜底（过渡期保留）

加载优先级（高 → 低）：
    真实环境变量 > 项目级 config.json > 用户级 config.json > .env > 代码默认值

load() 按上述优先级把配置合并进 os.environ（setdefault），业务模块继续
os.environ.get(KEY, default) 内联读取，零改动即可切换到新配置源。

本模块不 import paths.py（避免循环依赖）；paths.py 单向依赖本模块的 AIGENT_HOME。
"""

import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# ── 用户级应用 home ──────────────────────────────────────────────
AIGENT_HOME = Path.home() / ".aigent"
CONFIG_FILE = AIGENT_HOME / "config.json"
CREDENTIALS_FILE = AIGENT_HOME / "credentials.json"

# 密钥键判定：以 _API_KEY / _TOKEN / _SECRET 结尾的键视为密钥。
# 注意 _TOKEN$ 锚定结尾，MAX_CONTEXT_TOKENS 等以 _TOKENS 结尾的键不会误判。
_SECRET_KEY_RE = re.compile(r"(_API_KEY|_TOKEN|_SECRET)$")


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _merge_json_into_env(path: Path) -> None:
    """把扁平键 JSON 配置 setdefault 进 os.environ（不覆盖已存在的变量）。"""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)):
            os.environ.setdefault(str(key), str(value))


def load() -> None:
    """启动时调用：按优先级把配置合并进 os.environ。幂等。"""
    AIGENT_HOME.mkdir(parents=True, exist_ok=True)
    # 1) 真实环境变量优先；.env 仅填充空缺（遗留兜底）
    load_dotenv(override=False)
    # 2) 用户级配置 + 密钥
    _merge_json_into_env(CONFIG_FILE)
    _merge_json_into_env(CREDENTIALS_FILE)
    # 3) 项目级覆盖（cwd 在 agent_cli / ws_bridge 启动时均为仓库根）
    _merge_json_into_env(Path.cwd() / ".aigent" / "config.json")


def ensure_credentials_permission() -> None:
    """credentials.json 权限收紧为 0600（仅本用户可读写）。"""
    if CREDENTIALS_FILE.exists():
        os.chmod(CREDENTIALS_FILE, 0o600)


def _seed_from_env_file() -> None:
    """.env 存在且 config.json 缺失时：非密钥键 → config.json，密钥 → credentials.json（0600）。"""
    env_file = Path.cwd() / ".env"
    if not env_file.exists():
        return
    values = dotenv_values(env_file)
    if not values:
        return
    non_secret = {k: v for k, v in values.items() if not _is_secret_key(k) and v is not None}
    secret = {k: v for k, v in values.items() if _is_secret_key(k) and v}
    if non_secret:
        CONFIG_FILE.write_text(
            json.dumps(non_secret, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[config] 已从 .env 生成 {CONFIG_FILE}")
    if secret:
        CREDENTIALS_FILE.write_text(
            json.dumps(secret, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(CREDENTIALS_FILE, 0o600)
        print(f"[config] 已从 .env 生成 {CREDENTIALS_FILE}（权限 0600）")


def migrate_legacy(legacy_home: Path) -> None:
    """
    一次性迁移（幂等）：legacy_home（WorkSpace/HomeDir）下的 skills/worktrees/mcp
    搬迁到 ~/.aigent/ 对应目录；config.json 缺失时从 .env 生成种子。
    由 paths.py 的 ensure_dirs() 在导入期调用，传 ROOT_DIR/"WorkSpace/HomeDir"。
    """
    AIGENT_HOME.mkdir(parents=True, exist_ok=True)
    for name in ("skills", "worktrees", "mcp"):
        src = legacy_home / name
        dst = AIGENT_HOME / name
        if src.exists() and not dst.exists():
            src.rename(dst)
            print(f"[config] 已迁移 {src} → {dst}")
    if not CONFIG_FILE.exists():
        _seed_from_env_file()
    ensure_credentials_permission()
