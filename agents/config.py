#!/usr/bin/env python3
"""
config.py - 应用配置管理（用户级配置目录 ~/.aigent）

将配置从「项目根 .env」迁移到主流「用户级配置目录」模式（对齐
Claude Code ~/.claude、Codex ~/.codex 的做法）：

    ~/.aigent/config/config.json         用户级非敏感配置（扁平键，键名与 .env 一致）
    ~/.aigent/config/credentials.json    密钥（OPENAI_API_KEY 等，权限 0600）
    [项目根]/.aigent/config.json          项目级覆盖（可选，加入 .gitignore）
    .env                                  遗留兜底（过渡期保留）

配置文件统一收在 `~/.aigent/config/` 目录下（2026-09-22 迁移）：
`config.json` / `credentials.json` / `llmconfig.json` / `providers.json` /
`permissions.json` 全部集中在 config/，`~/.aigent/` 顶层只留**目录**
（logs / projects / skills / mcp / worktrees）。好处：用户「备份 / 审计 /
迁移配置」只需看一个目录；顶层不再是一堆文件名混杂。旧顶层路径由
`migrate_config_dir()` 一次性搬入（幂等）。

加载优先级（高 → 低）：
    真实环境变量 > 项目级 config.json > 用户级 config.json > .env > 代码默认值

load() 按上述优先级把配置合并进 os.environ（setdefault），业务模块继续
os.environ.get(KEY, default) 内联读取，零改动即可切换到新配置源。

本模块不 import paths.py（避免循环依赖）；paths.py 单向依赖本模块的 AIGENT_HOME。
"""

import json
import os
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# ── 用户级应用 home ──────────────────────────────────────────────
AIGENT_HOME = Path.home() / ".aigent"
# 配置文件目录（2026-09-22）：所有 *.json 配置集中于此，顶层只留目录。
CONFIG_DIR = AIGENT_HOME / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

# 迁移清单：旧顶层 `~/.aigent/<name>` → `~/.aigent/config/<name>`（保序，便于日志）
_LEGACY_CONFIG_FILES = (
    "config.json",
    "credentials.json",
    "llmconfig.json",
    "providers.json",
    "permissions.json",
)
# 随主文件一起搬迁的备份变体（glob 前缀 → 文件名前缀）
_LEGACY_CONFIG_BACKUP_PREFIXES = ("providers.json.bak-",)

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


def migrate_config_dir(home: Path | None = None) -> list[Path]:
    """把 home 顶层的配置文件搬进 `home/config/`（幂等，可重复执行）。

    规则（2026-09-22，配置收口到 config/ 目录）：
    - 源不存在 → 跳过；
    - 目标已存在 → **跳过且不删源**（新位置的才是当前生效版本；删旧文件属于
      不可逆操作，宁可留个孤儿文件让用户自己清理。旧路径仍被 `permission.py`
      的敏感路径黑名单拦截，残留副本不会变成读取后门）；
    - 源在、目标不在 → `rename`；跨设备等 `OSError` 时退化为 copy2 + unlink；
    - `providers.json` 的备份变体（`providers.json.bak-*`）随主文件一并搬迁；
    - `credentials.json` 落位后收紧 0600。

    返回实际搬迁的**目标路径**列表（空 = 无动作）。`home` 可注入（测试传临时
    目录，避免触碰真实 `~/.aigent`），缺省 = 真实用户目录。
    """
    root = Path(home) if home is not None else AIGENT_HOME
    dst_dir = root / "config"
    candidates: list[Path] = []
    for name in _LEGACY_CONFIG_FILES:
        candidates.append(root / name)
    for prefix in _LEGACY_CONFIG_BACKUP_PREFIXES:
        candidates.extend(sorted(root.glob(f"{prefix}*")))
    pending = [
        (src, dst_dir / src.name)
        for src in candidates
        if src.is_file() and not (dst_dir / src.name).exists()
    ]
    if not pending:
        return []
    dst_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for src, dst in pending:
        try:
            src.rename(dst)
        except OSError:
            # 跨设备 / 权限受限：退化为「复制 + 删源」，仍保证旧路径不留副本
            try:
                shutil.copy2(src, dst)
                src.unlink()
            except OSError as e:
                print(f"[config] 配置文件迁移失败，保持原位：{src} → {dst}（{e}）")
                continue
        moved.append(dst)
    if moved:
        print(f"[config] 配置文件已迁入 {dst_dir}：" + "、".join(p.name for p in moved))
    cred = dst_dir / "credentials.json"
    if cred.exists():
        try:
            os.chmod(cred, 0o600)
        except OSError:
            pass
    return moved


def load() -> None:
    """启动时调用：按优先级把配置合并进 os.environ。幂等。"""
    AIGENT_HOME.mkdir(parents=True, exist_ok=True)
    # 0) 配置文件收口到 config/（首次自动搬迁旧顶层文件，幂等）
    migrate_config_dir()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
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


def _print_first_run_guide() -> None:
    """全新首次启动：目录骨架已建好，打印配置引导。"""
    print(f"[config] 首次启动：已创建配置目录 {CONFIG_DIR}")
    print(f"[config]   非敏感配置 → {CONFIG_FILE}（键名参考 .env.example）")
    print(f"[config]   API Key/密钥 → {CREDENTIALS_FILE}（权限 0600），或用同名环境变量")
    print("[config]   未配置项将使用代码默认值")


def migrate_legacy(legacy_home: Path) -> None:
    """
    一次性迁移（幂等）：legacy_home（WorkSpace/HomeDir）下的 skills/worktrees/mcp
    搬迁到 ~/.aigent/ 对应目录；顶层散落的配置文件收口到 ~/.aigent/config/；
    config.json 缺失时从 .env 生成种子。
    全新环境（~/.aigent 不存在）时预建目录骨架并打印首次启动引导。
    由 paths.py 的 ensure_dirs() 在导入期调用，传 ROOT_DIR/"WorkSpace/HomeDir"。
    """
    first_run = not AIGENT_HOME.exists()
    AIGENT_HOME.mkdir(parents=True, exist_ok=True)
    # 配置文件收口到 ~/.aigent/config/（必须早于下面的 CONFIG_FILE.exists() 判定，
    # 否则「旧位置有配置」的用户会被误判为首次运行、再种一份空配置）
    migrate_config_dir()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("skills", "worktrees", "mcp"):
        src = legacy_home / name
        dst = AIGENT_HOME / name
        if src.exists() and not dst.exists():
            src.rename(dst)
            print(f"[config] 已迁移 {src} → {dst}")
        elif not dst.exists():
            # 全新启动：预建目录骨架
            dst.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        _seed_from_env_file()
        if not CONFIG_FILE.exists():
            # 无 .env 可播种：生成空配置，参数全部走代码默认值
            CONFIG_FILE.write_text("{}\n", encoding="utf-8")
            print(f"[config] 已创建空配置 {CONFIG_FILE}（参数将使用代码默认值）")
    ensure_credentials_permission()
    if first_run:
        _print_first_run_guide()
