# 14 · 工具读图（`view_image`）

> 状态：**已实施**（2026-09-21）。本文档记录一条独立通道 —— 模型**按需**把工作空间里的
> 图片读进上下文。与 `12-附件与文件输入.md`（用户显式添加）和 `13-引用文件与文件夹（@-mention）.md`
> （用户显式 @）构成"图片进入模型上下文的三种来源"。
>
> ⚠️ **入口已并入 `run_read`**（2026-09-21 稍晚，
> 见 [15-统一文件读取（run_read 按类型分派）](15-统一文件读取（run_read 按类型分派）.md)）：
> `view_image` **作为工具名已退役**，读图片改走 `run_read`（它按魔数/扩展名分派）。
> **本文描述的通道一条都没变** —— 中性块 `tool_image`、合成 user 消息、内存缩放、
> 能力门控、顺序硬约束全部照旧；因此正文里凡是 `view_image(path=…)` 的调用点，
> 今天都写作 `run_read(path=…)`，块形状里的 `image` 字段则扩成了 `images` 列表
> （多图，兼容存量单数字段）。本文保留原样作为该通道的**设计记录**。

***

## 一、概念

### 1.1 它解决什么问题

2026-09-21 引用（@-mention）上线后暴露了一个硬缺口：**用户 `@` 一张图片，模型完全看不到它。**

原因不是 bug，是结构性的：

| 环节 | 事实 |
| --- | --- |
| 引用通道注入的内容 | 只有**路径清单**（`refs.py` 的 `_render_ref_text`），并明确要求"用 run_read 读" |
| `run_read` 的实现 | `safe_path(path).read_text()`（`tools.py`）→ 读到 PNG 必然 `UnicodeDecodeError` → 返回 `Error: ...` |
| 工具集里有什么 | `bash / run_read / run_read_pdf / run_write / run_edit / run_glob` —— **没有任何图片工具**（当时的工具面） |
| 附件通道 | 能看图，但前提是**用户把图片当附件上传**；工作空间里的图片（截图、生成的图表、`run_glob` 扫到的图）不在其列 |

而且模型看到裸解码错误的**第一反应是换个命令再试**（`strings` / `cat` / `hexdump`），
白白烧掉好几轮。所以缺口不只是"看不到图"，还包括"用错误的方式反复试"。

### 1.2 关键认知：模型看不见磁盘

这条通道的全部设计都建立在一条事实上：

> **模型只能看见请求体。给它路径本身毫无意义 —— 必须有谁把像素读进请求体。**

由此推出两条**必须**分清的边界：

1. **不是"工具调用模型"**。`view_image` 内部**不调用任何模型**，也不生成任何文字描述。
   它只做三件事：路径校验 → 魔数确认真是图片 → 返回一个**中性图片块**（只有路径与
   mime，**没有字节**）。真正的"看图"发生在 agent 循环的**下一跳**请求里。
2. **不是"转述"**。让另一个（便宜）视觉模型把图描述成文字再回填，等于让主模型拿着
   有损的二手信息作答：子模型不知道用户到底想问什么（"这个按钮颜色对不对" vs
   "总结这张图"），图表数值 / UI 对齐 / 报错截图上的行号在转述时必然丢，而且同一张图
   付两次钱。**正确做法是让主模型直接看到像素。**

反过来也要说清工具返回值里**为什么还是有一段文字**（`"已读取 shot.png（image/png，128KB），内容随附。"`）：
那是**元数据说明**，不是对图片内容的转述 —— 区别在于它不携带任何图片语义，没有信息损失。
它存在的原因是协议要求：`tool` 消息必须回答那条 `tool_call_id`，而它**只能放文本**。

### 1.3 与另两条通道的分工（硬约定）

| | 附件（12） | 引用（13） | **工具读图（本文）** |
| --- | --- | --- | --- |
| 触发者 | 用户显式添加 | 用户显式 `@` | **模型自己决定** |
| 存储 | 复制副本到 `.attachments/` | 零复制零存储 | **零复制**（直接读原文件） |
| 位置 | 工作空间**之外** | 工作空间之内 | 工作空间**之内** |
| 图片何时进上下文 | 发送**前**（预内联，0 跳） | 从不（只给路径） | 发送**中**（按需，+1 跳） |
| 块类型 | `attachment` | `ref` | `tool_image` |
| 谁付 token | 无条件付（模型没看也付） | 不付 | **只为真看过的付** |

三者**协议字段、块类型、模块、CSS 前缀全部独立**，禁止互串。工具通道唯一的复用是
附件通道的**编码与门控代码**（`_data_url` / `prepare_image` 的缩放判据 / 能力三态），
以及同一条哲学：**账本无字节，线格式只在发送边界现算**。

***

## 二、设计

### 2.1 数据流

```
用户: @shot.png，看看这个界面错在哪
  ↓ refs 注入路径清单（不含内容）
模型: run_read(path="/ws/shot.png")            ← 模型自己发起的工具调用
  ↓ tools.run_read → 图片分支：safe_path + 魔数校验
工具返回: {type:"tool_image", text:"已读取…", image:{path,mime,name}}   ← 无字节
  ↓ agent_loop：tool 消息只收下 text；图片块被摘出来收集
history（jsonl）:
  [tool] 已读取 shot.png（image/png，128KB），内容随附。   ← 回答 tool_call_id
  [user] [text: 以下是 run_read 读取的图片…] + [tool_image × M]   ← 合成消息
  ↓ _model_messages（发送边界）展开
请求体（本例）:
  {role:"tool",  content:"已读取 shot.png…"}
  {role:"user",  content:[{type:"text",…}, {type:"image_url", image_url:{url:"data:image/png;base64,…"}}]}
  ↓
同一个模型在下一跳用视觉能力解读像素
```

### 2.2 三种形状

**① 工具返回值**（`attachments.build_tool_image_result`，不入库）

```jsonc
{
  "type": "tool_image",
  "text": "已读取 shot.png（image/png，128.0KB），内容随附。",
  "image": { "path": "/ws/shot.png", "name": "shot.png", "mime": "image/png" }
}
```

**② 账本形态**（落 jsonl，**无字节**；`attachments.build_tool_images_message` 产出）

```jsonc
{
  "role": "user",
  "content": [
    { "type": "text", "text": "[以下是 run_read 读取的图片，内容随附：shot.png]" },
    { "type": "tool_image", "image": { "path": "…", "name": "shot.png", "mime": "image/png" } }
  ],
  "_tool_images": true
}
```

`_tool_images` 是**合成消息的 marker**：落 jsonl 供回放辨认，但它**不在
`agent_full_v2.MODEL_MSG_FIELDS` 白名单里** → 不会漏进 API 请求体
（`tests/test_view_image.py` 有断言钉住两边没跑偏）。

**③ 请求体线格式**（只在 `Agent._model_messages` 现算，不回写 history）

```jsonc
{ "type": "image_url", "image_url": { "url": "data:image/png;base64,…" } }
```

### 2.3 为什么必须用"合成 user 消息"

因为协议限制：**Chat Completions 的 `tool` 消息 `content` 只接受 text part**，
图片塞不进去。跨供应商事实（2026-03 实测矩阵）：

| 供应商 / 接口 | 工具结果能否带图 |
| --- | --- |
| Anthropic Messages（`tool_result.content`） | ✅ 支持 `image` / `document` 块 → 图直接躺在工具结果里，**无额外往返** |
| OpenAI **Responses**（`function_call_output.output`） | ✅ 支持 `input_image` / `input_file` 数组 |
| OpenAI **Chat Completions** | ❌ schema 把 tool 消息内容限制为 text parts |
| Gemini（`functionResponse.inlineData`） | ⚠️ 仅 Gemini 3 系列 |
| Mistral Chat Completions | ❌ spec 定义 tool 内容为 string |

本项目走的是 `chat_completions`（`llm_config.py` 的 `API_FORMATS` 虽列出 `responses`，
但注释写明**"当前仅 chat_completions 参与运行"**），所以只有"独立 message"这一条路：

```
[assistant(tool_calls: view_image, bash, run_read)]
[tool] …view_image 的说明…
[tool] …bash…
[tool] …run_read…
[user] 合成消息：文本说明 + [image_url × M]      ← 必须在全部 tool 消息之后
```

#### ⚠️ 顺序是硬约束（违反即回退）

**绝不能把图片消息插在两条 tool 消息之间** —— 那会打断
`assistant.tool_calls` ↔ tool 消息链（协议风险）。所以：**这一批 tool 消息全部落盘之后，
再追加一条聚合的合成 user 消息**。一次响应里调了 3 次 `view_image` → 只有 1 条合成消息、
里面有 3 个图片块。

`tests/test_view_image.py::ToolImageOrderingContractTests` 用源码断言钉住这条
（循环体内不得出现 `image_msg`；追加点在循环之外且落盘）。之所以用源码断言：这个位置
由 `agent_loop` 的结构决定，而 `agent_loop` 需要真实 LLM 才能驱动 —— 与其造个假循环去
测假循环，不如把"追加点必须在循环之外"钉在源码上（仓库既有做法同此，见
`test_system_injection_contract`）。

### 2.4 图片编码（不落盘）

| 环节 | 做法 | 理由 |
| --- | --- | --- |
| 缩放 | **内存里**缩到长边 `ATTACHMENT_IMAGE_MAX_EDGE`（默认 1568）再编码 | 发送边界是热路径，**不允许往用户工作空间写派生文件**。这点与附件刻意不同（附件在 stage 阶段有天然的 dst 目录，所以那次是落盘缩放） |
| 何时缩 | 长边超限**或**体积 > 1.5MB | 与 `prepare_image` 同判据：为省几十 KB 付一次 JPEG 有损重编码不划算 |
| 失败处置 | **绝不回落原图** → 说清原因的占位文本；只有"尺寸本来就 ≤ 上限"才发原图 | 落回原图正是发不出去的那一个（2026-09-21 事故，见 §2.4.1） |
| 解码上限 | `ATTACHMENT_IMAGE_DECODE_MAX_PIXELS`（默认 5 亿像素；0 = 不限制） | 超限时**在解码之前**就判出来（先读头部拿尺寸），避免 GB 级内存尖峰 |
| 上限 | `ATTACHMENT_INLINE_MAX_BYTES`（默认 15MB，base64 后约 20MB） | 超限 → **说清原因的占位文本**（"过大未能发送，可先用 bash 缩放"），而不是让 provider 回 413/400 |
| 缓存 | 复用附件的展开缓存，键含 `("tool_image", 路径, mtime_ns, size, mime, edge, 解码上限)` | 一轮内 `_model_messages` 会被调用多次（每次 LLM 往返一次），不缓存就反复读盘 + base64。**失败结论同样缓存**：解码级成本（GB / 秒）不能每轮重算 |

**先缩后判上限**：一张 20MB 的截图缩完只有几百 KB，必须在缩放**之后**用编码结果比上限，
否则会误判成"太大发不出去"。

#### 2.4.1 大图与 Pillow 解压炸弹阈值（2026-09-21 线上事故，违反即回退）

**事故形状**：用户 `@` 引用了一张 22500×15016（3.4 亿像素、4.6MB）的 PNG 截图。
`Image.open()` 命中 **Pillow 自己的解压炸弹阈值**（默认 ≥89MP 报警、≥179MP 直接抛
`DecompressionBombError`）→ 缩放失败 → 旧代码把"缩放搞砸了"和"尺寸本来就小、不需要缩"
混成同一个 `None` → **回落发原图** → provider 回
`messages[11].image[0]: You have uploaded an unsupported image`，一条**与真实原因无关**的
400 把整轮对话打死。

**两个根因，缺一不可**：

1. **阈值必须抬升**。Pillow 那条线是防**不可信输入**的 DOS 防线，而这里读的都是用户自己
   工作空间里的文件；高分辨率渲染图 20000px 级别完全正常。改由
   `image_decode_max_pixels()` 接手：**先读头部拿尺寸**（`Image.open` 不解码像素），
   超限就明说"多大、为什么没发、怎么办"，绝不硬冲。
   实现上只用锁包住 `Image.open()` 那一刻（检查发生在这一刻，`load()` 不再查），
   既避免并发线程读到阈值的中间态，也不把 GB 级解码压在锁里。
2. **失败必须与"不需要缩"分开**。状态码 `NO_NEED`（尺寸本来就在范围内 → 发原图）与
   `FAILED` / `TOO_BIG`（→ 占位文本）是**两个不同结论**，混用即回退到本事故。

**实测**（本机）：3.4 亿像素 PNG → 解码 + 缩放 + 编码 **约 1 秒 / 1.35GB 内存峰值**，
产出 103KB 的 1568px JPEG；同一轮内第二次调用命中缓存（0.1ms）。

**残留边界**：附件路径（`prepare_image`）失败仍发原图，与工具图路径有意不同 —— 附件在登记
时已按类型与体积校验过，且没有向模型解释原因的通路；且 `_expand_image` 的发送文件是
`send_image or original`，语义上就是"备胎"。能落到那里的只剩超过本机解码上限的极端图。

### 2.5 单轮图片数上限

`VIEW_IMAGE_MAX_PER_TURN`（默认 8）—— 理由：**图片一旦进了历史，此后每次请求都要重新
上传一遍 base64**（附件同理）。不设限时模型连读二十张图，请求体与每轮上传都会线性膨胀。

超出的图**不静默丢弃**，换成一句 `（另有 N 张未随附：单轮图片上限 8 张，可分批继续读：…）`
的文本 —— 模型才知道自己没看全，而不是以为"就这些"。

### 2.6 能力门控（三态，与附件同口径）

`supports_image=False` → 展开为占位文本（`[图片: x.png]（未发送：当前模型不支持图片输入…）`），
且**在读盘/编码之前就判**（省一次 base64，并保证"模型看不到的图"会明确说出来而不是静默消失）。

判定口径与前端 `modelSupportsImage`、桥层 `_model_supports_image`、`llm_config.caps_allow_image`
四处统一：明确声明 input 不含 image → False；元数据缺失 / 形状不认识 → **True（放过）**，
宁可让 provider 回一个真实错误，也不要本地静默吞掉图片。

**门控的短路判据是 `attachments.history_has_images`**（附件图片块 ∪ 工具图片块）。
这一点容易踩：工具通道的会话**没有附件块**，只判 `history_has_attachments` 会拿到默认值
`True` → 图片被原样发给不支持图片的模型。

***

## 三、落地

### 3.1 改动面

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 块与展开 | `agents/attachments.py` | `TOOL_IMAGE_BLOCK_TYPE` / `TOOL_IMAGES_MARKER` / `view_image_max_per_turn()` / `image_decode_max_pixels()` / `build_tool_image_result` / `is_tool_image_result` / `tool_image_text` / `tool_image_block` / `build_tool_images_message` / `is_tool_images_message` / `history_has_images` / `sniff_image_mime` / `is_image_path` / `_expand_tool_image` / `_data_url_tool_image` / **`_resize_jpeg_bytes`（附件与工具图共用的缩放核心）**；`prepare_image` 改为复用该核心；`expand_content_for_model` 新增分支；`text_view` 新增标签 |
| 工具 | `agents/tools.py` | `base_tools` 增加 `view_image` schema；`run_view_image`；handler 注册；**`run_read` 命中图片时指路**（扩展名零 IO 判定 + 解码失败兜底）；`execute()` 注解放宽 |
| 引擎 | `agents/agent_full_v2.py` | `_execute_tool_call` 条件化 `str()`（图片块原样保留）；工具回放后追加**一条**聚合合成消息并落盘；能力门控判据换成 `history_has_images` |
| 引用 | `agents/refs.py` | 注入块补"图片用 view_image" |
| 压缩 | `agents/context_compact.py` | `content_to_str` 增加 `tool_image` → `[图片: 名字]`（**不回落到 path**） |
| 回放 | `agents/ws_bridge.py` | `_history_to_ui` 按 marker **跳过**合成消息 |
| 参数 | `.env.example` | `VIEW_IMAGE_MAX_PER_TURN`、`ATTACHMENT_IMAGE_DECODE_MAX_PIXELS` 注释示例 |
| 测试 | `tests/test_view_image.py`（新增 27 条）、`tests/test_context_compact_attachment_str.py`（+3） | — |

### 3.2 勿回退要点

1. **`tool_image` 块里永远不能有字节**。`_execute_tool_call` 的 `is_tool_image_result`
   判定不能改成按工具名硬编码（否则将来第二个产图工具又得改一遍）；也**不能**退回
   `str(tool_output)` —— 那会让模型只看到一段 Python dict repr。
2. **合成消息只能有一条，且在该批 tool 消息之后**（见 §2.3）。
3. **`run_read` 的指路不能删**。删掉它，模型会退回到"换个命令重试"的死循环。
4. **marker 不能加进 `MODEL_MSG_FIELDS`**，也不能让 `_history_to_ui` 不跳过它
   （否则前端每次回放都多一个内容为"[以下是 run_read 读取的图片…]"的假用户气泡）。
5. **`sniff_image_mime` 必须按魔数判**：只看扩展名会让"改名成 .png 的文本文件"以
   二进制的形式进请求体。
6. **发送边界不许写盘**。想让图变小只能在内存里缩；往工作空间写派生文件是副作用。
7. **缩放失败绝不回落原图**，且 `NO_NEED` / `FAILED` 必须是两个状态码（§2.4.1）。
   顺带：抬升 Pillow 解压炸弹阈值只允许"临时 + 用完复原"，别把全局改成 `None` 不管。

### 3.3 可调参数

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `VIEW_IMAGE_MAX_PER_TURN` | 8 | 单轮随附图片数上限 |
| `ATTACHMENT_IMAGE_MAX_EDGE` | 1568 | 长边缩放上限（与附件共用；0 = 不缩放） |
| `ATTACHMENT_IMAGE_DECODE_MAX_PIXELS` | 5 亿 | 发送用缩略图允许解码的像素上限（0 = 不限制；超限 → 占位文本 + 建议先 `sips` 缩放） |
| `ATTACHMENT_INLINE_MAX_BYTES` | 15MB | 单图内联字节上限（与附件共用） |

### 3.4 验证

- `tests/test_view_image.py` —— 30 条（工具层 / 无字节 / 聚合与上限 / 展开与门控 /
  **大图缩放与解码上限** / 请求体投影 / 顺序契约）；`tests/test_context_compact_attachment_str.py` 新增 3 条。
- 全量回归：`.venv/bin/python -m unittest discover -s tests` → **552 tests, OK**
  （2026-09-21，含本事故的 4 条新守卫）。
- 真实事故复现验证：把 `session_bM9siq5BMA.jsonl` 的历史喂给 `Agent._model_messages()`
  → 两张 3.4 亿 / 0.85 亿像素的 PNG 均展开为 1568px JPEG（各 103KB），
  整个请求体 0.27MB（修复前那张原图单独就有 6.5MB）；jsonl 内**仍然零 base64**。
- 手工链路：工作空间放一张截图 → 对话里 `@shot.png` 让它"看看这张图" → 日志出现
  `view_image` 工具行 → 请求体含 `image_url` → 模型描述出画面内容（而不是"读不到"）。

***

## 四、取舍与备选

### 4.1 为什么不做"发送前预内联"（方案 A）

引用展开时按扩展名直接把图片内联进请求体，可以省掉那一跳。没做的原因：

1. **覆盖面太窄**：它只能覆盖"用户 @ 的图片"。而模型真正高频需要看图的场景恰恰是它
   自己发现的那类 —— 跑完命令截的图、自己生成的图表、`run_glob` 扫到的目录里的图片。
2. **token 无条件付出**：模型没看也得付。
3. **目录引用无解**：`@` 一个文件夹，里头上百张图怎么办？
4. **会稀释引用的定位**：引用通道的核心承诺是"零复制、只给路径"；改成"图片给像素、
   其余给路径"会让语义变得不可预期。

保留为后续增量：如果实测发现"用户 @ 图片"是绝对高频场景，可以在 `refs` 侧加一个
**窄口径**分支（少量显式图片直内联），与工具通道共用同一份编码/门控代码。

### 4.2 为什么不用视觉子模型转述

见 §1.2 第 2 条。只有一种情况值得考虑：**主模型本身没有 vision 能力**时的兜底
（或需要 OCR / 结构化抽取）。当前不做 —— 能力门控已经诚实地降级为"未发送"提示，
比给一段有损转述更好。

### 4.3 为什么不切到 Responses API

Responses API 的 `function_call_output.output` **原生支持** `input_image`，
是理论上最干净的方案（图片名正言顺地躺在工具结果里，连合成消息都不需要）。
没做是因为那是**引擎级迁移**（`llm_manage` / 整个请求组装 / 流式解析都要动），
风险与收益不成比例，另立项评估。

### 4.4 图片进历史后的膨胀

图片块留在历史里 → 此后每轮请求都要重传 base64。当前的对策是单轮上限 + 上限内不回收。
未来若成为问题，可考虑"超过 N 轮的图片块降级为文本占位"（与 L1/L4 压缩协同），
但那需要在压缩层引入时间维度，本轮不做。
