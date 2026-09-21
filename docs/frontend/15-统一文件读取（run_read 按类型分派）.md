# 15 · 统一文件读取（`run_read` 按类型分派）

> 状态：**已实施**（2026-09-21）。本文记录一次**工具面收敛 + 能力补齐**：读文件从
> 三个工具合并成一个入口，并让"复合内容文档"（带图表/扫描页的 PDF、Office）在
> **引用通道**上第一次真正读得到内容。
> 相关文档：[12-附件与文件输入](12-附件与文件输入.md)（统一转换层的由来）、
> [13-引用文件与文件夹（@-mention）](13-引用文件与文件夹（@-mention）.md)（受惠通道）、
> [14-工具读图（view_image）](14-工具读图（view_image）.md)（通道未变，入口已并入本文）、
> [03-前后端通信协议](03-前后端通信协议.md)。

***

## 一、概念

### 1.1 它解决什么问题

改造前读文件要模型自己选工具：

| 工具 | 能读 | 读不了 |
| --- | --- | --- |
| `run_read` | 文本 / 代码 | 图片（解码失败）、PDF、Office |
| `run_read_pdf` | PDF 的**文本层**（`fitz.get_text`，默认 5 页 / 每页 3000 字） | PDF 里的**图表、扫描页**（只回一句"无可提取文本，可能为扫描件"） |
| `view_image` | 图片（像素） | 其它一切 |

三个后果，都是实测踩过的：

1. **选错工具常态化**。模型读 PDF 用了 `run_read` → 拿到一句解码错误；读图片用了
   `run_read` → 拿到二进制乱码。每次都要多花一轮。
2. **引用通道对复合内容文档是"假可用"**。`@` 引用一个带图表的 PDF，模型只能拿到
   文本层；`@` 引用 `.docx/.xlsx/.pptx` 更是直接解码失败——而注入块里那句
   "请用对应专用工具"指向的工具**在引用通道里根本不存在**（Office 抽取当时是
   `attachments` 的内部函数，不在工具表内）。
3. **同一个文件，走附件与走引用结果不同**。附件通道有 `doc_convert` 的
   「文本层 + 页图」交错注入，引用通道只有纯文本 —— 同一个 PDF 两条路质量不一。

一句话：**附件的"复合内容"能力从来没有下沉到工具层**。

### 1.2 与真实 Claude Code 的对齐点

Claude Code 的 Read 工具是**一个名字按类型分派**（文本 → 文本、图片 → 像素、
PDF → 每页文本 + 页图），模型没有"选错工具"的机会；Office 文档没有原生解析，
走脚本/技能转换。本方案按同一口径收敛：

| | 改造前 | 改造后 |
| --- | --- | --- |
| 入口 | `run_read` / `run_read_pdf` / `view_image` | **`run_read`**（唯一） |
| 图片 | 需选对工具 | 魔数认出 → 像素进上下文 |
| PDF | 文本层（工具路径） / 文本+页图（附件路径） | **文本层 + 页图**（两条通道同源） |
| Office | 工具路径不可用 | 统一转换层抽「文本 + 表格结构」 |
| 格式→工具的映射表 | 系统提示词 + 引用注入块里各写一份 | **删除**，只留"用 run_read" |

> **注意对齐点的边界**：本文只对齐"读文件"这一件事。引用通道仍然**只给路径**
> （零复制，见 doc 13），不因为本次改造变成"预内联内容"。

***

## 二、设计

### 2.1 分派表

```
run_read(path, limit?, max_pages?)
  │
  ├─ safe_path 失败            → "Error: {越界/敏感路径的既有文案}"
  ├─ 是目录                    → "Error: … 是目录，run_read 只读文件；目录内容请用 run_glob 或 bash（ls）"
  ├─ 不存在                    → "Error: File not found: {path}"
  ├─ 魔数命中图片（sniff_image_mime）
  │                            → 中性图片块（1 张，像素下一跳才编码）
  ├─ .pdf                      → 文本层 + 页图（0..N 张）
  ├─ .docx / .xlsx / .pptx     → 转换层抽「文本 + 表格结构」
  └─ 其余                      → 文本分支（按行 limit 截断；解不出 → 二进制错误串）
```

要点：

- **魔数优先、扩展名兜底**。被改名成 `.txt` 的 PNG 现在走图片分支（改造前它报
  "二进制无法解码"）；后缀是 `.png` 而内容其实是文本的，按内容读成文本。
  判据只看字节 —— 扩展名是用户可以随手改的东西。
- **`limit` 的含义随分支**：文本分支 = 行数上限；Office 分支 = 字符上限；
  PDF 分支不理会它（PDF 由 `ATTACHMENT_TEXT_MAX_CHARS` 封顶 + `max_pages` 控图）。
- **`max_pages` 约束的是页图数量**，文本层永远全量 —— 文本是检索与无视觉模型
  兜底的主通道，不该被图像预算砍掉（这条是 doc 12 §2.7 的既有结论，本次沿用）。
- **绝不抛异常**：整个分派包在一个 try/except 里，任何异常收束为 `"Error: …"`
  字符串。异常穿透会打死整轮 `agent_loop`（本轮 tool_result 全缺）。

### 2.2 PDF 分支：文本层 + 页图

与附件通道**同源**，都走 `doc_convert.convert_pdf`：按「这一页的文本层是否足以
代表本页」决定渲不渲染页图（`_needs_page_image`，判据见 doc 12 §2.7）。差别只有
落盘位置：

| | 附件通道 | 工具通道（本文） |
| --- | --- | --- |
| 页图落在 | `.attachments/<sid>/<att_id>.pages/`（工作空间**之外**） | `<workdir>/.aigent/pages/<key>/`（工作空间**之内**） |
| 谁付 token | 无条件付 | 只为模型真读过的付 |
| 内容时效 | 快照（原件改了也不变） | 现读（每次调用现渲染） |

**工具文本里不出现存储锚点**。`convert_pdf` 产出的 markdown 用 `<!--img:pN-->`
标记页图位置（展开侧靠它切交错块），但那是**存储标记**——写给模型看的文本里
必须译成人话：

```
PDF: spec.pdf，共 12 页。随附页图 3 张（第 3、7、9 页），正文中 `[第 N 页为图像，随附]` 处就是它。
--- 第 1 页 ---
<文本层>
--- 第 3 页 ---
[第 3 页为图像，随附]
…
（第 3 页无文本层，已按图像发送）
```

最后一行来自 `convert_pdf` 的 `warnings`：**"诚实失败"通道必须一并交给模型**，
否则它会以为这几页本来就是空的。页图与文本的对齐靠**页号**（每个图片条目带
`page`），不靠位置猜测。

### 2.3 多图工具结果：一个结果可以带 N 张图

`tool_image` 中性块从"只能装一张"扩成列表：

```jsonc
{ "type": "tool_image",
  "text": "…tool 消息的正文（PDF 的文本层 + 说明）…",
  "images": [ { "path": "…/p3.jpg", "name": "spec.pdf 第 3 页", "mime": "image/jpeg", "page": 3 } ],
  "source": "spec.pdf" }        // 只用于合成消息的头部标签，不进账本块
```

- **写入方只写 `images`**；读取方统一走 `attachments.tool_image_items(value)`
  归一化 —— 它同时认**旧存量数据**的单数 `image` 字段（历史 jsonl 回放不能瞎）。
  归一化只在这一处做，否则每加一个消费点就要各自兼容一次。
- 消费点全部改完：`is_tool_image_result` / `tool_image_block` / `text_view` /
  `build_tool_images_message` / `expand_content_for_model` /
  `context_compact._tool_image_label` / `ws_bridge`（marker 判定，逻辑不变）。
- **tool 消息只承载文本**（Chat Completions 的 tool 消息塞不进图片，见 doc 14
  §2.3），图片本体走紧随其后的**合成 user 消息**；顺序硬约束不变。

### 2.4 图片预算：按"组"计，不按"张"计

`VIEW_IMAGE_MAX_PER_TURN`（默认 8）现在数的是**图片承载的工具结果个数**：

- 一次 PDF 读取是**一个组**，要么整组随附、要么整组丢掉，**绝不切页** ——
  半份页图比什么都不给更危险（模型会以为看全了）；
- 单组内张数由工具自己按 `ATTACHMENT_DOC_MAX_IMAGES`（20）限制，超出的页在
  工具正文里点名（`warnings`）；
- 组被丢掉时给一句可行动的话：`（另有 N 个文件未随附：单轮图片上限 M 个，
  可下一轮继续读取：a.pdf、b.pdf）`。

### 2.5 页图缓存：为什么落在工作空间里

中性图片块只带**路径**，真正的 base64 编码发生在发送边界，而**发送边界不许写盘**
（doc 14 §2.4）。所以渲染必须发生在工具调用期间，产物要活到那一跳请求为止。

- 位置：`<workdir>/.aigent/pages/<key>/<att_id>.pages/pN.jpg`
- key = `sha1(源文件绝对路径 | mtime_ns | size | 渲染参数)[:16]` —— 源文件一变
  key 就变，旧目录成为**无人引用的死文件**，因此不需要精确 GC，只需按
  `TOOL_DOC_CACHE_TTL_SECONDS`（默认 7 天）惰性清剪。
- `.aigent` 同时加进 `refs.DEFAULT_IGNORE_DIRS`：缓存目录**不该出现在 `@` 候选里**
  （列出来只会淹没真实文件，而且用户还能选中引用一张缓存图）。
- 目录内自带内容为 `*` 的 `.gitignore`：用户的工作空间若是 git 仓库，不必去改
  他们自己的 `.gitignore`，我们把这个目录从他们的版本控制里摘出去。

**取舍坦白**：`.aigent/` 是本项目**唯一**往用户工作空间里写派生文件的地方 ——
doc 13「零复制、不污染工作空间」的卖点在这里有一个**明示例外**。收窄手段三条：
`.aigent` 进 `@` 忽略清单、目录内自带 `.gitignore`、按 TTL 清剪。若你要的是
工作空间绝对干净，把落点改成 OS 临时目录即可（取舍见 §4.3）。

### 2.6 不可写工作空间时的降级

工作空间可能是只读挂载 / 权限不对。此时**文本层必须照常拿到**：

- `tool_cache_dir` **不**因为不可写而返回 `None`（那样调用方连文本都拿不到）；
  它只负责定位路径 —— 目录创建在 `convert_pdf` 内部 try 住，失败时置位
  `render_disabled`、记一条 warning（`页图目录不可写，本次只提取文本层`）、
  **继续把文本层走完**。
- 工具侧的 `None` 分支只对应"连路径都算不出来"（workdir 为空 / 不可解析），
  那时回一句 `Error: 无法解析页图缓存目录…`。

### 2.7 Office：抽取上移到转换层

`_extract_docx` / `_extract_xlsx` / `_extract_pptx`、`_clip`、`empty_note`、
`EMPTY_NOTE_PREFIX` / `text_is_empty_note` 从 `attachments.py` **上移到
`doc_convert.py`**（叶子模块，第三方库仍在函数内懒加载），新增公开函数：

```python
def convert_office(src, ext, *, limit) -> dict
    # {"markdown","text_truncated","pages","images":[],"tables","converter","warnings"}
```

- `attachments.extract_text` 变成**转发**（签名不变），附件侧 66 例测试与公开 API
  不动；`EMPTY_NOTE_PREFIX` / `text_is_empty_note` 从转换层再导出。
- **两条通道因此共用同一段抽取代码**：同一个 xlsx 无论"上传"还是"`@` 引用"，
  结果质量一致。
- `converter` 标签从 `fallback_text` 改为 **`office_text`** —— 它已经不"回落"了，
  继续叫回落是把"哪条路跑过"这个排障信息说错。

**诚实声明（新增）**：非空正文末尾追加

```
（本格式仅提取文本与表格结构；图表、图片、版式未包含）
```

被截断时再加 `（已截断至 N 字符，完整内容请用 bash 或脚本读取原文件）`；
xlsx 的工作表头改成 `--- 工作表: X（M 行）---`，让模型知道这张表是 50 行还是
5000 行、下面是不是被截过。**为什么要有这行声明**：不给的话，模型会把"抽到的
文本"当成"这份文档的全部"，于是对着一张带图表的 Excel 言之凿凿地答错。

### 2.8 引用注入块：路由表消失

`refs._render_ref_text` 从"PDF 用 run_read_pdf、图片用 view_image"改成：

```
需要内容时请用 run_read 按上述绝对路径读取 —— 它按类型自动分派（文本、图片、PDF、
Word/Excel/PPT 都是同一个工具，PDF 会同时给出文本层与页图）；需要查看目录内容时用
run_glob 或 bash（工作目录即工作空间根）。
```

这是本次改造**最直接的用户可见收益**：引用通道不再需要维护一张"格式 → 工具"的
映射表，而那张表正是"模型选错工具"的温床。

### 2.9 退役两个工具名的波及面

`run_read_pdf` / `view_image` 作为工具名退役，方法体保留为私有实现
（`_read_pdf` / `_read_image`）。所有引用点同步：

| 位置 | 改动 |
| --- | --- |
| `tools.py` | schema 三合一；handlers / scoped_handlers 收敛；`sub_agent` 示例与 `execute` 文档 |
| `system_prompt.py` | 上下文保护规则那条、`allowed_tools` 示例 |
| `subagent.py` | 子智能体默认提示词第 3 条；**工具循环的返回值处理**（见 §2.10） |
| `teammate_manager.py` | 工具白名单 `wanted`；`_exec` 分派；**工具循环的返回值处理** |
| `context_compact.py` / `ws_bridge.py` / `agent_full_v2.py` | 注释与日志标签 |
| `docs/system-prompt.snapshot.md` | **重新生成**（见 §3.3 的代价说明） |

### 2.10 子智能体 / 队友：两条路径的同类缺陷

这两条循环**没有独立的发送边界**（不像主智能体有 `_model_messages`），所以
`run_read` 返回的 dict 会**就地**被 `str()` 成一段 Python repr 塞进 tool 消息 ——
图彻底丢失。改造后两条路径都补齐三件事：

1. 按**形状**识别返回值，图片部分进 `tool_image_values`，tool 消息只放
   `tool_image_text(...)`；
2. 在该批 tool 消息**全部之后**追加一条合成 user 消息（顺序硬约束同引擎）；
3. **就地展开**成 `image_url`（它们没有投影层），并摘掉 `_tool_images` marker
   —— 那个字段只用于主智能体回放辨认，漏进请求体是脏数据。

能力门控同样不能省：查 `llm_config.model_supports_image(<自己那个模型>)`，
text-only 模型下图片降级为占位文本，而不是让 provider 报 400。

***

## 三、落地

### 3.1 改动面

| 文件 | 类型 | 改动 |
| --- | --- | --- |
| `agents/doc_convert.py` | 修改 | 新增 `convert_office` / `empty_note` / `text_is_empty_note` / `humanize_anchors` / `tool_cache_dir`（+ `_cache_key` / `_prune_cache` / `_write_cache_gitignore`）；Office 抽取迁入；`convert_pdf` 增加"页图目录不可写 → 转纯文本"的降级 |
| `agents/tools.py` | 修改 | `run_read` 变成分派入口；`_read_image` / `_read_pdf` / `_read_office` / `_read_text`；三处 schema 合一；handlers 收敛 |
| `agents/attachments.py` | 修改 | `extract_text` 转发转换层；空占位与 Office 抽取改为再导出；`tool_image` 块扩成多图（`tool_image_items` 归一化 + `build_tool_images_result` + 分组预算）；`expand_content_for_model` / `text_view` 跟随 |
| `agents/refs.py` | 修改 | 注入文本去掉路由表；`.aigent` 进忽略清单 |
| `agents/subagent.py` / `agents/teammate_manager.py` | 修改 | 图片块的识别 / 聚合 / 就地展开（§2.10） |
| `agents/system_prompt.py` | 修改 | 工具说明与示例 |
| `agents/context_compact.py` / `agents/ws_bridge.py` / `agents/agent_full_v2.py` | 修改 | 注释、日志标签、多图 label |
| `tests/test_run_read_dispatch.py` | 新增 | 30 例：分派 / 页图缓存 / 降级 / 分组预算 / refs 路由 |
| `tests/test_doc_convert.py` | 修改 | +18 例：`convert_office` / `humanize_anchors` / `tool_cache_dir` |
| `tests/test_view_image.py` / `tests/test_sub_agent_tool_contract.py` / `tests/test_attachment_store.py` | 修改 | 跟随新入口与新标签（`office_text`） |
| `.env.example` / `docs/system-prompt.snapshot.md` / `docs/frontend/00-README.md` | 修改 | 新 knob 注释、快照重生成、文档清单 |

### 3.2 勿回退要点

1. **读文件只有一个入口**。新增格式时改 `run_read` 的分派，**不要**再挂第二个工具名
   ——"选错工具"是本次要消灭的那类失败。
2. `run_read` 可能返回**非字符串**（中性图片块）。引擎、子智能体、队友三处都必须
   按**形状**判定（`is_tool_image_result`），**不许** `str()` 掉。
3. 图片预算按**组**；文档读取**不切页**。
4. 模型可见文本里**不许出现 `<!--img:pN-->`**（存储锚点）。
5. 页图缓存落在工作空间内、key 含 mtime/size、`.aigent` 必须在
   `refs.DEFAULT_IGNORE_DIRS` 里、目录内 `.gitignore` 内容为 `*`。
6. **不可写 ≠ 报错**：`tool_cache_dir` 返回 `None` 只对应"路径不可解析"；
   目录不可写由 `convert_pdf` 降级为纯文本 + warning。**正文永远不该因为页图而丢**。
7. Office 的诚实声明（丢失内容 / 截断 / 工作表行数）**属于转换层**，两条通道共用。
8. 子智能体与队友的图片消息**必须追加在该批 tool 消息之后**，且就地展开并摘 marker。

### 3.3 代价：系统提示词快照变更

退役两个工具名必然改 L0 冻结段，`docs/system-prompt.snapshot.md` 已重新生成
（4250 → 4352 字符）。**跨会话前缀缓存会一次性失效**（下一次请求 miss，之后重新
预热）。这是一次性成本，与 doc 13 §2.4「刻意不动快照」的情形不同 —— 那里是
"为一个局部特性污染 L0 不值得"，这里是"工具名本来就写在 L0 里，不改进不去"。

### 3.4 可调参数

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `TOOL_DOC_CACHE_TTL_SECONDS` | 604800（7 天） | 页图缓存目录的存活秒数，超期在下一次渲染时清剪 |

复用（语义相同，未改键名）：`ATTACHMENT_DOC_MAX_PAGES` / `ATTACHMENT_DOC_MAX_IMAGES` /
`ATTACHMENT_DOC_PAGE_IMAGE_MIN_TEXT` / `ATTACHMENT_IMAGE_MAX_EDGE` /
`ATTACHMENT_TEXT_MAX_CHARS` / `VIEW_IMAGE_MAX_PER_TURN`（语义改为"组数"）。

### 3.5 验证

**离线单测（已跑）**：`.venv/bin/python -m unittest discover -s tests -v` → **600 例通过**
（较改造前 552 例新增 48 例）。新增用例覆盖：五路分派、真 PDF 的"密集文本页不渲染 /
图片页渲染 / 混合文档只渲该渲的页"、页图缓存路径与 key、`.gitignore`、超期清剪、
不可写降级（文本层必须完好）、转换层抛异常收束为字符串、整组丢弃文案、存量单数字段
归一化、refs 注入文本与忽略清单。

**离线链路（已跑，不连模型）**：走真实代码
`run_read → 中性块 → 合成 user 消息 → expand_content_for_model`，看**请求体**：

| 输入 | 工具结果 | 模型可见文本 | 展开后的请求体 |
| --- | --- | --- | --- |
| 2 页 PDF（第 1 页密集文本 / 第 2 页纯图片＝图表页） | 图片块 1 张，`page=2` | `PDF: spec.pdf，共 2 页。随附页图 1 张（第 2 页）…` + 第 1 页文本层 + `--- 第 2 页 ---[第 2 页为图像，随附]` | 1 个 `image_url`，**36 KB**（`data:image/jpeg;base64,…`，页图 26.5 KB） |
| 带柱状图的 xlsx（1 sheet / 4 行） | 纯文本 72 字符 | `--- 工作表: 销量（4 行）---` + 行列 + `（本格式仅提取文本与表格结构；图表、图片、版式未包含）` | 无图片 |
| png（900×700） | 图片块 1 张 | `已读取图片 fig.png（image/png，3.3KB），内容随附。` | 1 个 `image_url`，**5 KB** |

同时确认：落盘账本里**不含** base64；页图落在 `.aigent/pages/<16 位 hash>/doc.pages/p2.jpg`；
目录内 `.gitignore` 内容为 `*`；`.aigent` **未**出现在 `@` 候选列表里。

**端到端（待你在桌面端实测）**：把上面那三个文件放进工作空间，用 `@` 引用后提问
图表内容 / 具体数值，确认模型是**看图作答**而不是拿文本层硬猜。**本文没有记录
"模型真的读出了图表"这类断言** —— 那取决于 provider（doc 14 §3.4 同此结论）。
跑完请把实测结果补进本节。

> 上表也暴露了一个**已知缺口**：带柱状图的 xlsx 只回文本，正文里没有任何"这里有一张
> 月度销量柱状图"的线索（见 §4.5）。图表数值在单元格里，模型读得到数，**读不到图**。

***

## 四、取舍与备选

### 4.1 为什么合并而不是"保留三个名字、只补能力"

保留三个名字的代价不是多写几行 schema，而是**每次请求都要模型做一次分类决策**，
且这个决策错了要花一整轮才被发现。工具名的数量是模型出错概率的乘数 —— 能减就减。

### 4.2 为什么 Office 不做视觉渲染

整页视觉版式需要 LibreOffice（~800MB 外部二进制），已明确否决（doc 12 §四）。
替代路线是本项目一贯的那条：**渲染成图交给模型的视觉能力**，但对 Office 而言
渲染器本身就是那个重量级依赖，于是退到"文本 + 表格结构 + 诚实声明"。真要精确
读 Office 里的图表，可行路径是先转成 PDF 再读 —— 这是**模型/用户可以主动选择**的
动作，不由我们在读取时默默代劳。

### 4.3 为什么页图缓存不落 OS 临时目录

工具（`ToolRegistry`）只拿得到 `workdir` / `bash_cwd`，拿不到会话元数据目录，
所以"落在会话目录里跟着会话删除一起清"需要给所有构造点加参数。落在工作空间内的
代价是多了一个往用户目录写派生文件的例外（doc 13 的"零复制"卖点），用忽略清单 +
目录内 `.gitignore` + TTL 清剪把它收窄。**用户明确选择了这条路**（另一条是 OS 临时
目录：零 GC 代码但 Finder 里不可见、且清理由 OS 决定）。

### 4.4 为什么不做 PDF 页区间（`pages="1-5"`）

`convert_pdf` 没有区间参数，加它属于范围蔓延：要先扩转换层契约，再考虑与
`max_pages` 的语义叠加。当前用 `max_pages` 限页图数量、文本层全量，已经覆盖
"别把请求体撑爆"这个真实约束。真需要精读某几页时，模型可以用 bash 调 pymupdf
自己切片 —— 把这条写进文档而不是先做进工具。

### 4.5 已知缺口

- **Office 的图表数值**：`xlsx` 只能给单元格文本，图表系列引用的数据虽然在表里，
  但"这里有一张什么图"这个信息拿不到（需要读 chart part，未做）。
- **docx/pptx 内嵌图片**：可以把 image part 抽出来走同一条 tool_image 通道，
  未做（当前只在正文里丢失，模型看不到）。
- **PDF 页图的选择性重渲**：同一份 PDF 每次读取都按 key 复用缓存，但源文件一改
  （哪怕只动一个字节）就整份重渲。对超大 PDF 的频繁读取会有浪费。
