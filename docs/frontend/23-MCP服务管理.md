# 23 - MCP 服务管理

> 在设置弹窗里新增独立的「MCP」菜单：**市场浏览与一键安装**、**本地手动添加**、**引用本机已启动的 MCP**、以及启停 / 测试连接 / 删除 / 查看工具清单。
> 核心结论：**后端早就是一个真·MCP 客户端**（`agents/mcp_manager.py`：stdio / sse / streamable-http 三传输、`mcp__{server}__{tool}` 注入、按 mtime 热重载、`destructiveHint` 审批门控）—— 本期交付的是**管理面**，而不是 MCP 本身。改动因此集中在「配置的读写」「市场翻译」与「界面」三层，引擎层只加了 5 行可观测性。
>
> **2026-10-07 增补（§八）**：安装不再只有"写配置、交给 npx 拉包"一条路。新增**本地包安装** —— 把 npm 包装进 `~/.aigent/mcp/pkgs/<包@版本>/`，钉死版本、记录哈希、可复核可卸载，条目的 `command` 指向包内 bin 的绝对路径。市场安装确认页默认走这条（可切回 npx）。引擎层仍**零改动**：`mcp_installer.py` 是全新模块，`mcp_manager.py` 一行未动。

***

## 一、概念

### 1. 为什么市场用官方 registry，而不是自建

MCP 生态的「市场」不需要自己造。实测（2026-09-30）可用的数据源：

| 数据源 | 接口形态 | 是否选用 | 关键短板 |
| --- | --- | --- | --- |
| **官方 MCP Registry**<br>`registry.modelcontextprotocol.io` | 免鉴权 REST `GET /v0.1/servers` | ✅ **本期唯一数据源** | 只有元数据、不做审计；海外访问慢（实测 0.9s ~ 17s） |
| Smithery | CLI + API，7k+ 条目 | ❌ | 需 API key/OAuth，偏海外，含托管依赖 |
| Glama / mcp.so / PulseMCP | 基本是 HTML 目录 | ❌ | 无稳定公开 API，抓取脆弱 |
| 自建清单 | 自己发 JSON | ❌（列为后续增量） | 要自己维护与审核 |

选它的决定性理由：**列表接口直接返回完整 `server.json`**（含 `packages[]` / `remotes[]`），这两个字段几乎能一对一翻译成我们既有的 `mcpServers` 配置格式 —— 不需要为「安装」写一套新的包管理逻辑。

⚠️ **两个必须在 UI 上讲清楚的前提**：

1. registry 只做**命名空间所有权校验**（DNS TXT / GitHub 组织），**没有代码审计、没有漏洞扫描**。被列出来 ≠ 安全。安装确认弹窗不是可选装饰，是**唯一的安全闸门**。
2. registry 至今是 **preview**。实测同一份响应里**同时存在两版 schema**（`2025-09-29` 与 `2025-12-11`）—— schema 漂移是既成事实，不是风险假设。所以翻译层一律「尽力而为 + 失败降级成『请手动填写』」，并且**不把 market 数据落盘缓存**（避免把坏结构固化到磁盘上）。

### 2. 「安装」的边界：默认不下载，可选下载到本地

**2026-10-07 改版**。原结论是「安装只写配置、不做包管理」：`packages[]` 里的 npm / PyPI 包交给 `npx` / `uvx` 在首次连接时自己拉，我们只负责写出那条 `{command, args}`。

这个边界现在被**部分放开**，因为它的代价在真实使用中显形了：

| 走 npx 拉取 | 代价 |
| --- | --- |
| 包落在哪 | `~/.npm/_npx/<hash>/` —— **不在 `~/.aigent` 收口体系内**，应用既看不到也管不着 |
| 版本 | `npx pkg@1.2.3` 看着是钉死的，但 spec 里没有版本时（registry 常见）每次连接都会重新解析，**上游一发版行为就无声漂移** |
| 「装了什么」 | 没有这个概念。UI 上只有「配了一条命令」 |
| 卸载 | 做不到 |

所以现在**两条路并存**，在安装确认页二选一（默认「下载到本地」，见 §8）：

- **下载到本地（新，默认）**：真的把包装进 `~/.aigent/mcp/pkgs/<包@版本>/`，钉死精确版本 + 记录完整性哈希，`command` 指向包内 bin 的**绝对路径**。
- **由 npx 在连接时拉取（原行为）**：只写 `{command:"npx", args:["-y","pkg@ver",...]}`，不占磁盘。

仍然**没有**做的：包的升级 / 回滚通道、依赖去重（一个包一份 `node_modules`，同包同版本不同条目会各存一份）、PyPI 侧（要落 `uv tool install` / `pip --target`，与「统一 uv、禁止擅自装包」的口径冲突，需单独定规矩）。

> 关键约束：**下载与写配置是两步、两个命令**。`mcp_pkg_install` 只下载 + 校验，写条目仍走既有的 `mcp_server_upsert` —— 这样「下载失败」绝不会留下一条指向不存在文件的死条目。

### 3. 为什么元数据走旁路文件，不内嵌进条目

市场安装需要额外记录「从哪装的」（`market_id` / `publisher` / `installed_at`）。这三个字段**不放** `mcpServers` 条目里，而是单独存 `~/.aigent/mcp/mcp_sources.json`。

三条理由，第二条是硬的：

1. **不污染标准格式** —— `mcp_servers.json` 保持可直接拷给 Claude Desktop / Cursor 用的 `mcpServers` 格式。
2. **避免虚假重连** —— `mcp_manager.maybe_reload()` 用 `new[name] != old.get(name)` 判定「配置变了要重连」（`mcp_manager.py:506,511`）。元数据若内嵌，改一个 `installed_at` 就会**触发整条断连重连**。
3. `load_config()` 虽然会忽略未知键，但这是隐式契约 —— 依赖「它会忽略」不如结构上分离干净。

### 4. 为什么 UI 数据源是**原始文件**，而不是 `load_config()`

这是最容易踩的一条。

`mcp_manager.load_config()` 会把 `enable: 0` 的条目**过滤掉**（不连接、不可枚举、不进 `catalog()`）。如果设置页拿它当列表数据源，**被禁用的条目就永远看不见了 —— 也就无法被重新启用**。

所以两个数据源分工明确：

| 用途 | 数据源 | 代码 |
| --- | --- | --- |
| **UI 列表** | 原始 `mcpServers` 映射（**含 `enable: 0`**）+ 旁路元数据 | `mcp_store.McpStore.list_servers()` |
| **运行时连接** | `load_config()` 过滤后的视图 | `mcp_manager`（**本期零改动**） |

UI 侧再把两者**叠加**：原始条目提供「有什么」，`mcp_manager` 提供「连没连上 / 有哪些工具」。

***

## 二、设计

### 1. 数据布局（3 个文件 + 1 个目录）

| 文件 / 目录 | 常量 | 作用 |
| --- | --- | --- |
| `~/.aigent/mcp/mcp_servers.json` | `paths.MCP_CONFIG` | 标准 `mcpServers` 格式，**运行时唯一数据源** |
| `~/.aigent/mcp/mcp_sources.json` | `paths.MCP_SOURCES` | 旁路元数据 `{name: {source, market_id, market_name, publisher, installed_at, pkg?}}` |
| `~/.aigent/mcp/pkgs/<包@版本>/` | `paths.MCP_PKGS_DIR` | **本地已下载的包**（2026-10-07 新增）。内含 npm 写的 `package.json` / `package-lock.json` / `node_modules`，以及我们写的 `aigent-meta.json` 与安装中哨兵 `.install-incomplete` |
| `~/.aigent/mcp/market_cache.json` | — | **本期不做**（见 §1.1：preview 期不落盘缓存市场结构） |

`pkg` 元数据只记在 `mcp_sources.json` 里（`{slug, name, version, command, integrity, registry}`），**不进条目** —— 理由与 §1.3 完全相同：内嵌会让改 `installed_at` 触发整条断连重连，而且会破坏「`mcp_servers.json` 可直接拷给别的客户端」这条承诺。

> ⚠️ MCP 配置**不在** `~/.aigent/config/` 的收口体系内（`config.py:52-58` 的 5 文件清单不含它）→ **不要**塞进 `config_path()`。它和 `~/.aigent/sandbox/` 一样，是顶层兄弟目录。`pkgs/` 同理，只在 `~/.aigent/mcp/` 之下。

**文件权限 0600**：条目 `env` / `headers` 可能含明文 API key（详见 §2.5）。

### 2. 界面结构（4 个子视图）

设置弹窗左侧菜单在「沙盒」之后插入「MCP」（`SettingsModal.tsx` 的 `NAV`）：

```
通用 / 模型 / 权限 / 沙盒 / MCP / 归档 / 关于
```

面板内部**用视图切换而不是嵌套模态** —— 设置本身已经是模态，再叠一层遮罩会引出 z-index 与点击穿透问题，而这里没有必须并存的编辑场景：

| 视图 | 组件 | 职责 |
| --- | --- | --- |
| `list` | `McpSettings` 内联 | 条目列表：增 / 改 / 删 / 启停 / 试连 / 看工具；**下半部分是「本地包」区块**（见 §8） |
| `form` | `McpServerForm` | 新增或编辑单条（传输类型三分支表单 + 测试连接） |
| `market` | `McpMarketView` | 搜索官方 registry + 分页（「加载更多」追加） |
| `install` | `McpInstallView` | **安装确认** —— 全流程唯一的安全闸门；含「安装方式」二选一 |

列表页布局（`list`）：

```
┌ MCP 服务 ──────────────────────────────────────────────┐
│ 已安装 5 个 · 已启用 4 个 · 已连接 1 个 · 本地包 2 个    │
│                     [ 从市场安装 ]  [ + 手动添加 ]       │
│ ⚠ MCP 服务器是可执行的第三方代码 —— stdio 类型会直接在本 │
│   机启动该命令。请只添加你信任的来源。                   │
├────────────────────────────────────────────────────────┤
│ ● local-echo   stdio 本地  已连接   3 个工具 测试 编辑 删除 [开] │
│ ○ zotero-mcp   http  本地  已禁用    —       测试 编辑 删除 [关] │
│ ⚠ http-echo    http  本地  连接失败  —       测试 编辑 删除 [开] │
├ 本地包（2）────────────────── ~/.aigent/mcp/pkgs ──────┤
│ ● server-filesystem 1.2.3  8.4 MB 已就绪 1 个条目在用  校验 删除 │
│ ● mcp-server-time   0.6.2  1.1 MB 已就绪 无条目引用     校验 删除 │
└────────────────────────────────────────────────────────┘
```

行内操作（测试 / 编辑 / 删除 / 开关）与主信息**同一行**、开关在最右 —— 早期版本把操作单独放第二行，5 条就把面板撑满；改内联后行高从 ~70px 降到 **38px**（实测）。

**「条目」与「本地包」是两种独立资源**（各占一个列表）：条目是配置，本地包是磁盘上的下载产物。删条目**不动**包（它可能还被别的条目用），删包也**不动**条目（只在行内提示哪些条目因此失效）。耦合起来就会出现「改个名字顺手把包删了」这种事故。

### 3. 条目状态五态

后端算好下发（`_mcp_config_payload_sync`），**前端零硬编码推导**：

| 状态 | 含义 | 呈现 |
| --- | --- | --- |
| `disabled` | `enable: 0`，不参与连接 | 灰点 / 「已禁用」 |
| `connected` | 至少一个 MCPManager 已连上 | 绿点 / 「已连接」+ 工具数 |
| `error` | 有确切的失败原因（`last_error`） | 红点 / 「连接失败」+ 内联红字 |
| `idle` | 无任何可观测的 MCPManager，还没机会连 | 灰点 / 「待会话启动后连接」 |
| `disconnected` | 有 manager、没连上、也没留下错因 | 灰点 / 「未连接」 |

**`idle` 不是「连不上」**：`MCPManager` 是 per-Agent 实例（`agent_full_v2.py:423`），页面可能在任何一个 Agent 构造之前就被打开。把这种情况渲染成「未连接」是谎报故障。

状态汇总的坑（实测修正）：`_mcp_managers()` **必须把全局 `agent` 也算进去**。它在 `Agent.__init__` 里就 `connect_all()` 了，而它**不在** `registry.all_runtimes()` 里 —— 只看 runtime 会把「用户刚启用一条、还没开会话」显示成**未连接**，而真实连接其实已经建立。多实例之间按 `id()` 去重、状态取并集。

### 4. 来源与信任标记

`publisher_of(name)` 按命名空间分级（`mcp_market.py`）：

| 档 | 判据 | UI 标签 |
| --- | --- | --- |
| `official` | `io.modelcontextprotocol.*` | 官方命名空间 |
| `community` | `io.github.*` | GitHub 社区 |
| `domain-verified` | 其余反向域名 | 域名认证 |

⚠️ **三档的徽标刻意都用中性色系**（accent / 边框灰）—— 用绿色会暗示「更安全」，而三档**都只做命名空间校验、都没有代码审计**。这不是「安全评级」，只是「谁发布」。

`mcp_sources.json` 里的 `source` 另有一维：`market`（从市场装）/ `local`（手填）。列表里显示为「本地」或对应信任档。

### 5. 密钥处理：掩码回填

`env` / `headers` 里像密钥的键（`*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_AUTH` 等，`is_secret_key()`）在**回执里被脱敏成 `••••••`**。

编辑时的语义（`McpStore._merge_secret`，**三种情形容易搞反**）：

| 用户动作 | 回传值 | 落盘结果 |
| --- | --- | --- |
| 没动它 | `••••••` | **回填磁盘原值**（关键：不能把 `••••••` 真写进文件） |
| 改成别的 | 新值 | 用新值 |
| 在表单里删掉该项 | 该键不存在 | 删除 |

「试连」也必须照顾这一点：测**已保存**的条目时，前端传 `{name}`（后端从磁盘读**真实**配置），而不是传回执里那份掩码 —— 否则会把 `••••••` 当成真密钥发给 server，对需要鉴权的条目**必然假失败**。测**表单草稿**时才传 `{config}`。

> 本期密钥仍以**明文**写入 `mcp_servers.json`（文件权限 0600）。`${VAR}` 插值机制（`mcp_manager.interpolate`）已经存在但**至今零调用方**，把它真正用起来（值写 `~/.aigent/config/config.json`、配置里只留 `${KEY}`）列为后续增量。

### 6. 热生效链路

```
mcp_server_upsert / mcp_server_remove
  → 原子写 mcp_servers.json（tmp + os.replace）+ mcp_sources.json + chmod 0600
  → 遍历可观测的 MCPManager，逐个 maybe_reload()      # mtime 已变 → 精确 reconcile
  → 回读全量状态 → 回 mcp_config 信封
```

**不需要新增「重新加载」接口**：既有的 `maybe_reload()` 就是按 mtime 判定的 reconcile，写入后 mtime 一变，下一轮（或被主动调用时）自然生效。未构造的会话也不用管 —— `Agent.__init__` 的 `connect_all()` 会读到新配置。

`maybe_reload` 的 reconcile 是**精确的**：只断开配置变了/被删的、只连接新增/配置变了的条目，其余连接不动。这也是 §1.3「元数据必须旁路」的落实点。

### 6.0 试连通过后的定点重连（2026-10-08 补）

**症状**：开关开着时目标服务没启动 → 真实 `connect()` 失败、`_last_errors` 留下错因、列表显示「连接失败」；用户把服务起好后点「测试」→ 试连成功、列出 N 个工具，但**列表状态仍是「连接失败」**，必须把开关关一下再打开才恢复。

**根因**：`mcp_server_test` 的试连是**刻意无副作用**的 —— `_mcp_test_sync` 起一个临时 `__test__` session、握手列工具、**立刻 stop**，不落盘也不进任何 runtime 的 `_clients`。而列表状态读的是 `_mcp_runtime_snapshot()` → 各 manager 的 `_clients` / `_last_errors`，也就是**上一次真实 connect 的残留结论**。试连与状态刷新在原设计里是两个互不相干的世界。

「关一下再打开」之所以能修好，是因为那一次**真的写了盘** → mtime 变了 → `maybe_reload()` 才不空转（见下）。

**为什么不复用 `_reload_mcp_all_runtimes()`**：`maybe_reload()` 头一件事是比 mtime（`mcp_manager.py:586`），没变就 `return ""`。试连刻意不落盘 → mtime 不变 → **整个热重载是空转**，一个连接都不会重发。所以必须新开一条**按 name 定点重连**的路径（`_mcp_reconnect_one_sync`），自己调 `connect(name)`，不依赖 mtime。

```
mcp_server_test（{name} 寻址）
  → _mcp_test_sync：临时 session 握手 → stop → 结论 ok/失败
  → ok 且【寻址是 {name}】且【磁盘条目 enable=1】
      → _mcp_reconnect_one_sync(name)：逐个 manager，connected_names() 里就跳过，
        否则 connect(name)（模块级锁串行化）
      → 多发一帧 mcp_config（回读全量状态）
  → 回 mcp_test（含 refreshed）
```

**三条触发条件缺一即刷新会骗人**：

| 条件 | 缺了会怎样 |
| --- | --- |
| 试连 `ok` | 连不上还刷新 = 状态与事实相反 |
| 寻址是 `{name}` 而非 `{config}` | `{config}` 是**未保存的表单草稿**，重连用的是磁盘旧配置，会显示成「新配置已生效」 |
| 磁盘条目 `enable=1` | 禁用条目刷成「已连接」是假的（列表状态会按 `enable` 优先判成 `disabled`，两处打架） |

仍然**不落盘**：只调 `connect()`，不碰 `mcp_servers.json`。

**为什么是"真重连"而不是"前端把 status 刷成 connected"**：后者是撒谎 —— `_clients` 里依然没有该条目，模型这一轮真要调它的工具照样失败，而 UI 上看不出任何差别。**状态变 `connected` 必须是因为真的连上了**，这也是回执里 `refreshed` 字段存在的意义：前端据此显示「已同步到运行时」，否则用户会盯着"框是绿的、状态点是红的"怀疑自己看错了。

**前端零改动的关键**：主进程 `onEvent` 是先 `webContents.send` 再 `resolvePending`（`main/index.ts:296-300`），渲染层 `case 'mcp_config'` 本来就是**整份替换** → 多发一帧就能刷新列表，不需要新的信封或增量拼接。

**必须一起改的超时**：`mcpServerTest` 的 IPC 超时从 30s 抬到 **45s**。最坏路径 = 试连 15s + 重连 15s = 30s，**正好贴死** 30s 边界 → 偶发 promise 回 null、回执被丢弃，用户看到"点测试没反应"。与 `mcpServerUpsert` 的 30s 是同源问题，但这里要两次握手，只能再放宽一档。

**并发安全**：`MCPManager` 自身对 `_clients` **没有任何锁**。这里的重连可能与对话轮的 `maybe_reload()`、upsert 触发的热重载同时落在同一个 manager 上 → 两个线程各自 `connect()` 时后写的直接覆盖 `_clients[name]`，先建的那个 session 失去引用 → **stdio 子进程泄漏**；泄漏的子进程若持独占文件锁（cgc 的 kuzu），后续所有连接尝试必然失败。`_mcp_reconnect_lock` 就是为此存在的，粒度取「单条目重连」而非「整个 manager」，锁内只有一次 connect 握手。

**已知不解决的**：cgc 这类持**进程级独占锁**的 stdio 服务器，测试起一个进程、stop、再重连一次，若此刻别的 runtime 还持着锁，重连**仍会**失败 15s。这是先验事实不是 bug（多 manager 并存时必然只有一个连上），状态显示 `error` 是诚实的。

### 6.1 开关的乐观翻转（2026-09-30 补）

**缺陷**：点启停开关后 UI 无反馈，要关掉设置页重开才能看到新状态。

根因链（三层都有份，缺一层都不会发生）：

1. **回执慢**：`mcp_server_upsert` 的回执要等 §6 链路的 `maybe_reload()` 跑完才发 —— 对启用中的条目做 connect 握手，最长 `MCP_CONNECT_TIMEOUT`（默认 15s）/ 运行时，慢服务器 + 多会话可晚到十几秒（实测：禁用 3ms / 启用 2.8s / 慢服务器最坏 15s×N）。
2. **主进程 promise 5s 超时**：`request()` 默认 5s → 慢回执时 promise 路径回 `null`，权威回执只剩事件通道晚到那一条路。
3. **渲染层无乐观位**：`mcpConfig` 不变 → 整页不重绘 → 用户看到的就是"点了没反应"。

修复（三层各补一刀，`ws_bridge` / `mcp_manager` **零改动**）：

| 层 | 改动 | 理由 |
| --- | --- | --- |
| 渲染层 `McpSettings` | **乐观位 `optToggle`**：点击立即本地翻转 + 带 `.switch.pending` 呼吸样式；任何新 `mcpConfig` 回执到达即撤位（回执权威源，成功时视觉无跳变、被拒时 errors 已内联自然回退）；**30s 兜底**超时撤位 + `loadMcpConfig()` 重拉权威态 | 用户立刻看到点击生效；权威回执晚到时无缝收敛 |
| 主进程 `index.ts` | `mcpServerUpsert` / `mcpServerRemove` 超时 5s → **30s**（同 `mcpServerTest` 的放宽理由） | 保存动作会触发全运行时热重载握手，5s 必误报超时 |
| `styles/settings.css` | `.switch.pending .switch-knob` 呼吸动画 | "正在保存并热重载"的过程感，与禁用态区分 |

`disabled={saving}` **保持不动**：主进程 pending 表按 kind FIFO 配对且无 id，同 kind 并发会串台，不能放开连点。

CDP 实测（真实组件 + 桩后端，`/tmp/mcp-verify/run2.js`）：

| 断言 | 修复前 | 修复后 |
| --- | --- | --- |
| 快回执（10ms）UI 翻转延迟 | 128ms（本来就快） | 123ms |
| 慢回执（5.6s）点击后 UI 翻转 | **5896ms（期间零反馈）** | **72ms 乐观翻转 + pending**，5.6s 回执到达后撤 pending、状态收敛 |

### 7. 市场翻译规则

`packages[]` → `mcpServers` 条目（`mcp_market._config_from_package`）：

| registryType | 生成配置 |
| --- | --- |
| `npm` | `{type:"stdio", command:"npx", args:[...runtimeArguments, "<identifier>@<version>", ...packageArguments]}` |
| `pypi` | `{type:"stdio", command:"uvx", args:["<identifier>==<version>", ...]}` |
| `oci` / `nuget` / `mcpb` | **不支持** → 给具体原因（如「需要 Docker 镜像运行时，暂不支持一键安装，请手动填写配置」） |
| `remotes[]` | `{type: remote.type, url: remote.url, headers?}` |

实测踩到的三个细节：

1. **`runtimeHint` 常常缺失**（抽样的 10 条里 6 条没有）→ 必须能按 `registryType` 推导运行时（`npm→npx`、`pypi→uvx`）。
2. **参数顺序**：`<runner> <runtimeArguments> <packageSpec> <packageArguments>`。实测样本 `runtimeArguments=[{"value":"-y"}]` 正好印证 `-y` 在包名之前。
3. **空值 header 一律不写**：有条目声明 `Payment-Signature: ""`（占位符）。原样写进去会让服务端收到空签名而拒绝请求 —— 比「少一个 header」糟得多（后者用户还能在表单里补）。跳过时给 warning 说明。

**条目名撞车**：建议名取 registry name 最后一段（`io.github.acme/my-server` → `my-server`），与现有条目冲突则 `-2`、`-3`…（后端读一次 `load_config` 现算，前端零参与）。

### 8. 安全闸门：安装确认页

`install` 视图是整条链路上**唯一**需要用户点头的地方，两条不可妥协的约束：

1. **必须原样展示将要写入的 `command` / `args` / `url`**。这是「本机即将执行什么代码」的唯一凭据 —— 市场条目只经过命名空间校验，用户唯一能判断的依据就是这段原文。不折叠、不摘要、不只显示包名。
2. **必填环境变量没填齐时，「确认安装」必须 disabled**。后端只校验传输/命令这类**结构**，不会（也不该）知道某个 env 是业务必填 —— 漏了这条就能装出一个永远连不上的条目，而用户看不出为什么。

> 这个缺陷是实测抓到的：`disabled` first version 只写了 `saving || testing || !name.trim()`，漏了 `missingRequired.length > 0`，而旁边的「测试连接」检查了。CDP 断言 `M4` 直接把它逮住。

其他约束：
- 不提供「记住我的选择」之类的便捷开关 —— stdio 是任意代码执行，每次都该看一眼。
- 安装后**默认启用**（用户在确认页看过原文并点过确认，那一下就是授权）。

***

## 三、协议

### 3.1 命令与信封（10 条命令 / 5 个信封）

全部**点对点**：只回发起窗口、**不广播** —— 与 `permission_config` / `sandbox_config` 同一约定（广播会冲掉另一个窗口正在编辑的草稿）。因此都**不进** `isKnownAgentEvent` 白名单。

| 命令 | 载荷 | 回执信封 | 说明 |
| --- | --- | --- | --- |
| `mcp_config_get` | — | `mcp_config` | 读全量：原始条目 + 元数据 + 连接状态 + **本地包列表** |
| `mcp_server_upsert` | `{name, config, original_name?, meta?}` | `mcp_config` | 新增/编辑/重命名/启停（`original_name ≠ name` = 改名） |
| `mcp_server_remove` | `{name}` | `mcp_config` | 删条目（含旁路元数据，**不动包**） |
| `mcp_server_test` | `{config}` **或** `{name}` | `mcp_test`（+ 条件性追加一帧 `mcp_config`） | **一次性试连，不落盘**。`name` = 测已保存条目（取真实密钥）。命中「`name` + 已启用」时**额外做一次定点重连并刷新状态**（见 §6.0） |
| `mcp_market_search` | `{query?, cursor?, limit?}` | `mcp_market` | 代理官方 registry 搜索 |
| `mcp_market_resolve` | `{item}` | `mcp_market_plan` | **纯翻译，不落盘** —— 供安装确认页展示 |
| `mcp_pkg_resolve` | `{name, version?}` | `mcp_pkg_plan` | **纯解析，不落盘、不下载** —— 版本/哈希/依赖树/脚本清单 |
| `mcp_pkg_install` | `{name, version, bin?, allow_scripts?}` | `mcp_config` + `pkg_action` | **只下载 + 校验，不写配置**（2026-10-07） |
| `mcp_pkg_remove` | `{slug}` | `mcp_config` + `pkg_action` | 删 `pkgs/<slug>/`，**不动条目** |
| `mcp_pkg_verify` | `{slug}` | `mcp_config` + `pkg_action` | 按需复核：哈希对账 + bin 是否还在目录内 |

**为什么 `mcp_server_upsert` 是单条而不是整份覆盖**：MCP 是「多条目集合」，整份覆盖在多窗口场景下误伤面太大（对比 `permission_config_save` 的整份语义 —— 那里是一份单值配置，整份覆盖没有歧义）。

**为什么 4 条本地包命令里 3 条共用 `mcp_config` + 一个 `pkg_action` 字段**：那三条都会改变磁盘状态（装好了 / 删掉了 / 复核过），前端每次都需要刷新两份列表（条目 + 本地包）。复用同一封信封、把动作结果挂在 `pkg_action` 上，就不必为每个动作新开一个信封再各写一遍 store 整份替换逻辑。只有 `mcp_pkg_resolve` 是纯查询（不改变任何状态），单独走 `mcp_pkg_plan`。

### 3.2 `mcp_config` 载荷

```jsonc
{
  "path": "/Users/me/.aigent/mcp/mcp_servers.json",
  "sources_path": "/Users/me/.aigent/mcp/mcp_sources.json",
  "exists": true,
  "sessions": 1,                       // 可观测的 MCPManager 数（全局 Agent + 各会话）
  "summary": { "total": 5, "enabled": 4, "connected": 1, "packages": 2 },
  "pkgs_dir": "/Users/me/.aigent/mcp/pkgs",   // 2026-10-07
  "servers": [{
    "name": "local-echo",
    "enable": true,
    "transport": "stdio",              // 由后端按与 mcp_manager 同源规则推断
    "command": "/abs/.venv/bin/python",
    "args": ["-m", "mcp_servers.echo_server", "stdio"],
    "env": { "API_KEY": "••••••" },    // 密钥已脱敏；原样回传 = 不改
    "cwd": "/abs/repo",
    "url": null,
    "headers": null,
    "source": "local",                 // local | market | local-pkg
    "market_id": null,
    "publisher": null,                 // official | community | domain-verified
    "installed_at": null,
    "pkg": null,                       // 由本地包提供时为 {slug,name,version,command,integrity,registry}
    "status": "connected",             // 五态，见 §2.3
    "tools": ["echo", "add", "delete_thing"],
    "tool_count": 3,
    "last_error": null
  }],
  "packages": [{                       // 2026-10-07：本地已下载的包
    "slug": "modelcontextprotocol__server-filesystem@1.2.3",
    "name": "@modelcontextprotocol/server-filesystem",
    "version": "1.2.3",
    "dir": "/Users/me/.aigent/mcp/pkgs/modelcontextprotocol__server-filesystem@1.2.3",
    "command": ".../node_modules/@modelcontextprotocol/server-filesystem/dist/index.js",
    "bin": "mcp-server-filesystem",
    "registry": "https://registry.npmjs.org/",
    "integrity": "sha512-…",
    "dep_count": 42,
    "scripts_allowed": false,
    "size_bytes": 8800000,
    "status": "ok",                    // ok | incomplete | broken
    "referenced_by": ["fs-docs"]       // 哪些条目在用（卸载前必须提醒）
  }],
  "applied": true,
  "warnings": [],                      // 非阻断（如工具前缀撞车）
  "errors": [],                        // 校验/落盘错误 → 页面内联展示，不 toast
  "msg": "已保存「local-echo」",
  "pkg_action": { "action": "install", "ok": true, "command": "…" }   // 见 §3.1
}
```

`mcp_test` 载荷：`{ok, error, tools[], tool_count, resource_count, elapsed_ms, refreshed?, refresh_connected?}`。后两个字段是 2026-10-08 的定点重连结果（见 §6.0）：`refreshed=true` 表示后端顺势重连过、前端应显示「已同步到运行时」；草稿 / 禁用条目 / 试连失败时为 `false`，状态不刷新。
`mcp_market` 载荷：`{items[], next_cursor, query, error, cached, elapsed_ms}`。
`mcp_market_plan` 载荷：`{ok, name, config, env_required[], package_args[], pkg, warnings[], unsupported, error}`。
其中两个 2026-10-07 新增的字段是给「下载到本地」用的：
- **`package_args`** —— **只属于服务本身**的启动参数（`["-y","pkg@1.0.0","/tmp"]` 里只有 `/tmp`）。`command` 换成包内 bin 后 `-y` 与包名都不再适用，但这部分必须原样保留。**刻意在后端算好**：让前端去切 `config.args` 会制造第二处解析规则。
- **`pkg`** —— 包坐标 `{registry_type, identifier, version, local_installable}`，前端靠它去问 `mcp_pkg_resolve`。同理，前端自己去读 `item.packages` 也是第二处解析规则。`local_installable` 目前只认 npm。

`mcp_pkg_plan` 载荷（`mcp_pkg_resolve`，**不落盘、不下载**）：

```jsonc
{
  "ok": true,
  "spec": "@modelcontextprotocol/server-filesystem@1.2.3",  // 恒为精确版本
  "name": "…", "version": "1.2.3", "slug": "…", "dir": "/Users/me/.aigent/mcp/pkgs/…",
  "registry": "https://registry.npmjs.org/",   // 必须在确认区明文展示
  "integrity": "sha512-…", "shasum": "…", "tarball": "…",
  "description": "…",
  "direct_dep_count": 4,
  "dep_count": 42,          // 整棵依赖树（npm install --dry-run）；取不到 = null → UI 显示"未知"
  "scripts": { "postinstall": "node build.js" },       // 全部脚本
  "install_hooks": { "postinstall": "node build.js" }, // 其中安装期执行的那三个
  "has_scripts": true,      // 为真时确认区必须红字列出脚本名
  "bins": ["mcp-server-filesystem"], "default_bin": "mcp-server-filesystem",
  "pinned_from_latest": false,   // true = 条目没给版本，本次由 latest 钉死
  "already_installed": null,     // 非空 = 本机已有同版本，可复用不必再下
  "warnings": [], "error": ""
}
```

### 3.3 三条协议级约束

1. **逐字段兜底、绝不上抛**：`handle()` 的命令分发链**没有兜底 try/except**（见 19 篇的同类说明），这里抛出去会直接掀掉整条 WS 连接。任何探测失败都降级成中性值，回执照发 —— 否则前端会永远停在「读取 MCP 配置…」。
2. **校验失败不额外发 `error` 信封**：那一封会被 store 的 `case 'error'` 当全局 toast 弹出来，而设置页的约定是**错误内联展示**（用户要对着文本改，toast 一闪而过等于没提示）。
3. **`mcp_market` / `mcp_market_plan` 刻意没有事件分支**（与 `mcp_config` 不同）：市场搜索要**按页累加**，而事件分支只会把已累加的多页覆盖成最后一页。这与 `refs` / `file_content` 这些「点对点回执由调用方后处理」的先例一致，由 store 的 promise 路径负责。`mcp_config` 保留事件分支是有用的冗余：IPC 超时后回执仍能到达并刷新界面。

### 3.4 超时（**实测数据，别照抄默认值**）

| 命令 | IPC 超时 | 依据 |
| --- | --- | --- |
| `mcp_server_test` | **30s** | 真实起子进程 + 等握手，最长 `MCP_CONNECT_TIMEOUT`（15s）+ 余量 |
| `mcp_market_search` | **30s** | 实测官方 registry 单次 0.9s ~ 17s（波动极大） |
| `mcp_pkg_resolve` | **30s** | 要打两次网络：`npm view` + `--dry-run` 数依赖树 |
| `mcp_pkg_install` | **180s** | 真实下载整棵依赖树（含 tarball），网络差时可轻松超 60s |
| `mcp_pkg_remove` / `mcp_pkg_verify` | **30s** | 遍历 `node_modules` 算哈希 / 删目录 |
| 其余 | 默认 5s | 纯本地读写 |

后端的请求超时另有两层，**都要放宽**，只放前端必然误报：
- `MCP_MARKET_TIMEOUT`（默认 20s）、`MCP_CONNECT_TIMEOUT`（默认 15s）
- `MCP_RESOLVE_TIMEOUT`（默认 60s）、`MCP_INSTALL_TIMEOUT`（默认 180s，超时整组杀进程）

npm 侧的另两个可调项：`MCP_NPM_PATH`（打包后 PATH 可能与开发机不同，默认 `which npm`）、`MCP_NPM_REGISTRY`（默认官方源，非 https 一律退回官方源并记警告）。

***

## 四、落地

### 4.1 文件清单

**后端（新增 2 个模块 + 1 处路径常量 + 1 处引擎层最小改动）**

| 文件 | 改动 |
| --- | --- |
| `agents/mcp_store.py` | **新建**：配置 store（原子写 + 锁 + 掩码回填 + 撞名检测）；2026-10-07 加 `local_pkg_meta()` 与 `list_servers` 的 `pkg` 透传 |
| `agents/mcp_market.py` | **新建**：registry 客户端 + 归一化 + 翻译层；2026-10-07 `resolve` 增出 `package_args` 与 `pkg` 两个字段，`_config_from_package` 改 4 元组 |
| `agents/mcp_installer.py` | **新建（2026-10-07）**：本地包安装器 —— 规格白名单、目录命名、`npm view` 解析、dry-run 依赖树、安装（强制安全 flag）、哨兵、装后三重校验、bin 解析、列表 / 卸载 / 复核。**全部子进程调用走可注入 runner**（测试接缝） |
| `agents/paths.py` | 新增 `MCP_SOURCES = MCP_DIR / "mcp_sources.json"`；2026-10-07 加 `MCP_PKGS_DIR = MCP_DIR / "pkgs"` 并在 `ensure_dirs()` 补建 |
| `agents/mcp_manager.py` | **5 行**：`connect()` 失败时记 `_last_errors[name]`、成功时清除；新增只读 `last_error(name)` |
| `agents/ws_bridge.py` | 6 条命令分支 + 6 个辅助函数（`_mcp_store` / `_mcp_managers` / `_mcp_runtime_snapshot` / `_mcp_config_payload*` / `_reload_mcp_all_runtimes` / `_mcp_test_sync`）；2026-10-07 再加 4 条本地包命令，`_mcp_config_payload_sync` 增 `packages` / `pkgs_dir` / `pkg_action`；2026-10-08 加 `_mcp_reconnect_one_sync` + `_mcp_reconnect_lock`（试连后的定点重连，见 §6.0） |
| `tests/test_mcp_installer.py` | **新建（2026-10-07）**：30 例，假 runner 不真跑 npm |
| `.env.example` | 补 6 个可调参数（`MCP_MARKET_URL` / `_TIMEOUT` / `_PAGE_SIZE` / `_CACHE_TTL` / `MCP_CONNECT_TIMEOUT` / `MCP_CALL_TIMEOUT`）；2026-10-07 再加 `MCP_NPM_PATH` / `MCP_NPM_REGISTRY` / `MCP_RESOLVE_TIMEOUT` / `MCP_INSTALL_TIMEOUT` |

**引擎层的 5 行是本期唯一的「改引擎」**，且**只加可观测性**：不改 `load_config` / `maybe_reload` / `assemble_*` 的任何行为。理由：连接失败原先只 `print`（`mcp_manager.py:460`），UI 只能显示「未连接」却说不出**为什么**，用户填错 URL 时无从排查。

**前端（3 个新组件 + 6 处接线）**

| 文件 | 改动 |
| --- | --- |
| `components/Settings/McpSettings.tsx` | **新建**：容器 + 列表视图（四视图路由）；2026-10-07 加「本地包」区块（列表 / 校验 / 卸载） |
| `components/Settings/McpServerForm.tsx` | **新建**：新增/编辑表单（传输三分支 + KV 编辑器 + 试连） |
| `components/Settings/McpMarketView.tsx` | **新建**：市场浏览（防抖搜索 + 分页追加） |
| `components/Settings/McpInstallView.tsx` | **新建**：安装确认（配置原文 + env 表单 + 必填拦截）；2026-10-07 加「安装方式」二选一 + 本地包计划区 + 两步式下载→写配置 |
| `protocols/agentProtocol.ts` | `ControlKind` +6、`UiEvent` +4、新增 8 个类型；2026-10-07 `ControlKind` +4、`UiEvent` +1、`McpConfigResult` 增 `packages`/`pkgs_dir`/`pkg_action`、`McpMarketPlan` 增 `package_args`/`pkg`、新增 `McpLocalPkg`/`McpPkgPlan`/`McpPkgActionResult` |
| `lib/browserAgent.ts` / `preload/index.ts` / `preload/index.d.ts` / `main/index.ts` | 各 +6 个方法 / IPC 通道（含 2 处超时放宽到 30s）；2026-10-07 各再 +4（`mcpPkgResolve` 30s / `mcpPkgInstall` **180s** / `mcpPkgRemove` 30s / `mcpPkgVerify` 30s） |
| `store/agentStore.ts` | `SettingsTab` + `'mcp'`；8 个 state + 8 个 action；事件分支 `case 'mcp_config' \| 'mcp_test'`；懒加载与清理。2026-10-07 再加 3 个 state（`mcpPkgPlan` / `mcpPkgBusy` / `mcpPkgAction`）+ 4 个 action + `case 'mcp_pkg_plan'` |
| `components/SettingsModal.tsx` | `NAV` 加 `mcp`（沙盒之后）+ 渲染分支 |
| `styles/settings.css` | 追加 `.mcp-*` / `.mcp-market-*` / `.mcp-transcript*` 样式 |

### 4.2 前端四条硬约束

1. **回执为权威源，整份替换**：`mcp_config` 三条命令共用同一信封，每次都回读**全量** → `mcpConfig` 直接换掉，不做增量拼接（拼接会留下已删条目的残影）。
2. **错误内联、不 toast**（同 §3.3-2）。
3. **读配置失败 ≠ 没有条目**：`servers: [] + errors` 时绝不能渲染成「还没添加过」—— 那会诱导用户重新添加，反而覆盖掉磁盘上还在的内容。实测断言 `I1/I2` 专门锁死了这条（显示「配置读取失败，请先处理上面的错误。」而非「还没有添加任何 MCP 服务」）。
4. **`view === 'install'` 且无在途请求时不能停在加载态**：给一条明确的出路（「安装信息已失效，请重新选择条目」+ 返回按钮）。权限页与沙盒页都踩过「塌成加载态」这个坑。但死端文案只能是最后防线——store 的 `resolveMcpInstall` 收到 `null`（IPC 超时）时必须合成失败计划（2026-09-30 修，同 24 篇 §六 #19），让确认页走"无法安装 + 原因"分支。

### 4.3 一个视图状态细节：`toConfig` 必须显式重建

列表行上的启停开关要把条目还原成后端要的配置体。**不能直接回传列表条目对象**：展示字段名是 `transport`，而配置键是 `type`；直接回传会在 `_clean_entry` 的白名单里丢掉 `type`，导致传输类型被误判成默认值。

`toConfig()` 显式重建，实测断言 `G1` 锁的就是这条：

```json
{ "name": "zotero-mcp", "cfg": { "type": "streamable-http", "enable": true, "url": "http://127.0.0.1:23120/mcp" } }
```

***

## 五、验证

### 5.1 后端

| 项 | 结果 |
| --- | --- |
| 既有单测 | **448 例 OK**（`env -u PYTHONPATH -u CODEBUDDY_BROKERED_FS_HOOK_ENABLED .venv/bin/python -m unittest discover -s tests`）。⚠️ 跑之前设 `HOME=/tmp/<空目录>`：本机 `~/.aigent/mcp/mcp_servers.json` 里启用了真实的 codegraphcontext，**每个构造 Agent 的用例都要等一次 15s 握手**，全量从 8s 变 90+ 分钟 |
| 真实 stdio 连接 | 发现 3 工具 + 1 资源；无监听端口正确判失败 |
| 真实 WS 端到端（批次 1，6 项） | `get` / 非法 upsert（走 errors 而非 error 信封）/ 试连成功与失败 / 写入 + 脱敏 + 0600 / 撞车警告 / 删除 —— 全 PASS |
| 真实 registry（批次 2，6 项） | 搜索 5 条（6.5s）/ npm 翻译 + 撞名建议 `remote-filesystem-2` + 8 个 `env_required` / 远程端点翻译 / `oci` 降级给原因 / 垃圾载荷后**连接仍活** —— 全 PASS |
| **本地安装单测（2026-10-07）** | `tests/test_mcp_installer.py` **36 例 OK**（假 runner，不真跑 npm） |
| **本地安装真机端到端（2026-10-07）** | 用 `registry.npmmirror.com`（官方源在本机**连不通**，见 §8.9）：<br>· `@modelcontextprotocol/server-everything@2026.8.31` —— 解析钉死版本 + `sha512` 哈希 + dry-run 数出 104 个包；安装 19.2s；bin 解析到 `dist/index.js`；**用既有 `MCPServerSession` 真连上，发现 13 工具 / 7 资源**；`verify` 通过；卸载后目录清空<br>· `cowsay@1.6.0` —— 5.5s / 29 包 / 二次安装 `reused=true` 不再下载 / 卸载释放 1.13 MB<br>· 31 个非法规格（`latest`/`^`/`git+`/`file:`/`--prefix=`/`../../`…）全部被拒 |

### 5.2 前端（headless Chromium + 裸 CDP）

打包**真实**组件 + 桩 `window.agent`，真鼠标点击 / 真文本输入（`Input.insertText`）。

**MCP 管理面（2026-09-30）—— 62 条断言全 PASS**：

| 断言组 | 关键结果 |
| --- | --- |
| A 列表渲染 | 5 条条目；**五态文案全部正确**（已连接/连接失败/已禁用/待会话启动后连接/未连接）；绿点 1 / 红点 1 |
| B 详情展开 | 展开出工具 chips 3 个 + 命令原文；再点收起 |
| C 表单 | 三张传输卡等宽（216×85 ×3）；stdio↔http 字段正确切换；受控输入生效；试连载荷 `{type:"stdio", args:["-y","@acme/x@1.0.0","/tmp"]}` |
| D 保存 | 回列表 + 新条目出现；载荷 `{name, type:"stdio", enable:true}`（`original_name` 正确缺省） |
| E 保存被拒 | **仍在表单**（不是被踢回列表）+ 内联红字「stdio 传输必须提供可执行的 command」+ 未落盘 |
| F 密钥掩码 | 编辑带出 `[["GITHUB_TOKEN","••••••"],["LOG","info"]]`（掩码保真） |
| G 启停开关 | `type` 未被 `transport` 顶掉（见 §4.3） |
| H 删除 | 首次点击仅武装（`remove` 调用数 **0**），二次点击才调用 |
| I 读失败态 | 「配置读取失败…」而非「还没有添加任何 MCP 服务」 |
| J/K 几何 | 无横向溢出（scrollW 712 = clientW 712）；4 个输入框宽 664 不溢出；**行高 38px 且操作与主信息同轴**（内联改造前 ~70px） |
| L 市场 | 进页自动搜索 3 条；来源徽标/版本/传输齐全；不可安装条目按钮 **disabled**；风险文案含「没有代码审计」；**翻页 3→4 条且重复项被去重**；**搜失败只显示错误、不显示「没有匹配」**；真空结果才说「没有」 |
| M 安装确认 | 命令原文 `npx -y remote-filesystem-mcp-server@0.1.5`；必填未填 → 确认安装+测试连接**双双 disabled** 且提示缺哪一项；填完解锁；试连带上用户填的 env；安装载荷含 `meta{source:market, market_id, publisher}` + `enable:true` + 用户填的 env |
| N 翻译失败 | 给出原因（Docker）且**不给安装按钮** |

**本地包安装（2026-10-07，`/tmp/mcp-pkg-verify/`）—— 76 条断言全 PASS**：

| 断言组 | 关键结果 |
| --- | --- |
| P1 本地包区块 | 标题计数 `本地包（2）`；行内 `cowsay / 1.6.0 / 1.1 MB / 已就绪 / 1 个条目在用`；`broken` 行显示「文件缺失 / 无条目引用」；**两段删除首点 `pkgRemove` 调用数 = 0**，二点 = 1；被引用时 title 点名 `fs-docs`；校验回执文案正确；无横向溢出 |
| P2 下载到本地 | 默认选中「下载到本地」（不是 npx）；解析入参 `{name:"cowsay", version:"1.6.0"}`（**钉死版本**）；展示规格/registry/「共 29 个包」；**下载前 `pkgInstall` 调用数 = 0**；缺必填 → 确认与测试**双双 disabled**，填完解锁；按钮文案「下载并安装」；点下去 → `pkgInstall{allow_scripts:false, bin:"cowsay"}`，随后 `upsert.config.command` = **包内绝对路径（不再含 npx）**、`args` = `["/tmp"]`（只留服务自己的参数）、`meta.pkg.slug` 有值、`meta.source` 仍是 `market`、env 带上用户填的值 |
| P3 解析失败不阻断 | 内联原因 + 「由 npx 在连接时拉取」退路；确认按钮**保持禁用**（本地路走不通）；切 npx 后解锁、文案回「确认安装」、`pkgInstall` 调用数 **0**、command 仍是 `npx` 且 args 完整（含包名）、`meta` 不带 `pkg` |
| P4 已有副本复用 | 「本机已装过这个版本…不会重新下载」提示出现；预览命令已是绝对路径；按钮文案「确认安装」；`pkgInstall` 调用数 **0** |
| P5 脚本开关 | 红色警示块 + `postinstall → node build.js` 原文；**默认不勾选**，`pkgInstall.allow_scripts = false`；勾选后再确认 → `allow_scripts = true` |

视觉（`shots/`，逐张人工核对）抓到两处**断言看不见的缺陷**，都已修：

1. **预览与文案自相矛盾**：选了「下载到本地」但还没下载时，「将要写入的配置」里显示的是 `npx -y cowsay@1.6.0 /tmp`，旁边却写着"直接执行下面这个绝对路径"。而这一区是整页唯一的凭据 —— 显示一条**根本不会被写入**的命令，比留空更糟。改为：命令位留空并写「（下载完成后填入）」，参数位照实显示（那部分不会变）。
2. **`cmdPreview` 把 args 拼进了命令位**：命令还没填时渲染成 `" /tmp"`（前导空格 + 参数），既难看又掩盖了"这里确实还没有命令"。参数本来就有独立一行 → 拆开。

> 另有一处是**断言抓到的死代码**：复用提示的判据写成 `already_installed && !readyCommand`，而 `readyCommand` 恰恰是从 `already_installed.command` 推出来的 —— 两者不可能同时成立，提示一个字都不会渲染。改为判"当前这个 command 是否正好来自它"。

视觉：四个视图各截一图人工核对。据此修掉两处：

- 安装页左侧键名（命令/参数/传输）原先用 `--color-text-secondary`（浅色主题下是深灰）压在深色代码块上 → **对比度不足**，改为基于 `--color-code-fg` 的 `opacity: 0.72`。
- 市场条目无 `repository` 时（仓库链接不渲染），「暂不支持」按钮掉到左边 → 加 `margin-left: auto` 恒靠右；并把不可安装按钮从「暗掉的 btn-primary」换成中性描边 `.btn`（暗紫实心仍像可点，读不出「不可用」）。

***

## 六、关键取舍与踩坑

| # | 事项 | 结论 |
| --- | --- | --- |
| 1 | UI 列表数据源 | **必须**读原始文件（含 `enable:0`）。用 `load_config()` 会让被禁用条目永远不可见、也就无法重新启用 |
| 2 | 元数据落点 | 旁路 `mcp_sources.json`。内嵌进条目会让「改元数据」被 `maybe_reload` 判成「配置变了」→ 虚假重连 |
| 3 | 状态汇总 | `_mcp_managers()` 必须含**全局 Agent**（它不在 `registry.all_runtimes()` 里），否则「刚启用、未开会话」被显示成未连接 |
| 4 | 试连寻址 | 测已保存条目传 `{name}`（磁盘真实密钥）；传回执里的掩码会必然假失败 |
| 5 | 掩码回填 | `incoming[key] == 掩码` → 回填磁盘原值。搞反会把用户没改的密钥替换成 `••••••` |
| 6 | 参数顺序 | `<runner> <runtimeArguments> <packageSpec> <packageArguments>`（实测 `-y` 在包名之前） |
| 7 | `runtimeHint` | 常常缺失（10 条里 6 条）→ 必须能按 `registryType` 推导 |
| 8 | 空值 header | **不写**。`Payment-Signature: ""` 原样写进去会让服务端拒请求 |
| 9 | 市场超时 | 前端 IPC 30s + 后端 20s **两层都要放宽**。官方 registry 实测 0.9~17s |
| 10 | store 事件分支 | `mcp_config` 保留（幂等冗余，IPC 超时后仍能刷新）；`mcp_market` **必须没有**（否则多页被覆盖成最后一页） |
| 11 | 必填 env | 「确认安装」必须 disabled。后端只管结构，业务必填只有前端知道 |
| 12 | 传输键名 | 列表用 `transport`、配置用 `type` → 回传前必须显式重建（`toConfig`） |
| 13 | 同 kind 并发 | 主进程 `pending` 表按 kind FIFO 配对且无 id → 试连/搜索按钮进行中**必须 disabled** |
| 14 | 市场不落盘缓存 | preview 期 schema 会变（实测同响应两版 schema），固化到磁盘等于留下坏结构 |
| 15 | 徽标配色 | 三档信任标记**都用中性色**。用绿色会暗示「更安全」，而三档都没有代码审计 |
| 16 | 包名先于 npm 调用校验 | `plan()` 会先发 `npm view <name>@latest`，那一刻名字还没过规格正则。argv 数组**挡不住** `-` 开头的参数 → 跳过校验则 `--prefix=/etc` 被 npm 当选项吃掉（唯一的真实注入点） |
| 17 | `--ignore-scripts` 不可省 | 生命周期脚本是**装包期**任意代码执行，早于任何握手。去掉它等于把「确认弹窗看命令行」这道闸门整个废掉 |
| 18 | 干运行失败不拖垮计划 | `dep_count` 取不到就显示「未知」而不是编一个数 —— 传递依赖是供应链攻击的主要载体，报少了给的是虚假安心感 |
| 19 | 下载与写配置分两步 | `mcp_pkg_install` 不写配置，写条目仍走 `mcp_server_upsert`。否则下载失败会留下一条指向不存在文件的死条目 |
| 20 | 条目与包解耦 | 删条目不动包、删包不动条目（只提示哪些条目会失效）。耦合起来就会出现「改个名字顺手把包删了」 |
| 21 | 校验不做成每次连接都跑 | 复核要遍历整棵 `node_modules`；它防的是「装完之后被人动过」这种低频事件 → 只做按需按钮 |

***

## 七、后续增量

按优先级排：

1. **`${VAR}` 密钥分离** —— 值写 `~/.aigent/config/config.json`（扁平键，启动并入 env）、`mcp_servers.json` 里只留 `${KEY}`，让既有的 `interpolate()` 真正被用起来。当前它**零调用方**。
2. **从其它客户端导入** —— 读 Claude Desktop / Cursor / VS Code 的 mcp 配置批量导入。成本低、收益高，能立刻捞到用户已有的配置。
3. **市场磁盘快照 + 中文增强** —— 对应 §1.1 被压掉的「本地缓存层」选项；解决国内访问慢与全英文描述。需要先接受「preview 期结构可能变」的维护成本。
4. **stdio 沙盒包裹** —— 对市场来源的 stdio 子进程套用 `sandbox.py` 的 Seatbelt/bwrap。注意现有沙盒**只包 `run_bash`**，扩展面比看上去大。
5. **端口/进程探测** —— 自动发现本机已启动的 MCP，替代手填 URL。收益有限（手填 + 试连已够用）、误报成本高，优先级最低。
6. **`MCPManager` per-Agent 的资源问题** —— 每个会话构造都会 `connect_all()` 起自己那份 MCP 子进程，会话一多资源占用线性增长。这是**既有行为**（非本期引入），但本期把状态汇总打通后它变得可见了，建议单独评估。

本地包安装（§八）自身的后续，按优先级：

7. **安装进度流式推送** —— 现在 `mcp_pkg_install` 是阻塞式（最长 180s，UI 只有"正在下载…"）。后端→前端的新信封不改 IPC 三件套（`onEvent` 全量转发），往发起窗口多推几个 `mcp_pkg_progress` 即可，成本低、体感提升大。
8. **镜像源白名单 + 显式设置项** —— 现在换源只能改 `~/.aigent/config/config.json`（§8.9 实测官方源在本机不可达，用户必须改）。应在设置页给一个可见的输入框 + 常见镜像下拉，让"我信任谁"这件事有界面。
9. **PyPI 本地安装** —— 要落 `uv tool install` / `pip --target`，与"统一 uv 管理、禁止擅自装包"的口径冲突，需先定规矩（这也是 `local_installable` 目前只认 npm 的原因）。
10. **包的升级 / 回滚** —— 现在是"同版本幂等复用、不同版本并存"，没有"检查更新"与"切回上一版"的入口。
11. **依赖去重** —— 一个包一份 `node_modules`（同包不同版本要各存一份，体积翻倍）。要么做共享 store，要么接受并只给一个总的磁盘占用提示。

***

## 八、本地包安装（2026-10-07 增补）

对应 §1.2 放开的那个边界。**动机不是"更安全"，而是"可见"**：`npx -y` 已经把「执行第三方代码」这件事做了，只是藏在你看不见的地方 —— 包版本随时可漂、装了什么不知道、想删没入口。本地安装把同一份风险摆到台面上：**版本钉死、哈希可查、目录可删**。

### 8.1 安全模型（本模块存在的全部理由）

按攻击面逐条设防，**全部是默认行为**，不靠用户记得：

| 攻击面 | 对策 |
| --- | --- |
| 路径穿越 / 参数注入（`../../x`、`--prefix=/etc`、`-g`） | `SPEC_RE` 白名单正则；包名必须以字母数字或 `@` 开头，`:`/`/`/`\` 全不在字符集内 |
| **选项注入（唯一的真实注入点）** | `plan()` 在缺版本时会先发 `npm view <name>@latest`，那一刻名字还没过 `SPEC_RE`。argv 数组**挡不住** `-` 开头的参数 → 必须先用 `NAME_ONLY_RE` 校验包名，否则 `--prefix=/etc` 会被 npm 当成选项吃掉 |
| 非 registry 依赖形态（`git+https:`、`https://…tgz`、`file:`、`npm:alias`） | 同上正则天然挡掉（不含 `:` 与 `//`） |
| 浮动版本（`latest` / `^1` / `~1.2` / `1.x` / `1.0`） | 只收精确 `X.Y.Z[-pre][+build]`；缺版本时向 registry 问一次 latest **并钉死** |
| `postinstall` 等生命周期脚本 RCE | 强制 `--ignore-scripts`（CLI flag 优先级高于任何 `.npmrc` / 环境变量） |
| 用户 `.npmrc` 把 registry 偷换成镜像或私服 | 强制 `--registry=<配置值>`，且该值在确认页**明文展示**，绝不静默读取 |
| 包 `bin` 字段写 `../../..` 逃逸出安装目录 | 装后对 bin 做 `realpath` 包含检查，越界即拒（只查「链接存在」挡不住） |
| 供应链篡改 / 装后被本地替换 | `resolve` 时取 `dist.integrity`，装后与 `package-lock.json` 对账，不符即**判定安装失败** |
| 装到一半崩溃 / 关窗口 | 先落 `.install-incomplete` 哨兵，成功才删；带哨兵的目录对 UI 是 `incomplete`，**绝不当成可用包** |
| 卸载时路径逃逸 | slug 必须单段 + 父目录 `realpath` 包含检查，才允许 `rmtree` |
| 超时挂死 | 子进程独立进程组 + 硬超时，超时**整组**杀（npm 会 fork 子进程，只杀直接子进程会留孤儿） |
| 模型自主装包 | **不给模型任何工具入口**，只走设置页 IPC。将来若要开，必须过 `PermissionGate` |

三条**刻意不做**（避免被当成遗漏）：

1. **不隔离 `.npmrc`**（不传 `--userconfig`）—— 会连带杀掉用户的 proxy 配置，而 proxy 本身就是设计上的 MITM，隔离与否并不改变这一事实。
2. **不进沙盒跑 npm** —— 要联网、要写 `~/.aigent`，Seatbelt/bwrap 那套模板会挡死。
3. **不做 PyPI 本地安装** —— 要落 `uv tool install` / `pip --target`，与「统一 uv 管理、禁止擅自装包」的口径冲突，需单独定规矩（`local_installable` 因此只认 npm）。

### 8.2 安装参数（**不要因为"某个包装不上"就删掉其中任何一个**）

```
npm install <name>@<version> --prefix <pkgs/<slug>> \
    --ignore-scripts --registry <配置值> \
    --no-audit --no-fund --save-exact --loglevel=error
```

| 参数 | 作用 |
| --- | --- |
| `--prefix <专属目录>` | 一个包一个目录，卸载 = 删这个目录，没有跨目录副作用 |
| `--ignore-scripts` | 生命周期脚本 = **装包期**任意代码执行，早于任何握手。仅当用户在那个红字开关上显式勾选时才去掉 |
| `--registry <配置值>` | 命令行 flag 优先级高于 `.npmrc` / 环境变量 → 堵住静默换源 |
| `--save-exact` + `package-lock.json` | 留下可对账的哈希凭据（`_read_lock_integrity` 读的就是它） |
| `--no-audit --no-fund` | 少两个无关的网络往返，缩短暴露窗口 |

### 8.3 目录名与两种资源的关系

`slug` 由 `包名@版本` 推导：`@scope/pkg@1.2.3` → `scope__pkg@1.2.3`（`/` 折成 `__`、去掉 `@` 前缀）。**斜杠只可能出现在 scope 分隔处**，换掉它即保证 slug 恒为**单段路径** —— 这是卸载路径安全检查的前提。

目录名冲突的极小概率（`@a/b` 与 `a__b` 会撞）由**元数据身份校验**兜底：装之前先读 `aigent-meta.json`，`name`/`version` 对不上就响亮失败，绝不把别人的包当自己的用。

`<包目录>/` 里有两套文件：npm 写的（`package.json` / `package-lock.json` / `node_modules`）和我们写的（`aigent-meta.json` / `.install-incomplete`）。我们那份**不叫 `.aigent-meta.json` 这类隐藏名**是有意的 —— 放在 prefix 根下、与 npm 文件并列，一眼能看出这目录是谁管的。

### 8.4 装后校验（三件事，缺一不可）

1. **哈希对账** —— `package-lock.json` 里该包的 `integrity` vs registry 声明。不符即失败（可能是该版本在两次请求之间被重发）。
2. **bin 包含检查** —— 解析 `node_modules/.bin/<name>` 符号链接后再做 `realpath` 包含检查，越界即拒。
3. **可执行权限** —— `os.access(X_OK)`。

`command` 存的是**符号链接解析后**的真实路径，不是 `.bin/<name>` 链接本身。这样「可执行文件还在不在安装目录里」这个复核（§8.6）恒可做。

### 8.5 安装确认页的两处新约束

在 §2.8 那两条不可妥协的约束之外，本地安装再加两条：

1. **解析失败不阻断安装** —— registry 不可达时仍可退回 npx 方式，只把失败内联展示。否则一个网络抖动会让用户彻底装不上任何 MCP。
2. **默认值本身就是一条安全主张，所以必须在界面上看得见**。「下载到本地」是默认选项，且理由（钉版本、可复核、可卸载）直接写在选项里，而不是藏进某个设置。

依赖数展示上的一个克制：`dep_count` 来自 `npm install --dry-run`，**取不到就显示「未知」而不是编一个数**。一个可信的包拖进一个恶意传递依赖是供应链攻击的标准手法，报少了会给出虚假的安心感 —— 宁可说不知道。

### 8.6 「本地包」区块的能力

| 操作 | 说明 |
| --- | --- |
| 列表 | `slug` / 版本 / **占用磁盘** / 状态 / **被几个条目引用** |
| 展开 | 完整 `command`、目录、registry、安装时间、依赖数、哈希、是否允许过脚本 |
| 校验 | 按需复核哈希与 bin 位置。**不做成每次连接都跑** —— 那要遍历整棵 `node_modules`，而它要防的是「装完之后被人动过」这种低频事件 |
| 卸载 | 两段确认（首点武装、再点执行）。**被条目引用时在 title 里点名** —— 删了那些条目会起不来 |

三种状态：`ok`（可执行文件在）/ `incomplete`（有哨兵，上次没装完）/ `broken`（元数据在但可执行文件没了）。

### 8.7 可测试性：唯一的测试接缝

所有子进程调用都走可注入的 `runner`（签名 `(argv, cwd, timeout) -> (rc, out, err)`）。测试里换成假实现就**永远不会真的去下包** —— 而假 runner 必须真的造出目录结构（`package-lock.json` + 包内 `package.json` + `.bin/` 符号链接），否则装后校验那三条测不到。

`tests/test_mcp_installer.py` 共 30 例，覆盖：规格白名单（32 个必须被拒的形态 × + 7 个必须放行的形态）、包名先于 npm 调用被校验、`--ignore-scripts` / `--registry` 强制项及 `allow_scripts=True` 时前者消失、哈希不符判定失败、bin 逃逸、失败清理半成品、slug 撞车、列表三态、卸载路径安全、复核能发现篡改。

### 8.8 平台

bin 解析按 POSIX（`node_modules/.bin/<name>` 符号链接）实现。Windows 上是 `.cmd` shim，`StdioServerParameters` 无法直接执行 —— 故本模块在 Windows 上**明确报不支持**，而不是装完了才发现起不来。

### 8.9 registry 实测（**这条决定功能能不能用**）

真机验证时的第一个发现不是代码问题，是网络：

| 端点 | 结果 |
| --- | --- |
| `https://registry.npmjs.org/mcp-server-time` | **连不通**（curl 20s 超时，`http=000`；`npm view` 60s 超时后判定失败） |
| `https://registry.npmmirror.com/mcp-server-time` | `http=200` |
| `https://www.baidu.com` | `http=200`（排除"整个外网都不通"） |

**即：默认的官方源在本机不可达**，功能开箱即死。这正是设计里坚持"registry 用可配置项、且必须在确认页明文展示"的意义 —— 换源是一次**可见的、用户主动的**决定，而不是悄悄读 `.npmrc`。

要用起来，在 `~/.aigent/config/config.json` 里加一行（**没有替你写**，因为改 registry 属安全相关决定，不该由工具静默代劳）：

```json
{ "MCP_NPM_REGISTRY": "https://registry.npmmirror.com/" }
```

代价要说清楚：镜像**能同时篡改元数据与它声明的哈希**，"装后对账"那道防线对镜像攻击无效（它只挡"两次请求之间被重发"）。所以 §8.1 的安全模型在换源后整体降一档 —— 换来的是能装。

> 顺带一条：`mcp-server-time` 已被作者**从 npm 撤下**（元数据里有 `unpublished`），`npm view` 回 404。本模块的处理是给「registry 上没有这个包或版本」而不是崩 —— 这是 404 分支的实测样本。

---

## 九、启停失灵的两个根因（2026-10-08 实测修复）

用户报的现象是两条，看起来无关，实际是**两个独立缺陷叠在一起**：

1. codegraphcontext 启动时是连上的，**关一次再开就再也连不上**（显示「连接失败」）；
2. 之后**开关按钮点击完全没反应**（前端既不变色也不动）。

日志里的全部证据只有两行 —— `09:26:38` 与 `09:26:52` 各一条「MCP 条目已保存」，之后再无任何命令到达后端。所以第 2 条不是"前端没发请求"，而是**第一条之后链路就堵死了**。

### 9.1 根因一：`stop()` 从未真正停下（引擎层一行错，泄漏子进程）

`MCPServerSession.stop()` 里那句置位退出事件的代码是这样的：

```python
asyncio.run_coroutine_threadsafe(self._exit.set(), self._loop).result(timeout=5)
```

`asyncio.Event.set()` 是**普通同步方法，不是协程**（`inspect.iscoroutinefunction(asyncio.Event().set)` → `False`）。于是 `run_coroutine_threadsafe` 当场抛 `TypeError: A coroutine object was required`，而下面紧跟的 `except Exception: pass` **把它静默吞掉了**。

后果是链式的：

- `_exit` 永远没被置位 → `_serve()` 里的 `await self._exit.wait()` 永不返回；
- 后台线程不退 → 它 spawn 出去的 **stdio 子进程也不退**；
- `join(timeout=5)` 一直等到超时 → **`stop()` 稳定耗时 5.0s**（这就是"卡住"的直接指纹；正常 unwind 实测只要 **0.17s**）；
- `stop()` 仍把 `_thread`/`_loop` 置 None 并返回，**调用方以为已经断开干净了**。

对 stdio 类服务器，泄漏的子进程是致命的。codegraphcontext 启动就打开一个 **kuzu 嵌入式数据库并持进程间独占文件锁**：

```
RuntimeError: IO exception: Could not set lock on file:
  /Users/peijiping/.codegraphcontext/global/db/kuzudb
```

旧进程不退出 → 新进程启动即撞锁 → 握手超时（15s）→「连接失败」。而且**每次失败的连接尝试又会再泄漏一个进程**，越点越坏。这解释了"关一次就再也连不上"这个非线性的症状。

**修法**（`mcp_manager.py`）：用 `loop.call_soon_threadsafe(ev.set)`，并在 `join` 后如实汇报线程是否退了（`stop()` 改返回 `bool`）。

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| `disable` 耗时 | 5.0s（死等 join 超时） | **0.2s** |
| `disable → enable` 结果 | 连接失败（kuzu 锁） | **connected** |
| 残留 cgc 子进程 | 每次失败尝试 +1 | **shutdown 后归零** |

`stop()` 里那个 `except Exception: pass` 是这次排查最大的陷阱：**它把一个必然抛的异常变成了静默失败**。凡是需要"跨线程操作 loop 上的对象"，都要先确认那个对象是不是协程。

### 9.2 根因二：热重载串行 → 撞 30s 超时 → 前端"点了没反应"

`_reload_mcp_all_runtimes()` 原本是**逐个 manager 串行**调 `maybe_reload()`。而 `maybe_reload()` 内部要对每条**启用中**的条目做一次真实的 connect 握手，单条上限 `MCP_CONNECT_TIMEOUT`（默认 15s）。

于是耗时是**各 manager 累加**：全局 Agent + 1 个会话 runtime = 2 个 manager × (cgc 连不上 15s + zotero 0.03s) ≈ **30s+**，而主进程 `mcpServerUpsert` 的超时正好是 **30s**。超时 → promise 回 `null` → 回执被丢弃 → `mcpSaving` 复位但列表状态不更新 → **用户看到"点了没反应"**。

**修法**（`ws_bridge.py`）：`ThreadPoolExecutor` 并发化，耗时 = 最慢的那一个，与 manager 个数无关。

| 场景（2 个 manager） | 串行 | 并发 |
| --- | --- | --- |
| `disable` | — | **0.15s** |
| `enable` | 30s+（**超时**） | **15.01s** ✅ |

注意并发提交时**必须一次性 submit 全部 future 再逐个 `result()`**，逐个 `submit`+`result` 会退化成串行，等于没改。各 manager 读同一份配置文件、只读无写，无共享可变状态，并发安全。

### 9.3 连带修的日志缺口（这类问题本来查不出来）

`mcp_manager.py` 过去**全部用 `print` 打状态**，走 stdout，**不落日志文件**。失败路径有 print 但成功路径没有，重载决策过程一个字都没有。所以当时拿着 `agent_2026-10-08.log` 只能看到"MCP 条目已保存"，看不到"为什么没连上"。

现改为 `get_logger("mcp")` 落 `~/.aigent/logs/agent_日期.log`，并把三段关键信息补齐：

- **打算连什么**：`连接中: <name>（<transport>）<command+args | url>` —— 能区分"命令填错了"和"服务没起来"；
- **连了多久、成没成**：`连接成功: <name>（<s>，N 工具 / M 资源）` / `连接失败: <name>（<s>）<原因>`；
- **重载决策**：`检测到配置变更，开始 reconcile：<旧 keys> → <新 keys>`，逐条 `断开:` / `重连:`，无事发生时明确 `配置变更但无需增删改`（**静默 return 是这类问题最难查的地方**）；
- **重载总耗时**：`MCP 热重载完成：N/M 个 runtime，耗时 X.XXs` —— 这一行是 §9.2 的判据，一旦逼近 30s 就能立刻看出要动哪里；
- **泄漏取证**：`disconnect` 在线程没退出时打 `warning`，`stop` 超时同样告警 —— 残留子进程从"要靠猜"变成"日志里就有"。

`connect_all()` 另加一条汇总（Agent 构造时的第一手证据）：`connect_all 完成：N/M 成功，耗时 X.XXs`。

### 9.4 验收

- **448 后端测试全绿**（`Ran 448 tests in 8.3s / OK (skipped=4)`，隔离 `HOME` 跑，避免真实 `mcp_servers.json` 里的 cgc 让每个构造 Agent 的用例都等一次 15s 握手）；
- 前端**零改动**（本次两个根因都在后端），`tsc --noEmit` 通过；
- 真机复现脚本实测：`disable → enable → disable → enable` 四轮全部符合预期，退出后 `pgrep` 无残留。

> 遗留（**未修，下次遇到同一现象时先查这里**）：日志里那条 `Could not set lock on file` 也可能是**另一个真实 cgc 实例**（另一个进程里跑着、或上次异常退出的僵尸进程）持着锁，而不是本模块泄漏。判据是修复合入后若仍失败，跑 `pgrep -fl "cgc mcp start"` 看有几个 —— 多于一个就是外部占用，需手动清理后再开关。
