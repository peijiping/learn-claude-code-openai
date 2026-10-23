# 26 - 联网工具（web_search 与 web_fetch）

> 本文档对应实现：`agents/web_tools.py`（叶子模块）+ `agents/tools.py`（工具注册）。
> 日期：2026-10-17。属于「底座能力补齐」增量，不涉及前端协议/UI 改动（前端工具卡片渲染是通用的）。

***

## 一、概念：模型需要「联网」这条腿

Agent 的三条信息获取腿：

| 通道 | 拿到的信息 | 来源 | 本文档 |
| --- | --- | --- | --- |
| `run_read` | 工作空间里的**本地文件**（文本/图片/PDF/Office） | 用户机器 | 15 篇 |
| 附件 / 引用 | 用户**主动**交给或指给模型的本地内容 | 用户机器 | 12 / 13 篇 |
| **`web_search` / `web_fetch`** | **外部世界**的实时信息 | 公网 | **本文** |

前两条腿都困在「本地」。当模型需要**最新信息**（库/框架/API 当前用法、版本发布、
报错条目、外部事实）时，本地文件没有答案 —— 这时要么模型诚实说「我的知识可能过期」，
要么就得有联网工具。本次把 DeepSeek 的 Search / Fetch 两个 HTTP 接口封装成
`web_search` / `web_fetch` 两个工具，补齐第三条腿。

**它是 Harness 能力，不是模型能力**：模型本身不会上网，联网这条腿由 Harness 提供工具实现。

***

## 二、后端接口：DeepSeek Search / Fetch

来自官方指南 <https://api-docs.deepseek.com/zh-cn/guides/search/>。两者都是**原始 JSON API**，
与 DeepSeek 模型 API **共用同一种 Bearer 鉴权**。

| API | Endpoint | 用途 |
| --- | --- | --- |
| Search | `POST {base}/v1/tools/search` | 按 query 搜索网页，返回结构化结果 |
| Fetch | `POST {base}/v1/tools/fetch` | 抓取单个网页，以 **Markdown** 返回正文 |

`base` 默认 `https://api.deepseek.com`。

### 2.1 Search 请求字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | - | 搜索词，1~128 字符，不能全空白，支持搜索语法（见下） |
| `count` | integer | 否 | `10` | 返回条数 1~50 |
| `ori_question` | string | 否 | - | 触发搜索的用户原始问题，帮助理解意图 |
| `need_content` | boolean | 否 | `false` | `true` 时在 `content` 返回网页正文；否则恒为空串 |
| `localization` | string | 否 | `""` | 本地化 IP（本期未暴露） |

搜索语法（原样传给搜索引擎，可组合）：`site:` / `-site:` / `-` / `-"..."` / `"..."` /
`lang:zh,en` / `after:2025` / `before:2026-06-01`。

### 2.2 Search 响应字段（`results[]`）

`rerank_score`（0~3，≥1.5 为优质）/ `title` / `url` / `snippet`（100~200 字，适合前端）/
`summary`（600~800 字，**适合给模型读**）/ `content`（正文，`need_content=false` 时为空）/
`site_name` / `site_icon` / `date`（RFC 3339 UTC）/ `authority`（0~4，≥3 极权威，-1 未知）。

### 2.3 Fetch 请求/响应

请求：`{"url": "..."}`（≤8192 字符，须为含 host 的 HTTP/HTTPS URL，不能带用户名/密码）。
响应：`{"title": "...", "content": "# ...\n\nMarkdown 正文"}`。受目标站点可用性与访问策略影响。

### 2.4 错误与限速

错误为 OpenAI 兼容格式（`error.message`）。状态码：400 参数非法 / 401 Key 无效 /
402 余额不足 / 403 Fetch 被安全策略拒 / 404 抓不到可提取正文 / 408 读体超时 /
413 体过大 / 429 超 QPS / 500 / 504。**Http 408/429/5xx 可重试，其余 4xx 不改请求别重试。**

计费**按请求次数**（每 1000 次成功请求 5 元，Search 与 Fetch 同价），与 token / 结果条数无关；
只有 HTTP 200 且产生结果才计费。限速：Search / Fetch **各 5 QPS**（账号粒度，与用哪把 Key 无关）。

***

## 三、设计决策（为什么这么做）

### 3.1 工具名按能力取名：`web_search` / `web_fetch`（而非 `deepseek_*`）

工具名会写进系统提示、文档、以及未来的 `allowed_tools` 白名单，日后不易改。按**能力**
取名可屏蔽后端细节：将来换搜索引擎/加备用源时，工具名与文档不用动，只换 `web_tools.py`
内部实现与配置键。**后端是谁，在配置键里点名**（见 3.2）。

### 3.2 配置与模型供应商解耦：独立一把 DeepSeek Key

本 Agent 支持多家模型（OpenAI/Anthropic 兼容端点，连接中心里可切），**主模型未必是 DeepSeek**，
因此模型侧的 `OPENAI_API_KEY` **不能**假定为 DeepSeek 的 Key。而这两个联网工具是 DeepSeek
平台侧的能力，所以**单独配置一把 Key**（用户把 DeepSeek 的 Key 填进去即可）：

| 键（扁平，键名同 `.env`） | 落点 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_SEARCH_API_KEY` | `~/.aigent/config/credentials.json`（0600，密钥） | 无 | Search + Fetch 共用 |
| `DEEPSEEK_SEARCH_BASE_URL` | `~/.aigent/config/config.json` | `https://api.deepseek.com` | 代理/镜像时覆盖 |
| `DEEPSEEK_SEARCH_TIMEOUT` | `~/.aigent/config/config.json` | `30` | 单次请求超时（秒） |

加载链沿用 `config.py`：真实环境变量 > 项目级 config.json > 用户级 config.json > `.env` > 默认值。
`DEEPSEEK_SEARCH_API_KEY` 以 `_API_KEY` 结尾 → 按既有约定归 `credentials.json`。

### 3.3 放进 `base_tools`：主智能体与子智能体都可用

`base_tools` 是**子智能体**的工具集，也是 `tools` / `main_agent_tools` / `default_agent_tools`
的公共前缀 —— 把两个工具放这里，主智能体与子智能体**自动都拿到**（子智能体读代码/查资料
时常需联网，让它自己查能省一次主循环往返）。二者是只读网络请求，无副作用，适合下放。

### 3.4 参数取舍

- 暴露 `query` / `count` / `need_content` / `ori_question`（`ori_question` 帮助理解意图）。
- **未暴露** `localization`（服务端按 IP 自动本地化，手工传 IP 的场景本项目用不到）。
- `need_content` 默认 `false`：`summary`（600~800 字）是官方「适合模型阅读」的字段，
  默认只回摘要即可覆盖绝大多数查询；正文很长，仅在需要细读某条时开。
- 两个工具都提供 `parallel` 字段（与全仓其他工具一致）：多条互不依赖的查询同轮并发提速。

### 3.5 输出渲染与上下文预算

工具结果会进模型上下文，必须封顶（**截断明确告知**，不静默丢）：

- 搜索：每条 `summary` 截 600 字，整体封顶 `12000` 字（`count=50` 全量可达 4 万字）；
- 抓取：正文封顶 `30000` 字（与 `attachments` 的 `ATTACHMENT_TEXT_MAX_CHARS` 同口径）。
- 搜索默认格式：`序号. 标题` / `URL` / `来源 | 日期 | 权威 | 相关度` / `摘要`；
  `need_content=true` 时另附 `正文`。

### 3.6 错误契约：永远返回字符串、绝不抛异常

与 `tools.py` 工具层铁律一致 —— 网络/超时/鉴权/限流/空结果都收敛成 `Error: ...`
（或明确的空结果/无法抓取文案）回给模型，**绝不打死整轮**。HTTP 错误文案把状态码翻译成
**可操作的中文说明**（如 402「账号余额不足」、429「超过 QPS 限速」，并附官方 `message`）。
**未配置 Key** 时返回明确指引（去 `credentials.json` 配置 `DEEPSEEK_SEARCH_API_KEY`），
让模型能向用户如实报告，而不是抛异常。

### 3.7 权限：按「非文件工具」直接放行

`permission.py` 的判定链把「任务/技能/记忆/后台/cron/workflow/ask_user/sub_agent…」等
**非文件语义**的工具直接放行；联网工具同属此类，**默认模式即放行、无需审批**（只读网络请求，
不改本地状态）。它们也不进 `FILE_TOOLS`，不参与路径判定。若未来要管控联网，宜另立规则，
不塞进现有文件/命令两类判定里。

### 3.8 未配置时不隐藏工具 schema

两个工具的 schema 是**静态**的（与其他工具一样每轮下发），不做「未配置就不暴露」的动态裁剪 ——
后者要牵动 `tools` / `base_tools` / `main_agent_tools` / `default_agent_tools` 多处缓存失效逻辑，
收益（省一点 schema token）远小于代价（复杂度 + 缓存 bug 风险）。未配置时由 handler 返回 Error 文案。

***

## 四、落地实现

### 4.1 `agents/web_tools.py`（叶子模块）

只依赖标准库 + `httpx` + `logger`，**不 import config/paths**（与 `mcp_market.py` 同构，
运行时直接读 env，配置由启动期 `config.load()` 灌入）。

| 函数 | 作用 |
| --- | --- |
| `api_key()` / `base_url()` / `timeout()` / `configured()` | 配置读取（内联 `os.environ.get(...)`） |
| `_post(path, payload)` | 统一 POST：**唯一**失败收敛点，返回 `(data, error_text)` |
| `search(query, count, need_content, ori_question)` | 转调 Search 接口并渲染结果文本 |
| `fetch(url)` | 转调 Fetch 接口并返回「标题 + Markdown」 |

`search` 内联钳 `count` 到 1~50；`query` 校验非空、≤128 字；`fetch` 校验 http/https。

### 4.2 `agents/tools.py`（注册点）

- `import web_tools`（挨着 `import doc_convert` 的叶子模块区）。
- `_build_handlers()` 新增两个 handler（转调 `web_tools`）：`parallel` 不进 handler（lambda 只取所需键）。
- `base_tools` 新增两个工具定义（OpenAI function schema）。

### 4.3 关闭 / 回滚

- 临时关闭：不配 Key 即可（调用返回指引性 Error）；或在 `base_tools` 里注释掉两个定义。
- 彻底移除：删 `web_tools.py`、`tools.py` 里的 import/两 handler/两定义、`.env.example` 本节。

***

## 五、边界与已知限制

- **Fetch 是单页抓取**：不解 JS 渲染，也抓不动需要登录/交互的页面；受站点策略影响，
  403/404 是常态而非异常（文案已如实说明）。
- **Search 结果质量**：以 `authority`（权威性）与 `rerank_score`（相关性）两个字段提示模型
  甄别来源；渲染里都带出来。
- **计费按次**：`count` 不影响单次价格；无结果不计费。
- **QPS 5**：主循环并发搜索超过 5 次/秒会收到 429（文案提示稍后重试）。

***

## 六、验证记录（2026-10-17）

- `python -m py_compile agents/web_tools.py agents/tools.py` 通过。
- `web_tools` 单元级验证（venv python）：未配置 Key 的 Error 文案、空 query / 超长 query /
  非 http(s) URL / 空 url 的参数校验、结果渲染与截断、错误透传 —— 全部符合预期。
- `ToolRegistry` 注册验证：`web_search` / `web_fetch` 出现在 `base_tools` / `default_agent_tools` /
  `main_agent_tools` / `handlers` / `resolve_handler`；`execute("web_fetch", url="ftp://nope")`
  返回 Error 文案。
