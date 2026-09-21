# 13 · 引用文件与文件夹（@-mention）

> 状态：**已实施**（2026-09-21）。本文是该功能的设计与落地依据。
> 相关文档：[12-附件与文件输入](12-附件与文件输入.md)（**另一条通道**，务必对照阅读）、
> [03-前后端通信协议](03-前后端通信协议.md) §2.9、[11-工作空间管理](11-工作空间管理.md)。

***

## 一、概念

### 1.1 它解决什么问题

输入框里打 `@`，弹出当前工作空间的文件/目录列表，选中后变成一个**胶囊**内嵌在正文里；
发送时只把**路径**交给模型，内容由模型自己按需读取。

与「添加文件或图片」（附件）的差别是根本性的，不是入口不同：

| | 附件（doc 12） | 引用（本文） |
| --- | --- | --- |
| 存储 | `copy2` 副本到 `.attachments/` | **零复制、零存储** |
| 位置 | 工作空间**之外**（`~/.aigent/...`） | 工作空间**之内**（沙箱根之下） |
| 模型可读性 | 副本被 `safe_path` 拒绝 → 只能预注入全文 | `run_read` 直接可读 → **只给路径** |
| 注入内容 | 全文（30000 / 60000 字封顶） | 只有路径清单 |
| 内容时效 | 快照（原件改动不影响） | 现读（永远最新），但**引用会失效** |
| token 成本 | 全文 | 几条路径 |

一句话：附件是**把文件搬进上下文**，引用是**给上下文指一条路**。

### 1.2 与附件的边界（硬约定）

- **两条通道的协议字段、块类型、后端模块、前端组件、CSS 前缀、文档章节全部独立。**
  `refs` 不是 `attachments` 的变体，任何把引用塞进 `attachments` 字段的做法都会踩到
  doc 12 记录过的坑（后端按 `att_id` 找不到草稿 → 静默丢失）。
- 唯一复用的只有**表现层约定**：点对点应答信封、chip 视觉口径、`hasSendableContent` 判据。

### 1.3 三条入口

| 入口 | 行为 |
| --- | --- |
| 输入框手打 `@` | 触发候选面板（主路径） |
| `+` 菜单 →「引用文件或文件夹」 | **只往输入框插入一个 `@`**，之后与手打完全同一条路径。**绝不调 `pickFiles`**（那会走附件复制链路，把"引用"变成"上传"） |
| （将来）`/` 命令、`#` 会话 | 复用同一套 token 机制 |

***

## 二、设计

### 2.1 数据流

```
手打 @（或 + 菜单插入 @）
  → Suggestion 插件激活 → 前端调 refs_list（**一次拉全量扁平列表**）
  → 后端 BFS 遍历工作空间（忽略清单 + 上限 3000）→ 回 refs 信封
  → 前端缓存候选；按键过滤**全在本地做**（零延迟）
  → 回车/点击选中 → 插入 atom 胶囊节点（attrs: path/rel/isDir/label）
  → 发送：serializeDoc → text(含 @相对路径 token) + refs[]
  → 后端 normalize_refs 越界校验 → 挂中性引用块 {"type":"ref","ref":{...}}
  → 落 jsonl（账本形态，**只有路径**）
  → 每次请求由 Agent._model_messages() 现算成一个说明文本块发给模型（**不回写历史**）
  → 回放：_history_to_ui 用 harvest_refs 还原 → 气泡里渲染只读 chip
```

### 2.2 后端：`agents/refs.py`（叶子模块）

只依赖标准库 + `logger`；**不 import `attachments` / `agent_full_v2` / `session_manage`**。

| 函数 | 职责 |
| --- | --- |
| `resolve_within(workdir, raw)` | 路径规范化 + 越界拒绝。**语义镜像 `tools.safe_path`**（`resolve()` 后 `is_relative_to`），但吞掉一切异常返回 `None` —— 一条越界引用不该打死整轮发送 |
| `list_workspace(workdir, *, limit, ignore)` | 扁平列目录，返回 `{workdir, items, truncated, total_seen, skipped}` |
| `normalize_refs(workdir, raw_refs)` | 前端线索 → **以磁盘为准**的记录（`name` / `is_dir` 重新 stat，前端伪造不了），按 path 去重 |
| `attach_ref_blocks(content, refs)` | 挂中性引用块。**无 refs 时返回传入的同一个对象** |
| `expand_ref_blocks_for_model(message)` | 发送边界：所有引用块合并成**一个**说明文本块。**无引用块返回同一对象**；绝不抛异常 |
| `harvest_refs(content)` | 回放取回 `{path,name,is_dir}`（**不做 stat**，保持回放廉价） |
| `ref_title_hint(raw_refs)` | 纯引用消息的标题兜底（`[引用] a.ts`） |

**遍历算法：BFS 而非 DFS。** 平铺列表 + 条数上限的组合下，DFS 会让第一个庞大子目录吃满
名额、同级目录直接看不见；BFS 保证同一深度先铺满，截断时砍掉的是最深条目。

其它遍历约定：

- **忽略清单**（按目录名整棵剪枝）：`.git` `node_modules` `.venv` `venv` `__pycache__`
  `dist` `build` `out` `target` `.next` `.nuxt` `.idea` `.vscode` `.pytest_cache`
  `.mypy_cache` `.ruff_cache` `.cache` `coverage` `.tox` `.eggs`；文件级忽略 `.DS_Store`。
  可用 `REF_LIST_IGNORE`（逗号分隔）**追加**，不能移除默认项。
- 每个目录内部先目录后文件，各自按 `name.casefold()` 升序 → 结果确定、可比对。
- **符号链接一律跳过**：防环；且链出工作空间的 symlink 交给模型去读必然被拒绝，
  不该由我们主动推荐一次注定失败的尝试。
- 权限错误 / 并发删除：逐目录吞 `OSError` 并计入 `skipped`，不中断。
- 上限 `REF_LIST_MAX_ENTRIES`（默认 3000），超出置 `truncated=True`。
- **绝不抛异常**：本函数在 `asyncio.to_thread` 里跑，异常会让前端一直转圈。

### 2.3 协议：`refs_list` → `refs`

```jsonc
// 前端 → 后端
{ "kind": "refs_list", "payload": { "project_id": "ws3k9f2m1xq0", "session_id": "Kx7mQ2vT8p" } }
```

```jsonc
// 后端 → 前端（成功）
{ "kind": "refs", "payload": {
    "project_id": "ws3k9f2m1xq0",
    "workdir": "/Users/pei/Projects/demo",
    "items": [ { "path": ".../src", "name": "src", "type": "dir" },
               { "path": ".../src/a.ts", "name": "a.ts", "type": "file" } ],
    "truncated": false, "total_seen": 412, "skipped": 0, "disabled": false } }
```

```jsonc
// default 空间 / 目录不可用：**正常态，不是 error**
{ "kind": "refs", "payload": { "project_id": "default", "workdir": "", "items": [],
    "truncated": false, "total_seen": 0, "skipped": 0, "disabled": true,
    "reason": "默认工作空间是临时草稿目录，没有可引用的项目文件；请先切换到自定义工作空间" } }
```

要点：

- **`disabled` 而不是 `error`**：这是用户输入 `@` 时的**常规状态**，弹 toast 是打扰。
  前端把它渲染成面板里的一行原因（**不静默消失** —— 静默会让用户以为按键失灵）。
- **default 空间在遍历之前就返回**：scratch 是草稿区，扫它既没意义也白费时间。
- 有 `session_id` 时按会话 `work_root` 快照解析沙箱根，**与 `run_read` 同根**。
  否则会出现"列表里选得到、模型却读不到"的脱节，而"可读"正是引用成立的前提。
- `items` 用 `type: "dir"|"file"`（**列表语义**），消息里的引用记录用 `is_dir`（**引用语义**）。
  形状不同是刻意的；`dir`（所在目录）**不发**，前端由 `path` 去掉 `name` 推导。
- **`refs` 是点对点应答信封，不进 `isKnownAgentEvent` 白名单**（与 `attachments_staged` 同类）。

### 2.4 账本块与发送边界展开

账本块（落 jsonl，**只有路径与元数据**）：

```jsonc
{ "type": "ref", "ref": { "path": ".../a.ts", "name": "a.ts", "is_dir": false, "project_id": "ws3k9f2m1xq0" } }
```

发送边界展开成**一个**文本块：

```
[用户引用了以下工作空间路径（仅路径与类型，文件内容不在上下文中）]
- /Users/pei/Projects/demo/src/utils/a.ts（文件）
- /Users/pei/Projects/demo/src/components（目录）
- /Users/pei/Projects/demo/old/x.ts（文件，当前不存在，可能已被删除或移动）

需要内容时请用 run_read 按上述绝对路径读取 —— 它按类型自动分派（文本、图片、PDF、
Word/Excel/PPT 都是同一个工具，PDF 会同时给出文本层与页图）；需要查看目录内容时用
run_glob 或 bash（工作目录即工作空间根）。
不要在未读取的情况下猜测、复述或杜撰这些文件的内容。
```

> 这段措辞在 2026-09-21 简化过：原先写着"PDF 用 `run_read_pdf`、**图片用
> `view_image`**"——一张**格式 → 工具**的映射表。三个读工具合并成 `run_read`
> 之后（[15-统一文件读取](15-统一文件读取（run_read 按类型分派）.md)），路由由工具
> 内部按魔数/扩展名完成，注入块里不该再留这张表 —— 它正是"模型选错工具"的温床。
> 引用通道本身仍然**只给路径**（零复制）：图片与页图都是模型按需用工具去取的，
> 不是在引用展开时预内联进请求体的。

**提示词约束放在这条注入块里，不放系统提示词。** 理由：① 只有带引用的消息需要它；
② 系统提示词有"逐字节相同 → 跨会话共享前缀缓存"的硬约束，为一个局部特性污染所有
会话的 L0 冻结段不划算。**因此 `docs/system-prompt.snapshot.md` 本次无需更新**——
不是漏改，是设计如此。

### 2.5 零行为变化（硬保证）

`attach_ref_blocks(content, [])` 与 `expand_ref_blocks_for_model(message)` 在"无引用"
时**返回传入的同一个对象**（不是等值副本）。无附件的会话请求体因此与改造前逐字节一致
—— 这条由结构保证，而不是靠"新增代码恰好没副作用"。
`tests/test_agent_model_messages.py` 里有 `assertIs` 身份断言钉住它：只写等值断言的话，
将来有人把它改成"总是返回新 dict"照样能过，而零复制的保证就没了。

### 2.6 与其它机制的接缝

| 接缝 | 处理 |
| --- | --- |
| `chat.payload.text` | **恒为字符串**。胶囊序列化成 `@相对路径` token 留在 text 里（只给模型定位看）；权威信息在 `refs[]`。**不要从 text 反解路径** |
| `_model_messages` | 引擎层**唯一**新增的一步纯变换（追加在附件展开之后） |
| `context_compact.content_to_str` | 必须加 `ref` 分支（`[引用: 名字]`）。否则未知块走 `str(block)`，路径清单与 JSON 键名会被拼进 L4 摘要与 token 估算 |
| L1 裁剪 | **不保护 ref 块**（与图片不同）：引用只丢一条路径清单，prose 里的 `@相对路径` token 与 workdir 仍在，模型可自行 `run_read` 补回；加进 keep 集反而让上下文膨胀 |
| 标题生成 | `text` 优先，其次 `[附件] 文件名`，最后 `[引用] 文件名` |
| 图片引用（2026-09-21） | 引用通道**不预内联**图片：路径照给，模型按需调 `view_image` 去取（通道与协议完全独立，见 `14-工具读图（view_image）.md`） |

### 2.7 前端结构

| 文件 | 职责 |
| --- | --- |
| `components/Chat/editor/refExtension.ts` | `Mention.extend(...)`：补 `path/rel/isDir` 属性 + React NodeView + `renderText` 序列化 + Suggestion 配置 |
| `components/Chat/editor/serializeDoc.ts` | `serializeEditor()` / `collectRefs()` —— 序列化**唯一出处**，纯函数（只有类型导入） |
| `components/Chat/RefPicker.tsx` | 候选面板（向上弹出、封顶 280px、键盘/鼠标交互、空态/加载态/disabled 原因/截断提示） |
| `components/Chat/RefCapsule.tsx` | 胶囊 NodeView（图标 ⇄ 悬浮变删除、tooltip 绝对路径） |
| `components/Chat/RefBar.tsx` | 消息气泡里的**只读**引用 chip |
| `lib/refFilter.ts` | 本地过滤打分 + `toCandidates` + `capsuleLabel`（纯函数） |
| `hooks/useWorkspaceRefs.ts` | 一次拉全量 + 按 (空间, 会话) 缓存 |

**Tiptap 接入的关键取舍**

- 直接用 `@tiptap/extension-mention`，不手搓 `Node.create`：它已经提供原子 inline 节点
  （`inline / atom / selectable:false`）与 `renderText`，且支持多触发字符 —— 将来挂
  `/` 命令、`#` 会话引用时加一个 suggestion 条目即可。**节点名保持 `mention`**（不重命名）。
- **不用 `starter-kit`**：聊天输入框不需要标题/列表/加粗。
- **不引 tippy / floating-ui**：面板是"相对输入框向上弹出"（`position:absolute; bottom:100%`），
  不是锚光标。CSS 就够，少两个依赖。
- `allowedPrefixes: null`：默认只允许"行首或空格后"触发，中文用户写「看看@文件」不会触发。
- **`onKeyDown` 首行放行输入法**（`isComposing || keyCode === 229`）：拼音候选框里的
  ↑↓/Enter 若被当成列表导航/选中，中文输入直接不可用。

**面板条目必须"每次渲染现算"**：候选列表是异步到位的，若在 `@` 触发时快照一份 items
存进状态，列表到货后面板不会刷新（用户只能再敲一个字才看见结果）。所以
`RefPanelState` 只存 `{query, command}`，条目由 `filterRefs(refs.items, query)` 在
渲染时现算。这是实测中真实抓到的一个 bug。

**按键顺序（容易踩）**：`editorProps.handleKeyDown` 比插件的 `handleKeyDown` **先**执行
（prosemirror-view 的 `someProp` 先查直接 props 再查插件）。所以面板开着且有条目时，
输入框的处理器必须 `return false` 把 Enter 让给 Suggestion 去"选中"，
否则会出现"面板开着却把消息发出去了"。

**编辑器内容不受控**：父组件只从 `onUpdate` 收 `{text, refs}` 快照用于发送判据，
**绝不把父 state 回灌进编辑器**（`setContent` 会冲掉光标、打断拼音输入、清空撤销栈）。
清空走 `clearSignal` 递增 → `editor.commands.clearContent(true)`。

### 2.8 改造后重验的输入区约束

textarea → contenteditable 之后，原有约束全部归零，逐条重做并实测：

| # | 原约束 | 现做法 |
| --- | --- | --- |
| 1 | `autoGrow` 上限 160px | 删 JS，改 CSS `.composer-editor{min-height;max-height:160px;overflow-y:auto}` |
| 2 | Enter + `isComposing` 守卫 | `editorProps.handleKeyDown`，条件加倍：`!e.shiftKey && !e.nativeEvent.isComposing && !editor.view.composing` |
| 3 | 粘贴只在真取到文件时 `preventDefault` | `editorProps.handlePaste`：无 files → `return false` 放行文本 |
| 4 | 拖拽三硬约束（dragDepth / preventDefault / 遮罩 `pointer-events:none`） | **保持不动**，事件仍绑在 `.composer` 包裹层；编辑器只补 `handleDrop` **拦住 ProseMirror 把文件名当文字插入**，登记仍由 `.composer` 的 onDrop 统一负责（两处都登记会重复提交） |
| 5 | 五处发送守卫 | 统一到唯一判据 `hasSendableContent(text, attachments, refs)` |

**五处发送守卫**：`InputBox` 按钮 disabled、`InputBox` Enter 守卫、`ChatPanel.doSend`、
`store.send` 内部守卫、**`main/index.ts` 的 `agent:send`**。
最后一道跨进程无法 import，只能镜像同一条件：

```ts
if (!isTrustedSender(e) || (!payload?.text && !payload?.attachments?.length && !payload?.refs?.length)) return
```

漏掉 `refs` 就等于把"只 @ 了一个文件就发送"的消息**静默丢掉** —— 正是 doc 12 §3.2
第 1 条踩过的同一个坑。

***

## 三、落地

### 3.1 改动面

**后端**（新增 1 个模块 + 2 处引擎层小改）：

| 文件 | 类型 | 改动 |
| --- | --- | --- |
| `agents/refs.py` | 新增 | 见 §2.2 |
| `agents/ws_bridge.py` | 修改 | 导入 `refs`；新增 `refs_list` 分支（`asyncio.to_thread`）；`chat` 收 `refs` → `normalize_refs` → `attach_ref_blocks`；标题兜底链加 `ref_title_hint`；`_history_to_ui` 加 `harvest_refs` |
| `agents/agent_full_v2.py` | 修改 | `_model_messages` 返回前追加 `expand_ref_blocks_for_model`（**已授权范围**） |
| `agents/context_compact.py` | 修改 | `content_to_str` 加 `ref` 分支 + `_ref_label`（**已授权范围**） |
| `agents/attachments.py` / `paths.py` / `system_prompt.py` | **未改** | 引用与附件解耦；`@` 范围就是既有 `workdir`；约束写在注入块 |
| `tests/test_refs.py` | 新增 | 46 例单元测试 |
| `tests/test_ref_protocol.py` | 新增 | 20 例协议面测试 |

**前端**（新增 7 个文件 + 修改 12 个）：

新增 `editor/refExtension.ts`、`editor/serializeDoc.ts`、`RefPicker.tsx`、`RefCapsule.tsx`、
`RefBar.tsx`、`lib/refFilter.ts`、`hooks/useWorkspaceRefs.ts`；
修改 `InputBox.tsx`（textarea → EditorContent，最大的一处）、`ChatPanel.tsx`、
`MessageItem.tsx`、`PlusMenu.tsx`、`agentStore.ts`、`agentProtocol.ts`、`preload/index.ts`
+ `index.d.ts`、`main/index.ts`、`lib/browserAgent.ts`、`styles/chat.css`、`package.json`
（Tiptap 依赖）、`tsconfig.web.json` + `electron.vite.config.ts`（加 `@lib` 别名）。

### 3.2 勿回退要点

1. `chat.payload.text` 恒为字符串；引用只走 `refs` 兄弟字段。
2. 账本块只有路径；展开只在发送边界现算，**不回写 history**。
3. 无引用时 `attach_ref_blocks` / `expand_ref_blocks_for_model` **返回同一对象**（`assertIs` 钉住）。
4. `refs` 信封不进 `isKnownAgentEvent` 白名单。
5. default 空间回 `disabled` 而非 `error`；前端**显示原因而不是静默消失**。
6. `@` 的沙箱根必须与会话 `work_root` 快照一致（与 `run_read` 同根）。
7. `+` 菜单的 `refPath` **只插入 `@`**，绝不调 `pickFiles`。
8. 五处发送守卫共用 `hasSendableContent`；主进程那道必须同时看 `refs`。
9. 编辑器内容不受控；清空走 `clearSignal`，不用 `setContent`。
10. 面板条目每次渲染现算，不存快照。
11. 输入区的拖拽三硬约束、粘贴"只在真取到文件时才拦"保持原样。
12. 跳过符号链接；越界路径一律丢弃且不阻断发送。

### 3.3 可调参数

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `REF_LIST_MAX_ENTRIES` | 3000 | 一次列目录返回的条目上限 |
| `REF_LIST_IGNORE` | 空 | 追加的忽略目录名（逗号分隔） |

（与附件一致：读在**调用点**而非导入点，见 `README`/`config.json` 的说明。）

### 3.4 验证（2026-09-21 实测）

**后端**：`unittest` 全量 **512 例通过**（新增 66 例：`test_refs.py` 46 + `test_ref_protocol.py` 20）。

**前端**：`npm run typecheck` / `npm run build` 全绿。

**交互实测**（headless Chromium + 裸 CDP 驱动真实构建产物；桩掉 `window.agent`，
用 43 条候选与 1200×900 视口）：

| 项 | 实测数字 | 结论 |
| --- | --- | --- |
| 面板定位 | `panel.bottom = 738`，`composer.top = 745`；`position:absolute`，`z-index:30`；左边界 275 vs composer 274 | 在输入框正上方、不被裁剪 |
| 列表高度 | `clientHeight = 280`（封顶），`scrollHeight = 1427` | 高度封顶、内部可滚 |
| 滚轮隔离 | 面板内滚轮 → `listScrollTop = 300`，`document.scrollingElement.scrollTop = 0` | 滚列表不带动外层（`overscroll-behavior: contain`） |
| 默认选中 | `firstItemActive = true` | 打开即选中第一项 |
| 本地过滤 | 输入 `READ` → 命中 1 项 `README.md` 且为高亮项 | 前缀优先、大小写不敏感 |
| 方向键 clamp | 1 项时连按 ↓ 仍停在 `README.md` | 不环绕 |
| Enter 优先级 | 面板开 → 插胶囊 1 个、`send` 调用数 **0**；面板关 → `send` 调用数 **1** | 面板开着不会误发 |
| 胶囊内容 | 文本 `README.md`，`title = /Users/pei/proj/README.md` | 名称带后缀 + tooltip 绝对路径 |
| 悬浮换图标 | 默认图标 `flex → none`，删除图标 `none → flex`；`elementFromPoint` 命中 `.ref-icon-remove` 内部 | 真命中，非"看起来变了" |
| caret 跟随 | 插胶囊后继续打字 → 胶囊之后文本为 `" kan"`；发送文本 `"@README.md kan"` | 光标在胶囊之后 |
| Esc | 面板关闭且保留 `@query` 文本；再 Enter 正常发送 | 取消选择不等于清空输入 |
| 粘贴纯文本 | 文本进编辑器，附件登记调用 **0** 次 | 没吃掉纯文本粘贴 |
| 粘贴带文件 | 附件登记 1 次（`['/tmp/fake-clip.png']`） | 走附件登记链路 |
| 拖放 | `.composer` 收到 drop（1 file）且登记 1 次；编辑器内**未插入文件名** | `handleDrop` 只拦不登记，无重复 |
| `+` 菜单接线 | `pickFiles` 调用 **0** 次；输入框出现 `@` 且面板打开（43 项） | 只插 `@`，同一套选择器 |
| 控制台 | 无 CSP violation、无报错 | — |

> 提示：`handlePaste` / `onDrop` 都是 `async`，登记发生在微任务之后 —— 用合成事件测它们时
> **派发后必须再等一拍**才能读结果，同步读会永远读到空（本次实测踩过一次）。

***

## 四、取舍与备选

### 4.1 为什么不做"复制一份"（即：为什么不复用附件链路）

复制能让引用永久有效，但也就失去了引用的全部价值：内容永远最新、不占磁盘、token 成本低。
"引用会失效"是真代价，我们用注入块里的"当前不存在，可能已被删除或移动"如实标注来兜，
而不是靠复制掩盖它。

### 4.2 为什么"一次拉全量 + 前端本地过滤"

需求要的是"结果跟随输入实时变化"。每次按键回后端查会带来往返延迟与请求乱序（还得处理
竞态）。代价是**被上限截断的那部分搜不到** —— 用列表底部的"已截断/还有 N 项"提示来兜住
这个认知缺口。若将来工作空间大到 3000 条不够，再考虑"本地优先 + 后端兜底"的混合方案。

### 4.3 为什么不引虚拟滚动

后端上限 3000，但全量 DOM 渲染会明显卡顿。这里只画前 **200** 条（`REF_RENDER_LIMIT`）
并提示"继续输入以缩小范围" —— 比引一个虚拟滚动库简单得多，也够用（用户真要找某个文件
时本来就会打字）。

### 4.4 面板高度用 `max-height` 而非固定 `height`

需求写的是"高度固定、可有滚动条"。用 `max-height: 280px` 达到同样效果，同时避免
条目少时出现一大片空白。若确实要严格固定高度，改这一行 CSS 即可。

### 4.5 不做拼音匹配

v1 明确不做（成本与收益不成比例）。**这是有意的，不是 bug。**

### 4.6 引用失效的可选增强

回放时**不做 stat**（保持回放廉价，`_history_to_ui` 也拿不到 workdir）。若将来要在
气泡里显示"文件已缺失"标记，可给 `_history_to_ui` 加可选 `workdir` 参数顺带 stat，
与附件的 `missing` 口径对齐。
