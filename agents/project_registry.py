"""
project_registry.py - 工作空间注册表（`~/.aigent/projects/projects.json` 的唯一读写入口）

设计见 `docs/frontend/11-工作空间管理.md`。这里只回答四个问题：

1. 有哪些工作空间？（`list_infos`）
2. 某个工作空间的元数据目录 / 沙箱根在哪？（`paths` → `paths.WorkspacePaths`）
3. 选了新目录 → 怎么登记？（`create`：校验 → 生成 `ws` 短码 → 建同构元数据目录 → 落索引）
4. 重命名 / 删除怎么落盘？（`rename` / `remove`）

硬约束（违反即数据事故，勿放宽）：

- **default 恒存在、不可删、不可改名**。它不依赖 projects.json（索引丢失/损坏也要能用），
  沙箱根取遗留 `WORKDIR`，元数据目录叫 `default` —— **存量数据零迁移**。
- **索引读-改-写必须整段持锁 + 原子写**（临时文件 + `os.replace`）。理由与任务文件同：
  半截文件被读者读到会让整个侧边栏空间列表消失。
- **删除只删元数据目录，绝不碰用户选定的真实目录**（那是用户的代码/文件）。
- 元数据目录名不用文件夹名（会重名），用 `ws` + 10 位 base62 短码；
  目录内部结构与 default 完全同构（`paths.WORKSPACE_SUBDIRS`）。
"""

import json
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import AIGENT_HOME
from logger import get_logger
from paths import (
    DEFAULT_PROJECT_ID,
    DEFAULT_PROJECT_NAME,
    PROJECTS_INDEX,
    PROJECTS_ROOT,
    PROJECT_ID_LEN,
    PROJECT_ID_PREFIX,
    WORKSPACE_SUBDIRS,
    WorkspacePaths,
    workspace_paths,
)
from session_manage import BASE62_CHARS  # 与会话短码同一字母表（避免两处漂移）

log = get_logger("projects")

INDEX_VERSION = 1

# 目录名去重后缀上限（同名目录太多时直接放弃并报错，好过无限循环）
_MAX_NAME_SUFFIX = 99
# 短码重掷上限
_MAX_ID_ATTEMPTS = 200


class WorkspaceError(Exception):
    """工作空间操作被拒绝（含**用户可见的原因**）。

    `ws_bridge` 捕获后原样回 `error` 信封 —— 这类错误（目录不存在/不可写、
    删默认空间、重名…）都应该让用户看到具体原因，而不是被吞成"操作失败"。
    """


@dataclass
class ProjectInfo:
    """一个工作空间的元数据（落盘字段 + 派生字段）。

    落盘的是 `id / name / path / created_at / last_opened_at`；
    `system` 与 `exists` 是**派生**的，不写进 projects.json（避免双写不一致）。
    """

    id: str
    name: str
    path: Optional[str] = None
    created_at: Optional[str] = None
    last_opened_at: Optional[str] = None
    # 派生：default 空间（不可删/不可改名）
    system: bool = False
    # 派生：真实目录当前可达（default 恒 True —— 它没有真实目录，不存在"失效"）
    exists: bool = True

    def to_payload(self, session_count: int = 0) -> dict:
        """转成给前端的 JSON（`projects` 信封里的元素）。"""
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "system": self.system,
            "exists": self.exists,
            "created_at": self.created_at,
            "last_opened_at": self.last_opened_at,
            "session_count": session_count,
        }


def _now_iso() -> str:
    """本地时间秒级 isoformat（与 session_manage._now_iso 同口径）。"""
    return datetime.now().isoformat(timespec="seconds")


def _default_entry() -> dict:
    return {
        "id": DEFAULT_PROJECT_ID,
        "name": DEFAULT_PROJECT_NAME,
        "path": None,
        "created_at": None,
        "last_opened_at": None,
    }


class WorkspaceRegistry:
    """projects.json 的读写门面。

    进程内单例由 `get_registry()` 提供；测试可直接 new 一个指向临时目录的实例
    （`projects_root` 一并注入，避免污染真实 `~/.aigent/projects`）。
    """

    def __init__(self, index_path: Path | str | None = None,
                 projects_root: Path | str | None = None):
        self.index_path = Path(index_path) if index_path else PROJECTS_INDEX
        self.projects_root = Path(projects_root) if projects_root else PROJECTS_ROOT
        # 索引的读-改-写互斥锁：多个调用线程（asyncio.to_thread）可能并发 add/rename/
        # remove，非互斥的 RMW 会丢更新（后写覆盖先写）。可重入：create 内部会
        # 调 _load → 再 _write。
        self._lock = threading.RLock()

    # ═══════════════════════════════════════════════════════════
    #  读
    # ═══════════════════════════════════════════════════════════
    def ensure(self) -> None:
        """启动自举：projects.json 缺失/损坏时重建为只含 default 的索引。

        幂等，可反复调用。老用户升级路径走这里 —— 只有 default 一个空间，
        行为与升级前完全一致。
        """
        with self._lock:
            data = self._load()
            self._write(data)

    def list_infos(self) -> list[ProjectInfo]:
        """全部工作空间（**default 恒第一**，其余按 last_opened_at 新→旧）。"""
        return [self._to_info(e) for e in self._load()["projects"]]

    def get(self, project_id: str) -> Optional[ProjectInfo]:
        pid = str(project_id or "")
        for info in self.list_infos():
            if info.id == pid:
                return info
        return None

    def require(self, project_id: str) -> ProjectInfo:
        info = self.get(project_id)
        if info is None:
            raise WorkspaceError(f"工作空间不存在：{project_id}")
        return info

    def paths(self, project_id: str) -> WorkspacePaths:
        """该工作空间的路径束（会话/任务/记忆/沙箱根的唯一出处）。

        default 走 `paths.workspace_paths`（沙箱根 = 遗留 WORKDIR）；自定义空间用
        **本注册表的 projects_root** 拼元数据目录，这样测试注入临时根时不会
        把目录建到真实 `~/.aigent` 下。
        """
        info = self.require(project_id)
        if info.id == DEFAULT_PROJECT_ID:
            return workspace_paths(DEFAULT_PROJECT_ID)
        # bash 与文件工具同根（2026-09-20 规则收口进路径束，原在 Agent 推导）
        root = Path(info.path or "")
        return WorkspacePaths(
            info.id, self.projects_root / info.id, root, bash_cwd=root
        )

    def active_id(self) -> str:
        """当前活动工作空间（前端 chip 默认值 / 无 project_id 的 chat 归属）。"""
        active = str(self._load().get("active") or DEFAULT_PROJECT_ID)
        return active if self.get(active) is not None else DEFAULT_PROJECT_ID

    # ═══════════════════════════════════════════════════════════
    #  写
    # ═══════════════════════════════════════════════════════════
    def set_active(self, project_id: str) -> ProjectInfo:
        """记录"当前活动空间"（跨重启保留，前端 chip 恢复用）。"""
        with self._lock:
            data = self._load()
            info = self._find(data, project_id)
            if info is None:
                raise WorkspaceError(f"工作空间不存在：{project_id}")
            info["last_opened_at"] = _now_iso()
            data["active"] = info["id"]
            self._write(data)
        return self._to_info(info)

    def create(self, real_path: str, name: str | None = None) -> ProjectInfo:
        """把一个真实目录登记为工作空间（幂等：同一目录已登记则直接复用）。

        `name` 缺省用文件夹名；同名（不同目录）自动加 ` (2)` 后缀。
        成功时已建好元数据目录与全部同构子目录。
        """
        norm = self._validate_dir(real_path)
        with self._lock:
            data = self._load()
            for entry in data["projects"]:
                if entry.get("path") and self._same_path(entry["path"], norm):
                    # 同一目录再次选择：不新建条目，直接把既有空间打开
                    entry["last_opened_at"] = _now_iso()
                    data["active"] = entry["id"]
                    self._write(data)
                    info = self._to_info(entry)
                    log.info("工作空间复用: %s (%s)", info.id, info.path)
                    return info
            pid = self._new_id(data)
            entry = {
                "id": pid,
                "name": self._unique_name(data, (name or "").strip() or Path(norm).name),
                "path": norm,
                "created_at": _now_iso(),
                "last_opened_at": _now_iso(),
            }
            data_root = self.projects_root / pid
            self._make_meta_dir(data_root)
            data["projects"].append(entry)
            data["active"] = pid
            try:
                self._write(data)
            except Exception:
                # 索引没落盘就回滚目录，避免留下"无主的元数据目录"
                shutil.rmtree(data_root, ignore_errors=True)
                raise
            info = self._to_info(entry)
            log.info("工作空间新增: %s 名称=%s 目录=%s 元数据=%s",
                     info.id, info.name, info.path, data_root)
            return info

    def rename(self, project_id: str, name: str) -> ProjectInfo:
        """重命名（只改展示名；元数据目录名 = id，不随名称变化）。

        default 拒绝；与其它空间重名拒绝（显式输入不静默加后缀 —— 用户会以为
        自己改成了想要的名字）。
        """
        pid = str(project_id or "")
        new_name = (name or "").strip()
        if not new_name:
            raise WorkspaceError("工作空间名称不能为空")
        if pid == DEFAULT_PROJECT_ID:
            raise WorkspaceError("默认工作空间不支持重命名")
        with self._lock:
            data = self._load()
            entry = self._find(data, pid)
            if entry is None:
                raise WorkspaceError(f"工作空间不存在：{pid}")
            taken = {e.get("name") for e in data["projects"] if e["id"] != pid}
            if new_name in taken:
                raise WorkspaceError(f"已存在同名工作空间：{new_name}")
            entry["name"] = new_name
            self._write(data)
            log.info("工作空间重命名: %s -> %s", pid, new_name)
            return self._to_info(entry)

    def remove(self, project_id: str) -> ProjectInfo:
        """删除工作空间：**只删元数据目录**（含其中的会话/任务/记忆/回收站），
        真实目录原样保留。

        不可恢复（无回收站），调用方必须先弹确认框；运行中的会话由
        `ws_bridge` 在调用前拦截。
        """
        pid = str(project_id or "")
        if pid == DEFAULT_PROJECT_ID:
            raise WorkspaceError("默认工作空间不可删除")
        with self._lock:
            data = self._load()
            entry = self._find(data, pid)
            if entry is None:
                raise WorkspaceError(f"工作空间不存在：{pid}")
            data_root = self.projects_root / pid
            if data_root.exists():
                shutil.rmtree(data_root)  # 目录不存在（曾被手工删掉）也继续走完索引清理
            data["projects"] = [e for e in data["projects"] if e["id"] != pid]
            if data.get("active") == pid:
                data["active"] = DEFAULT_PROJECT_ID
            self._write(data)
            info = self._to_info(entry)
            log.info("工作空间删除: %s 名称=%s（真实目录保留：%s）",
                     pid, info.name, info.path)
            return info

    # ═══════════════════════════════════════════════════════════
    #  内部
    # ═══════════════════════════════════════════════════════════
    def _load(self) -> dict:
        """读索引并**归一化**：永不抛异常（坏文件备份后重建为 default-only）。

        归一化保证三件事：`projects` 一定是 list、`default` 一定在且排第一、
        每条都有合法 `id`。这样上层（含前端）永远不必判空。
        """
        raw: object = None
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass  # 首次启动：下面按空索引归一化，由 _write 落盘
        except (OSError, ValueError) as e:
            log.error("projects.json 无法解析，备份后重建：%s", e)
            self._backup_broken()
        if not isinstance(raw, dict):
            raw = {}
        entries = raw.get("projects")
        if not isinstance(entries, list):
            entries = []
        cleaned: list[dict] = []
        seen: set[str] = set()
        for e in entries:
            if not isinstance(e, dict):
                continue
            pid = str(e.get("id") or "").strip()
            if not pid or pid in seen or pid == DEFAULT_PROJECT_ID:
                continue
            seen.add(pid)
            cleaned.append({
                "id": pid,
                "name": str(e.get("name") or pid),
                "path": str(e["path"]) if e.get("path") else None,
                "created_at": e.get("created_at"),
                "last_opened_at": e.get("last_opened_at"),
            })
        # default 恒在且恒第一（索引被手改删掉也要补回，否则老会话全成孤儿）
        cleaned.sort(key=lambda e: (e.get("last_opened_at") or ""), reverse=True)
        cleaned.insert(0, _default_entry())
        active = str(raw.get("active") or DEFAULT_PROJECT_ID)
        if active != DEFAULT_PROJECT_ID and active not in {e["id"] for e in cleaned}:
            active = DEFAULT_PROJECT_ID
        return {"version": INDEX_VERSION, "active": active, "projects": cleaned}

    def _write(self, data: dict) -> None:
        """整份原子写回索引（临时文件 + `os.replace`）。"""
        data["version"] = INDEX_VERSION
        data["updated_at"] = _now_iso()
        self.projects_root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_name(
            f".{self.index_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except OSError as e:
            log.error("projects.json 写入失败: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _backup_broken(self) -> None:
        """把损坏的索引挪成 `.bak`（保留现场供排查），失败也不阻断重建。"""
        try:
            self.index_path.replace(self.index_path.with_suffix(".json.bak"))
        except OSError as e:
            log.warning("projects.json 备份失败: %s", e)

    def _find(self, data: dict, project_id: str) -> Optional[dict]:
        for entry in data["projects"]:
            if entry["id"] == project_id:
                return entry
        return None

    def _to_info(self, entry: dict) -> ProjectInfo:
        pid = str(entry["id"])
        path = entry.get("path") or None
        return ProjectInfo(
            id=pid,
            name=str(entry.get("name") or pid),
            path=path,
            created_at=entry.get("created_at"),
            last_opened_at=entry.get("last_opened_at"),
            system=(pid == DEFAULT_PROJECT_ID),
            exists=self._probe(path),
        )

    @staticmethod
    def _probe(path: Optional[str]) -> bool:
        """真实目录是否可达（被删/改名/移动硬盘未挂载 → False）。

        default（path 为空）恒 True：它本来就没有真实目录，不该显示"路径不可用"。
        """
        if not path:
            return True
        try:
            return Path(path).is_dir()
        except OSError:
            return False

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        """按 realpath 比较（软链/相对路径/大小写差异下仍判为同一目录）。"""
        try:
            return os.path.realpath(a) == os.path.realpath(b)
        except OSError:
            return a == b

    def _validate_dir(self, real_path: str) -> str:
        """校验用户选定的目录，返回规范化后的绝对路径。不合法即抛 `WorkspaceError`。"""
        raw = str(real_path or "").strip()
        if not raw:
            raise WorkspaceError("未选择工作目录")
        try:
            path = Path(raw).expanduser().resolve()
        except OSError:
            path = Path(os.path.abspath(raw))
        if not path.is_dir():
            raise WorkspaceError(f"目录不存在或不是目录：{path}")
        home = AIGENT_HOME.resolve()
        # 拒绝应用自身数据目录：否则用户的 .chathistory/.tasks 会被写进
        # ~/.aigent 内部，既违背"元数据目录统一在 projects/ 下"的口径，
        # 也会让 default 的既有数据被当成本空间数据。
        if path == home or home in path.parents:
            raise WorkspaceError("不能把 ~/.aigent 内部目录选为工作空间")
        if not os.access(path, os.W_OK):
            raise WorkspaceError(f"目录不可写，请换一个目录：{path}")
        return str(path)

    def _new_id(self, data: dict) -> str:
        """生成不重复的 `ws` + 10 位 base62 短码（同时避开已有目录名）。"""
        taken = {e["id"] for e in data["projects"]}
        for _ in range(_MAX_ID_ATTEMPTS):
            pid = PROJECT_ID_PREFIX + "".join(
                secrets.choice(BASE62_CHARS) for _ in range(PROJECT_ID_LEN)
            )
            if pid not in taken and not (self.projects_root / pid).exists():
                return pid
        raise WorkspaceError("生成工作空间短码失败，请重试")

    def _unique_name(self, data: dict, base: str) -> str:
        """同名目录去重：`frontend` / `frontend (2)` / `frontend (3)`…"""
        base = (base or "").strip() or "未命名"
        taken = {e.get("name") for e in data["projects"]}
        if base not in taken:
            return base
        for i in range(2, _MAX_NAME_SUFFIX + 1):
            candidate = f"{base} ({i})"
            if candidate not in taken:
                return candidate
        raise WorkspaceError(f"同名工作空间过多：{base}")

    def _make_meta_dir(self, data_root: Path) -> None:
        """建元数据目录 + 全部同构子目录（与 default 一致）。"""
        data_root.mkdir(parents=True, exist_ok=True)
        for name in WORKSPACE_SUBDIRS:
            (data_root / name).mkdir(parents=True, exist_ok=True)


# ── 进程内单例（ws_bridge 与工具层共用一份）────────────────────────────
_registry: Optional[WorkspaceRegistry] = None
_registry_guard = threading.Lock()


def get_registry() -> WorkspaceRegistry:
    global _registry
    with _registry_guard:
        if _registry is None:
            _registry = WorkspaceRegistry()
        return _registry


def set_registry(registry: Optional[WorkspaceRegistry]) -> None:
    """替换单例（测试注入临时目录用；传 None 则下次 get 重建）。"""
    global _registry
    with _registry_guard:
        _registry = registry
