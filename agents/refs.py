#!/usr/bin/env python3
"""
refs.py - 工作空间「引用文件/文件夹」（@-mention）：扁平列目录、中性引用块、发送前展开（新增）

设计见 docs/frontend/13-引用文件与文件夹（@-mention）.md。本模块是桌面端「@ 引用」
功能的后端实现，**叶子模块**：只依赖标准库 + `logger`，**禁止 import
attachments / agent_full_v2 / session_manage**（会与引擎形成循环依赖）。

════════════════════════════════════════════════════════════════════════
与「附件」（attachments.py）的分工 —— 本模块存在的全部理由
════════════════════════════════════════════════════════════════════════

| | 附件 | 引用（本模块） |
|---|---|---|
| 存储 | 复制副本到 `.attachments/` | **零复制、零存储** |
| 位置 | 工作空间**之外**（`~/.aigent/...`） | 工作空间**之内**（沙箱根之下） |
| 模型可读性 | 副本被 `safe_path` 拒绝 → 只能预注入内容 | `run_read` 直接可读 → 只给路径 |
| 注入内容 | 全文（有 30000/60000 字封顶） | **只有路径与类型** |
| 内容时效 | 快照，原件改动不影响 | 现读；但**引用会失效**（删/改名/移走） |

两条通道**协议字段、块类型、模块、CSS 前缀、文档章节全部独立**。引用唯一复用的
是附件的表现层约定（点对点应答信封 / chip 四态 / 「账本块 + 发送边界展开 + harvest」
这套结构），不碰它的 stage / migrate 链路。

════════════════════════════════════════════════════════════════════════
两层表示（与附件同构）
════════════════════════════════════════════════════════════════════════

- **账本形态**（磁盘 / jsonl）：`{"type":"ref","ref":{"path","name","is_dir","project_id"}}`
  **只有路径与元数据**，没有任何文件字节。由 ws_bridge 组装并落盘；历史回放时
  `ws_bridge._history_to_ui` 用 `harvest_refs` 取回 UI 需要的列表。
- **请求体形态**（发给 LLM）：**一个**说明文本块（所有引用合并成一段路径清单 +
  「内容不在上下文中，需要时用 run_read」的硬约束）。由 `expand_ref_blocks_for_model`
  在**发送边界**（`Agent._model_messages`）现算，**不回写 history**。

提示词约束刻意写在**这条注入块里**而不是系统提示词：系统提示词有「逐字节相同 →
跨会话共享前缀缓存」的硬约束，为一个只在带引用消息上才成立的约束去污染所有会话的
L0 冻结段不划算。

════════════════════════════════════════════════════════════════════════
零行为变化（硬保证）
════════════════════════════════════════════════════════════════════════

`attach_ref_blocks(content, [])` 与 `expand_ref_blocks_for_model(message)`
在「无引用」时**返回传入的同一个对象**（不是等值副本）。无附件的会话请求体因此
与改造前**逐字节一致** —— 这条由结构保证，而不是靠"新增代码恰好没副作用"。

════════════════════════════════════════════════════════════════════════
附带的第二个用途：右栏「文件预览」（2026-09-23，docs/frontend/19 / 21）
════════════════════════════════════════════════════════════════════════

`read_workspace_file()` 让前端右栏读工作空间内单个文件的内容。它放在这里而不是
新开模块，唯一理由是**沙箱判定只能有一份**：`resolve_within()` 已经是「前端线索
→ 工作空间内路径」的既定口径（`safe_path` 的镜像），预览再写一套就一定会漂移。
引用与预览的分工是「列路径 / 读内容」，共用同一条判定链。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections import deque
from pathlib import Path

from logger import get_logger

log = get_logger("refs")

# jsonl content 里的块类型。与 attachments.ATTACHMENT_BLOCK_TYPE 平级、互不相干。
REF_BLOCK_TYPE = "ref"

# ── 默认忽略清单 ──────────────────────────────────────────────────
# 按**目录名整棵剪枝**：命中即不进入该目录，也不列出该目录本身。
# 依据是「列出来对用户没有价值，反而会淹没有效项」：依赖目录、构建产物、
# 版本控制内部结构、各种缓存。用户可在 ~/.aigent/config/config.json 用
# REF_LIST_IGNORE（逗号分隔）追加，但**不能**移除默认项。
DEFAULT_IGNORE_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "out", "target", ".next", ".nuxt",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", "coverage", ".tox", ".eggs",
    # 我们自己写进工作空间的派生目录（2026-09-21）：`run_read` 读 PDF 时把页图
    # 栅格化到 `<workdir>/.aigent/pages/<key>/`（见 doc_convert.tool_cache_dir）。
    # 它**对用户没有任何引用价值**，列出来只会淹没真实文件；而引用候选里一旦
    # 出现它，用户还能选中引用一个缓存图，那更是荒谬。
    ".aigent",
})

# 文件级忽略（只跳过这些具体文件名，不做模式匹配）
DEFAULT_IGNORE_FILES = frozenset({".DS_Store"})


# ══════════════════════════════════════════════════════════════════
#  可调参数（环境变量 / ~/.aigent/config/config.json，读在调用点而非导入点）
# ══════════════════════════════════════════════════════════════════
# 与 attachments 同款：写成函数而非常量，因为 config.load() 合并配置进 os.environ
# 的时机可能晚于本模块被导入（agent_full_v2 → ws_bridge → refs 的导入链），
# 常量会在配置生效前被固化。

def _int_env(key: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def max_entries() -> int:
    """一次列目录返回的条目数上限（默认 3000）。

    上限存在的理由是**体感与性能**而不是安全：工作空间里一个 frontend/node_modules
    就有上千个文件，不做裁剪列表会长到无法浏览、遍历也会明显变慢。超出时
    `truncated=True`，前端在列表底部提示"已截断"。
    """
    return _int_env("REF_LIST_MAX_ENTRIES", 3000, minimum=1)


def ignore_dirs() -> frozenset[str]:
    """生效的忽略目录名集合 = 默认清单 ∪ `REF_LIST_IGNORE`（逗号分隔）。"""
    raw = os.environ.get("REF_LIST_IGNORE") or ""
    extra = {name.strip() for name in raw.split(",") if name.strip()}
    return DEFAULT_IGNORE_DIRS | extra


def file_preview_max_bytes() -> int:
    """单个文件预览的字节上限（默认 512KB），超过则**完全不读**。

    不读而不是"读前 512KB"：预览区只有一屏，把 200MB 的日志读进内存再截断，
    代价全在服务端而用户什么也没多看到。
    """
    return _int_env("FILE_PREVIEW_MAX_BYTES", 512 * 1024, minimum=1)


def file_preview_max_lines() -> int:
    """单个文件预览的行数上限（默认 5000），超过则截断并置 `truncated`。

    与字节上限是**两个独立的降级档**：字节超限 = 整屏替换（什么都没读到），
    行数超限 = 内容照常渲染 + 顶部横幅（读到了，尾部砍了）。
    """
    return _int_env("FILE_PREVIEW_MAX_LINES", 5000, minimum=1)


# ══════════════════════════════════════════════════════════════════
#  路径安全（语义镜像 tools.safe_path）
# ══════════════════════════════════════════════════════════════════

def resolve_within(workdir, raw) -> Path | None:
    """把前端传来的路径规范化到工作空间内；越界 / 非法一律返回 None。

    与 `tools.safe_path` **同一判定口径**（`resolve()` 后用 `is_relative_to(base)`
    判定），但输入形态不同：safe_path 收的是工具参数、由模型提供，可以直接抛
    `ValueError`；这里收的是前端线索，**必须吞掉一切异常返回 None**（一条越界引用
    不该打死整轮发送）。

    相对路径按工作空间根解析（与 run_read 一致）；`~` 会展开。
    """
    try:
        base = Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        # resolve() 同时解掉符号链接 —— 链出工作空间的路径会在这里现出原形并被拒绝
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != base and not resolved.is_relative_to(base):
        return None
    return resolved


def is_under(workdir, raw) -> bool:
    """`resolve_within` 的布尔快捷式（测试与 ws_bridge 用）。"""
    return resolve_within(workdir, raw) is not None


# ══════════════════════════════════════════════════════════════════
#  扁平列目录（BFS + 忽略清单 + 上限）
# ══════════════════════════════════════════════════════════════════

def list_workspace(workdir, *, limit: int | None = None,
                   ignore: frozenset[str] | None = None) -> dict:
    """扁平列出工作空间内的所有目录与文件（**无层级**）。

    返回 `{"workdir", "items", "truncated", "total_seen", "skipped"}`，
    `items` 每项为 `{"path", "name", "type"}`（type 取 `"dir"` / `"file"`）。
    前端用 `path` 去掉 `name` 即可还原所在目录，故**不重复发 dir 字段**。

    **算法取舍：BFS 而非 DFS。** 平铺列表 + 条数上限的组合下，DFS 会让第一个
    庞大子目录吃满全部名额，同级目录在列表里根本看不见；BFS 保证同一深度先铺满，
    截断时被砍掉的是最深条目，用户视野内的浅层条目永远完整。

    其它约定：
    - 每个目录内部先目录后文件，各自按 `name.casefold()` 升序 → 结果确定、可比对。
    - **符号链接一律跳过**：防环；且链出工作空间的 symlink 交给模型去读必然被
      `safe_path` 拒绝，不该由我们主动推荐一次注定失败的尝试。
    - 权限错误 / 并发删除：逐目录吞 `OSError` 并计入 `skipped`，**不中断整轮**。
    - **绝不抛异常**（本函数在 `asyncio.to_thread` 里跑，异常会让前端一直转圈）。
    """
    cap = limit if limit is not None else max_entries()
    ignore_names = ignore if ignore is not None else ignore_dirs()

    result: dict = {
        "workdir": "",
        "items": [],
        "truncated": False,
        "total_seen": 0,
        "skipped": 0,
    }
    if not str(workdir or "").strip():
        # 空 workdir 绝不能被当成"当前目录"：Path("") == Path(".")，
        # resolve() 会落到进程 cwd（Electron 拉起后端时 = 仓库目录），
        # 于是"列个工作空间"会变成把整个仓库列出来。宁可返回空。
        return result
    try:
        base = Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return result
    result["workdir"] = str(base)

    items: list[dict] = []
    total_seen = 0
    skipped = 0
    truncated = False
    queue: deque[Path] = deque([base])

    while queue and not truncated:
        current = queue.popleft()
        try:
            with os.scandir(current) as scan:
                entries = list(scan)
        except OSError:
            skipped += 1
            continue

        subdirs: list = []
        plain_files: list = []
        for entry in entries:
            total_seen += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in ignore_names:
                        continue
                    subdirs.append(entry)
                elif entry.is_file(follow_symlinks=False):
                    if entry.name in DEFAULT_IGNORE_FILES:
                        continue
                    plain_files.append(entry)
            except OSError:
                skipped += 1
                continue

        subdirs.sort(key=lambda e: e.name.casefold())
        plain_files.sort(key=lambda e: e.name.casefold())

        for kind, group in (("dir", subdirs), ("file", plain_files)):
            for entry in group:
                if len(items) >= cap:
                    truncated = True
                    break
                items.append({
                    "path": str(Path(entry.path)),
                    "name": entry.name,
                    "type": kind,
                })
            if truncated:
                break

        if not truncated:
            for entry in subdirs:
                queue.append(Path(entry.path))

    result["items"] = items
    result["truncated"] = truncated
    result["total_seen"] = total_seen
    result["skipped"] = skipped
    return result


# ══════════════════════════════════════════════════════════════════
#  读单个文件（右栏「文件预览」，docs/frontend/19）
# ══════════════════════════════════════════════════════════════════

# 二进制嗅探的头部长度：足够覆盖所有已知容器格式的魔数区，又不值得再大
_BINARY_SNIFF_BYTES = 8192

# ── 预览类型（前端据此选渲染分支，2026-09-23，docs/frontend/21）─────────────
# 为什么由**后端**分类、而不是前端看扩展名：沙箱判定与文件读取都已经在这里，
# 再来一份扩展名表就是第二处真相（与「`resolve_within` 只留一份」同理）；而且
# 魔数兜底只有后端做得到 —— 它本来就要读头部字节。
PREVIEW_KIND_TEXT = "text"
PREVIEW_KIND_IMAGE = "image"
PREVIEW_KIND_PDF = "pdf"
PREVIEW_KIND_OFFICE = "office"
PREVIEW_KIND_BINARY = "binary"

# `.svg` 归图片：`<img>` 加载 SVG **不执行脚本**（`<object>` / `<iframe>` 才会），
# 所以这条路是安全的，且渲染出来比让用户看源码有价值。
#
# ⚠️ `.html` / `.htm` **刻意不在任何清单里**，必须继续走文本预览。把工作空间里的
# HTML 当网页渲染 = 用户点开一个文件就执行任意同源脚本，CSP 也拦不住（`default-src
# 'self'` 恰恰放行同源脚本）。这是本模块唯一一处"少支持一种格式换来安全"的取舍。
IMAGE_PREVIEW_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".avif",
    ".tif", ".tiff",
})
# 能交给 LibreOffice 转 PDF 的格式。`.doc/.xls/.ppt`（老二进制格式）与
# `.odt/.ods/.odp` 只有在 LibreOffice 在场时才能预览 —— 文本降级路径
# （`doc_convert.convert_office`）只认 docx/xlsx/pptx 三种。
OFFICE_PREVIEW_EXTS = frozenset({
    ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp",
})


def _stream_max_bytes_default() -> int:
    """图片 / PDF / 转换产物交给前端协议直读时的字节上限（默认 64MB）。

    比文本的 512KB 宽得多，因为**代价根本不同**：文本要进 JSON、进而要进
    WebSocket 帧（1 MiB 上限，故必须卡死在 512KB）；这三类走 `aigent-file://`
    直读磁盘、**根本不进帧**，卡上限只为防一个 2GB 的 PDF 把主进程读爆内存。
    """
    return 64 * 1024 * 1024


def stream_max_bytes() -> int:
    return _int_env("FILE_STREAM_MAX_BYTES", _stream_max_bytes_default(),
                    minimum=1024)


def office_convert_timeout() -> int:
    """LibreOffice 单次转换的超时秒数（默认 90）。

    冷启动要 3~8s，大文档更久；但它是**同步阻塞**在 `to_thread` 里的，不设上限
    会让前端一直转圈，所以宁可超时失败降级为文本。
    """
    return _int_env("OFFICE_CONVERT_TIMEOUT", 90, minimum=5)


def _sniff_kind(head: bytes) -> str:
    """魔数兜底 —— 只在**扩展名不认识**时才会被问到。

    为什么不反过来让魔数优先：扩展名撒谎（`.txt` 里装 PDF）远比"扩展名正确却
    识别不出"罕见；而先信魔数会让一个正常的 `.svg`（XML 开头，不带任何图片魔数）
    落到文本分支。所以顺序是扩展名优先、魔数兜底，不是二选一。
    """
    if head.startswith(b"%PDF-"):
        return PREVIEW_KIND_PDF
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return PREVIEW_KIND_IMAGE
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return PREVIEW_KIND_IMAGE
    return ""


def classify_preview_kind(path, head: bytes = b"") -> str:
    """路径（可带头部字节）→ 预览类型；返回 `""` 表示"按文本/二进制处理"。

    扩展名清单里没有的格式一律回落 `""`，**刻意不猜**：一个 `.xyz` 文件既可能是
    文本也可能是二进制，交给下面原有的 `\\x00` 嗅探判定比在这里硬编一张更长的
    表更可靠。
    """
    ext = Path(str(path or "")).suffix.lower()
    if ext in IMAGE_PREVIEW_EXTS:
        return PREVIEW_KIND_IMAGE
    if ext == ".pdf":
        return PREVIEW_KIND_PDF
    if ext in OFFICE_PREVIEW_EXTS:
        return PREVIEW_KIND_OFFICE
    return _sniff_kind(head)


def _preview_result(path: str, name: str = "", *, reason: str = "",
                    size: int = 0, mtime: float = 0.0,
                    binary: bool = False, too_large: bool = False,
                    truncated: bool = False, lines: int = 0,
                    text: str = "", encoding: str = "",
                    kind: str = PREVIEW_KIND_TEXT, pdf_path: str = "",
                    office_hint: str = "") -> dict:
    """预览回执的统一形状（**所有**分支都从这一个构造函数出去）。

    集中构造的理由是前端只需要认一套字段：它不区分"读失败"与"读成功但降级"，
    只按 `reason` / `binary` / `too_large` / `truncated` 四个标志分派五态。

    `kind` 是 2026-09-23 新增的**渲染分支选择器**（docs/frontend/21）：前端不再
    靠扩展名猜自己该用哪条渲染路径，后端说是什么就是什么。`pdf_path` 只在
    `kind="office"` 且转换成功时非空（指向 `.aigent/office-preview/` 下的产物，
    它同样在工作空间内 → 协议读得到）；`office_hint` 是 Office 降级时给用户看的
    原因（"没装 LibreOffice"这类），成功时为空。
    """
    return {
        "path": path,
        "name": name,
        "size": size,
        "mtime": mtime,
        "encoding": encoding,
        "binary": bool(binary),
        "too_large": bool(too_large),
        "truncated": bool(truncated),
        "lines": int(lines),
        "text": text,
        "reason": reason,
        "kind": kind or PREVIEW_KIND_TEXT,
        "pdf_path": pdf_path,
        "office_hint": office_hint,
    }


def _decode_text(data: bytes) -> tuple[str, str]:
    """字节 → 文本，返回 `(text, encoding)`。

    顺序是**刻意**的：UTF-8 严格优先（正确编码的文件得到零损伤结果），失败再试
    GB18030（中文环境最常见的另一种编码，且它是 UTF-8 的超集式兼容解码器，
    对绝大多数中文文件能给出正确结果），最后才 utf-8 + `errors="replace"` 兜底。
    直接用 replace 会把"编码判断错误"伪装成"文件里有乱码"，用户没法区分。
    """
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"
    except (UnicodeDecodeError, LookupError):
        pass
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


# ══════════════════════════════════════════════════════════════════
#  Office 预览：LibreOffice 转 PDF（**只运行时探测，绝不打包**）
# ══════════════════════════════════════════════════════════════════
#
# 为什么是 LibreOffice，而不是前端引一个 JS 渲染库（2026-09-23 决策）：
# - **程序体积 0 增量**。LibreOffice.app 本机实测 794MB，随包分发会把安装包从
#   约 100MB 推到约 900MB（9 倍，不可接受）；而纯 JS 三件套（docx / excel / pptx
#   预览库）gzip 后也要 +0.7~1MB，且保真度明显不如真渲染 —— 公式、图表、分页、
#   字体都会打折。既然装了 LO 的机器能拿到高保真，就没必要为"没装的机器"再引
#   一份降级实现。
# - 所以策略是：**探测到就用，探测不到就降级为文本抽取**（`convert_office`，与
#   附件通道、工具读文档共用同一段代码，口径一致）。
#
# 这与 `doc_convert.py` 里那句"LibreOffice 渲染（~800MB）已被否决"不矛盾：那句
# 说的是**给模型读内容**（模型要的是文本，渲染无收益还多几百 MB 依赖）；这里是
# **给人看预览**（要的正是版式与图表）。同一份二进制，两个场景结论相反。

OFFICE_PREVIEW_DIRNAME = ".aigent/office-preview"

# `.gitignore` 内容与 `doc_convert` 里那份同款：本模块是叶子模块（见文件头），
# 不为一行常量去 import 它。
_CACHE_GITIGNORE = "*\n"

_OFFICE_DEGRADED_HINT = "未检测到 LibreOffice，已降级为文本抽取：版式、图片与图表未包含"

# 进程内缓存。LibreOffice 探测是若干次 stat，单次很便宜，但用户会连着点十几个
# 文件；而且"未安装"同样是稳定结论（不会装着装着就装上了）。
_LIBREOFFICE_CACHE: dict[str, str] = {}


def _libreoffice_candidates() -> list[str]:
    """候选路径，按优先级：平台默认安装位置 > PATH。"""
    listed = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",              # macOS
        "/usr/bin/soffice", "/usr/local/bin/soffice",
        "/opt/libreoffice/program/soffice",                                   # Linux
        r"C:\Program Files\LibreOffice\program\soffice.exe",                  # Windows
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            listed.append(found)
    return listed


def libreoffice_binary() -> str:
    """可用的 LibreOffice 可执行文件路径；没装返回 `""`。

    非标准安装位置的唯一出口是环境变量 `LIBREOFFICE_PATH`（显式指定优先于一切
    探测）—— 没有它，绿色版 / Homebrew 装的 LO 就只能靠运气被 PATH 找到。
    """
    if "bin" in _LIBREOFFICE_CACHE:
        return _LIBREOFFICE_CACHE["bin"]
    result = ""
    explicit = str(os.environ.get("LIBREOFFICE_PATH") or "").strip()
    if explicit:
        try:
            if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
                result = explicit
        except OSError:
            result = ""
    if not result:
        for candidate in _libreoffice_candidates():
            try:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    result = candidate
                    break
            except OSError:
                continue
    _LIBREOFFICE_CACHE["bin"] = result
    if result:
        log.info("Office 预览：检测到 LibreOffice %s", result)
    else:
        log.info("Office 预览：未检测到 LibreOffice，Office 文件将降级为文本抽取")
    return result


def office_pdf_cache(workdir, src) -> Path | None:
    """转换产物的落盘目录：`<workdir>/.aigent/office-preview/<key>/`。

    缓存键含 `mtime_ns` 与 `size`（与 `doc_convert._cache_key` 同一取舍）：源文件
    一改键就变，旧目录再无人引用 → **不需要精确 GC**，靠 TTL 清剪顺手做即可。

    放在工作空间**之内**不是图方便：`aigent-file://` 协议只放行后端给过的路径，
    而产物必须被那个协议读到；落到 OS 临时目录就得再开一条协议白名单。代价是
    污染工作空间，靠「`.aigent` 已在 `DEFAULT_IGNORE_DIRS` 里 + 目录内 .gitignore
    + TTL 清剪」收窄 —— 与 `doc_convert.tool_cache_dir` 完全同款，不是新例外。
    """
    try:
        resolved = Path(src).resolve()
        st = resolved.stat()
        base = Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    raw = "|".join((str(resolved), str(st.st_mtime_ns), str(st.st_size)))
    key = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
    return base / OFFICE_PREVIEW_DIRNAME / key


def _ensure_cache_gitignore(cache_root: Path) -> None:
    """保证 `<workdir>/.aigent/.gitignore` 存在（内容 `*`）。

    不写的话，用户的工作空间若是 git 仓库，点开一个 Word 就会在 `git status` 里
    冒出一串未跟踪文件；去改用户自己的 `.gitignore` 更越界。写失败一律吞掉 ——
    预览能不能用远重于这份卫生。
    """
    try:
        agent_dir = cache_root.parent
        agent_dir.mkdir(parents=True, exist_ok=True)
        target = agent_dir / ".gitignore"
        if not target.exists():
            target.write_text(_CACHE_GITIGNORE, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - 写不了就算了
        log.debug("写 .aigent/.gitignore 失败: %s", exc)


def _stderr_tail(raw, limit: int = 160) -> str:
    """把转换进程的 stderr 压成一行人话。

    取**最后一行非空**而不是第一行：LibreOffice 的 stderr 开头全是
    `javaldx: Could not find a Java runtime` 这类与本次失败无关的噪音，真正原因
    （"source file could not be loaded"）总在末尾。
    """
    try:
        text = (raw or b"").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 解码失败不值得影响预览
        return ""
    for line in reversed([ln.strip() for ln in text.splitlines()]):
        if line:
            return line[:limit]
    return ""


def convert_office_to_pdf(workdir, src) -> tuple[str, str]:
    """Office → PDF，返回 `(pdf 绝对路径, 失败原因)`；成功时原因为空串。

    **绝不抛异常** —— 与 `read_workspace_file` 同契约：它在 `asyncio.to_thread`
    里跑，异常会让前端一直转圈；而且"没装 LibreOffice"是**常规降级**、不是错误。

    独立 profile（`-env:UserInstallation=`）是必须的，不是洁癖：LibreOffice 默认
    profile 带锁文件，用户自己正开着 Writer 时再被我们拉起第二个实例，会失败或
    静默卡住 —— 表现为"点开 Word 一直转圈"。
    """
    binary = libreoffice_binary()
    if not binary:
        return "", _OFFICE_DEGRADED_HINT

    source = Path(src)
    cache = office_pdf_cache(workdir, source)
    if cache is None:
        return "", "无法定位转换产物的缓存目录"

    target = cache / f"{source.stem}.pdf"
    try:
        if target.is_file() and target.stat().st_size > 0:
            return str(target), ""      # 命中缓存：秒开
    except OSError:
        pass

    _ensure_cache_gitignore(cache)
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "", f"无法创建转换缓存目录：{exc}"

    profile = cache / "lo-profile"
    cmd = [
        binary, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to", "pdf", "--outdir", str(cache), str(source),
    ]
    timeout = office_convert_timeout()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("LibreOffice 转换超时(%ss): %s", timeout, source)
        return "", f"转换超时（超过 {timeout}s），文件可能过大"
    except OSError as exc:
        log.warning("LibreOffice 启动失败: %s: %s", type(exc).__name__, exc)
        return "", f"无法启动转换进程：{exc}"

    try:
        produced = target.is_file() and target.stat().st_size > 0
    except OSError:
        produced = False
    if not produced:
        detail = _stderr_tail(proc.stderr)
        log.warning("LibreOffice 转换未产出文件 (rc=%s): %s %s",
                    proc.returncode, source, detail)
        return "", f"转换失败{('：' + detail) if detail else ''}"
    log.info("Office 预览转 PDF 成功: %s → %s", source.name, target.name)
    return str(target), ""


def _office_text_fallback(path: str, cap_chars: int) -> str:
    """LibreOffice 缺位时的降级：抽正文与表格结构（无版式）。抽不出来返回 `""`。

    **局部 import `doc_convert`**，不放文件头：本模块是叶子模块，顶层 import 会
    给每次 `import refs` 都挂上 doc_convert 的导入代价，而这条路径只在真的预览
    一个没装 LO 的 Office 文件时才走到。`doc_convert` 不在文件头禁止的清单
    （attachments / agent_full_v2 / session_manage）里，也不构成循环依赖。
    """
    ext = Path(path).suffix.lower()
    try:
        from doc_convert import convert_office
    except Exception as exc:  # noqa: BLE001 - 缺库同样只是降级，不该打死预览
        log.warning("加载 doc_convert 失败: %s: %s", type(exc).__name__, exc)
        return ""
    try:
        out = convert_office(path, ext, limit=max(1000, int(cap_chars)))
    except Exception as exc:  # noqa: BLE001 - 抽取失败同样只是降级
        log.warning("Office 文本抽取失败 %s: %s: %s", path, type(exc).__name__, exc)
        return ""
    return str(out.get("markdown") or "")


def read_workspace_file(workdir, raw_path, *, max_bytes: int | None = None,
                        max_lines: int | None = None) -> dict:
    """读取工作空间内单个文件，供右栏预览（**绝不抛异常**）。

    沙箱走 `resolve_within()`，与引用、`safe_path` 同一判定口径 —— 越界、软链
    链出、路径非法一律在第一步被挡下，不存在第二条判定路径。

    返回的 `kind` 决定前端用哪条渲染分支（2026-09-23 多格式预览，docs/frontend/21）：

    | `kind` | 谁来渲染 | `text` |
    |---|---|---|
    | `image` / `pdf` | 主进程 `aigent-file://` 直读磁盘，前端 `<img>` / `<embed>` | 恒空 |
    | `office` | 转出的 PDF 走同一条（`pdf_path`）；转不动则文本抽取 + `office_hint` | 降级时有 |
    | `text` | 前端代码视图（行号 + 等宽） | 有 |
    | `binary` | 平级空态，不是错误 | 恒空 |

    降级层次（前端据此渲染**不同**的状态，见 docs/frontend/19 §5.7）：
    - `reason` 非空 → 读失败（越界 / 不存在 / 是目录 / 无权限），`text` 为空；
    - `binary` → 二进制，`text` 为空（不是错误，是"这类文件本来就不该这样看"）；
    - `too_large` → 超过对应上限（文本 512KB / 图片与 PDF 与产物 64MB），
      **一点内容都没读**，`text` 为空；
    - `truncated` → 读到了但只保留了前 `max_lines` 行，`text` 非空。

    本函数在 `asyncio.to_thread` 里跑，异常会让前端一直转圈 —— 所以内部逐层
    吞异常并转成 `reason` 人话。
    """
    cap_bytes = file_preview_max_bytes() if max_bytes is None else int(max_bytes)
    cap_lines = file_preview_max_lines() if max_lines is None else int(max_lines)

    resolved = resolve_within(workdir, raw_path)
    if resolved is None:
        log.warning("文件预览路径越界或非法: %r", str(raw_path)[:200])
        return _preview_result("", reason="路径不在当前工作空间内")

    path_str = str(resolved)
    name = resolved.name or path_str
    try:
        st = resolved.stat()
    except FileNotFoundError:
        return _preview_result(path_str, name, reason="文件不存在")
    except OSError as exc:
        log.warning("文件预览 stat 失败 %s: %s", path_str, exc)
        return _preview_result(path_str, name, reason="无法访问该文件")

    if os.path.isdir(path_str):
        return _preview_result(path_str, name, reason="这是一个目录，无法预览")
    if not os.path.isfile(path_str):
        return _preview_result(path_str, name, reason="不是普通文件，无法预览")

    base = dict(path=path_str, name=name, size=int(st.st_size), mtime=float(st.st_mtime))

    # ── 类型分派（扩展名优先，魔数兜底）─────────────────────────────────
    # 先只看扩展名；分不出来才读头部 —— 文本/二进制嗅探要的也是同一份头部字节，
    # 所以**最多只读一次**。一个 `.xyz` 文件里装着 PNG 也会在这里被纠正过来。
    kind = classify_preview_kind(path_str)
    head = b""
    if not kind:
        try:
            with open(path_str, "rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
        except OSError as exc:
            log.warning("文件预览读取失败 %s: %s", path_str, exc)
            return _preview_result(**base, reason="读取失败")
        kind = _sniff_kind(head)

    # ── 图片 / PDF：**一个字节的正文都不读** ─────────────────────────────
    # 内容由主进程的 `aigent-file://` 协议直读磁盘（路径在上面已经过完沙箱判定）。
    # 不走这里的原因与附件缩略图同款：图片/PDF 动辄几 MB，base64 进 JSON 会撑爆
    # WebSocket 的 1 MiB 帧上限 —— 那条路从设计上就走不通，不是性能取舍。
    if kind in (PREVIEW_KIND_IMAGE, PREVIEW_KIND_PDF):
        if st.st_size > stream_max_bytes():
            return _preview_result(**base, kind=kind, too_large=True)
        return _preview_result(**base, kind=kind)

    # ── Office：先转 PDF（LibreOffice 在就用），转不动才降级为文本 ────────
    if kind == PREVIEW_KIND_OFFICE:
        pdf_path, why = convert_office_to_pdf(workdir, resolved)
        if pdf_path:
            return _preview_result(**base, kind=PREVIEW_KIND_OFFICE, pdf_path=pdf_path)
        text = _office_text_fallback(path_str, cap_bytes)
        return _preview_result(**base, kind=PREVIEW_KIND_OFFICE, text=text,
                               encoding="utf-8" if text else "", office_hint=why)

    # ── 文本 / 二进制（原有逻辑，只是补上 kind）──────────────────────────
    if st.st_size > cap_bytes:
        return _preview_result(**base, too_large=True)
    if b"\x00" in head:
        return _preview_result(**base, kind=PREVIEW_KIND_BINARY, binary=True)

    try:
        with open(path_str, "rb") as fh:
            # 头部之外的部分在确认不是二进制之后再读，避免为二进制文件白白多读一遍
            rest = fh.read() if st.st_size > len(head) else b""
            data = head + rest
    except OSError as exc:
        log.warning("文件预览读取失败 %s: %s", path_str, exc)
        return _preview_result(**base, reason="读取失败")

    text, encoding = _decode_text(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n")
    truncated = len(parts) > cap_lines
    if truncated:
        parts = parts[:cap_lines]
    text = "\n".join(parts)
    return _preview_result(**base, text=text, encoding=encoding,
                           truncated=truncated, lines=len(parts))


# ══════════════════════════════════════════════════════════════════
#  账本形态：规范化引用记录 + 组装 user 消息 content
# ══════════════════════════════════════════════════════════════════

def normalize_ref(workdir, raw) -> dict | None:
    """前端线索 → **以磁盘为准**的引用记录；越界/非法返回 None。

    `name` 与 `is_dir` **一律重新从磁盘取**，不采信前端字段（前端字段可以伪造，
    伪造不出磁盘）。文件在挑选后消失时仍接受（`is_dir=False`）——用户点过它，
    静默丢弃比留一条"当前不存在"的提示更糟。
    """
    if not isinstance(raw, dict):
        return None
    resolved = resolve_within(workdir, raw.get("path"))
    if resolved is None:
        log.warning("引用路径越界或非法，已丢弃: %r", str(raw.get("path"))[:200])
        return None
    try:
        is_dir = resolved.is_dir()
    except OSError:
        is_dir = False
    return {
        "path": str(resolved),
        "name": resolved.name or str(resolved),
        "is_dir": bool(is_dir),
        "project_id": str(raw.get("project_id") or ""),
    }


def normalize_refs(workdir, raw_refs) -> list[dict]:
    """批量规范化 + 按 path 去重（保留首次出现），供 ws_bridge 一次调用。"""
    if not isinstance(raw_refs, list):
        return []
    seen: set[str] = set()
    records: list[dict] = []
    for raw in raw_refs:
        record = normalize_ref(workdir, raw)
        if record is None or record["path"] in seen:
            continue
        seen.add(record["path"])
        records.append(record)
    return records


def build_ref_block(ref: dict) -> dict:
    """引用记录 → 账本块（落 jsonl 的形态，**只有路径与元数据**）。"""
    return {
        "type": REF_BLOCK_TYPE,
        "ref": {
            "path": str(ref.get("path") or ""),
            "name": str(ref.get("name") or ""),
            "is_dir": bool(ref.get("is_dir")),
            "project_id": str(ref.get("project_id") or ""),
        },
    }


def attach_ref_blocks(content, refs) -> object:
    """把引用块挂到 user 消息 content 上。

    **无引用时返回传入的同一个对象**（零行为变化的硬保证）：
    - content 是 str（无附件）且有引用 → `[text块?, *ref块]`
    - content 是 list（有附件）且有引用 → `[*原块, *ref块]`（新列表，不改原对象）
    """
    if not isinstance(refs, list) or not refs:
        return content
    blocks = [build_ref_block(r) for r in refs if isinstance(r, dict)
              and str(r.get("path") or "").strip()]
    if not blocks:
        return content
    if isinstance(content, str):
        head = [{"type": "text", "text": content}] if content.strip() else []
        return head + blocks
    if isinstance(content, list):
        return list(content) + blocks
    return blocks


def history_has_refs(messages) -> bool:
    """这批消息里是否含有待展开的引用块（结构化短路用）。"""
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and block.get("type") == REF_BLOCK_TYPE
                    and isinstance(block.get("ref"), dict)):
                return True
    return False


def harvest_refs(content) -> list[dict]:
    """从 user 消息 content 里取出引用列表（供 `_history_to_ui` 回放）。

    返回**给前端渲染**的形状 `{"path","name","is_dir"}`。刻意**不做 stat**：
    回放要保持廉价（`_history_to_ui` 拿不到 workdir），且引用是否仍然有效对
    "这轮引用了什么"这个事实没有影响。
    """
    if not isinstance(content, list):
        return []
    found: list[dict] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != REF_BLOCK_TYPE:
            continue
        ref = block.get("ref")
        if not isinstance(ref, dict):
            continue
        path = str(ref.get("path") or "")
        if not path:
            continue
        found.append({
            "path": path,
            "name": str(ref.get("name") or ""),
            "is_dir": bool(ref.get("is_dir")),
        })
    return found


def ref_title_hint(raw_refs) -> str:
    """只有引用、没有正文时的标题兜底（`[引用] a.ts`）。

    通常用不到 —— 胶囊在 text 里序列化出 `@相对路径`，纯引用消息的 text 也非空。
    留它是防御性的：前端将来若改成不往 text 里写 token，首轮不至于得到空标题。
    """
    if not isinstance(raw_refs, list):
        return ""
    for item in raw_refs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            path = str(item.get("path") or "").strip()
            name = path.rsplit("/", 1)[-1] if path else ""
        if name:
            return f"[引用] {name}"
    return ""


# ══════════════════════════════════════════════════════════════════
#  请求体形态：发送边界展开（_model_messages 调用）
# ══════════════════════════════════════════════════════════════════

def _existence_note(path: str) -> str:
    """引用↔磁盘的现实核对：文件在挑选之后被删/改名/移走时，如实标注。

    只写在注入文本里、**不改账本块** —— 历史消息记录的是"当时引用了什么"，
    不该被后来的磁盘状态改写。
    """
    try:
        if Path(path).exists():
            return ""
    except (OSError, ValueError):
        return ""
    return "，当前不存在，可能已被删除或移动"


def _render_ref_text(refs: list[dict]) -> str:
    """把一批引用渲染成**一个**说明文本块（模型可见的全部引用信息）。"""
    lines = ["[用户引用了以下工作空间路径（仅路径与类型，文件内容不在上下文中）]"]
    for ref in refs:
        path = str(ref.get("path") or "")
        if not path:
            continue
        kind = "目录" if ref.get("is_dir") else "文件"
        lines.append(f"- {path}（{kind}{_existence_note(path)}）")
    lines.append("")
    lines.append(
        "需要内容时请用 run_read 按上述绝对路径读取 —— 它按类型自动分派"
        "（文本、图片、PDF、Word/Excel/PPT 都是同一个工具，PDF 会同时给出文本层"
        "与页图）；"
        "需要查看目录内容时用 run_glob 或 bash（工作目录即工作空间根）。"
    )
    lines.append("不要在未读取的情况下猜测、复述或杜撰这些文件的内容。")
    return "\n".join(lines)


def expand_ref_blocks_for_model(message):
    """把一条消息里的引用块展开为**一个**说明文本块（发送边界调用）。

    **契约**：
    - `content` 不是 list、或不含引用块 → **原样返回同一对象**（零行为变化）。
    - 含引用块 → 返回新的 dict；所有引用块被替换成末尾**一个** text 块，
      其余块（正文 / 已展开的附件块）顺序不变。
    - **绝不抛异常**：本函数处在 `_model_messages` 的列表推导里，一旦穿透就会
      让本轮请求体缺一条消息、整轮对话被打死。任何意外都降级为"原样返回"。
    """
    try:
        if not isinstance(message, dict):
            return message
        content = message.get("content")
        if not isinstance(content, list):
            return message

        refs: list[dict] = []
        remaining: list = []
        has_ref = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == REF_BLOCK_TYPE:
                has_ref = True
                ref = block.get("ref")
                # 空路径的块是畸形数据，不计入 refs —— 否则会注入一段只有标题与
                # 指令、却一条路径都没有的空说明（纯噪声）。
                if isinstance(ref, dict) and str(ref.get("path") or "").strip():
                    refs.append(ref)
                continue
            remaining.append(block)
        if not has_ref:
            return message

        if refs:
            remaining.append({"type": "text", "text": _render_ref_text(refs)})
        expanded = dict(message)
        expanded["content"] = remaining
        return expanded
    except Exception as exc:  # noqa: BLE001 — 工具/展开层契约：绝不向上抛
        log.error("expand_ref_blocks_for_model 异常: %s: %s", type(exc).__name__, exc,
                  exc_info=True)
        return message
