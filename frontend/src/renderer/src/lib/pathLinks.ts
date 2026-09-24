import type { RefsPayload } from '@protocols/agentProtocol'

/**
 * 助手正文里的**裸路径 → 可点链接**（多格式预览，2026-09-23，docs/frontend/21）。
 *
 * 背景：模型经常在正文里直接输出形如 `data/attachments/full.png` 的相对路径
 * （既不是 @ 胶囊也不是 Markdown 链接）。这类路径此前只是死文本，用户要自己
 * 去文件树里找。本模块在 **remark（mdast）层**把它们识别出来、替换成内部
 * 链接节点，ReactMarkdown 的 `a` 组件拦截后转成右栏打开动作。
 *
 * 三个刻意的设计决策：
 * 1. **白名单匹配，不是正则猜**：只有**逐字命中**工作空间文件列表（右栏树同源，
 *    `refs_list` 回执）的 token 才变链接 —— 绝不会出现"点了没反应"的假链接。
 *    树没加载（`index` 为空）时整个插件零开销直通，正文原样渲染。
 * 2. **mdast 层拆 text 节点**，不碰 `code` / `inlineCode` / `image`
 *    等子树：代码块里的路径保持死文本（那是代码，不是导航）。已有的
 *    `link` 节点单独处理 —— `file://` 形态（大模型爱写）且命中工作空间
 *    列表时就地改写成内部跳转，其余链接不动。
 * 3. 命中结果带 `is_dir`：文件 → 右栏开独立预览 tab；目录 → 定位「文件」视图。
 *
 * ⚠️ 白名单只决定"哪种渲染形态"，**不决定"能不能点"**：没命中的 `file://` 链接
 * 由 `localTargetOf` 兜底，照样点得开（见该函数注释里那次整页重载事故）。
 */

/** 相对/绝对路径 → 条目（isDir 决定点击行为） */
export type PathIndex = Map<string, { abs: string; isDir: boolean }>

/** 从工作空间文件列表构建索引（树回执 → 每条消息渲染前 useMemo 一次）。 */
export function buildPathIndex(tree: RefsPayload | null | undefined): PathIndex {
  const idx: PathIndex = new Map()
  if (!tree || typeof tree.workdir !== 'string' || !tree.workdir) return idx
  const base = tree.workdir.replace(/\/+$/, '')
  const items = Array.isArray(tree.items) ? tree.items : []
  for (const item of items) {
    if (!item || typeof item.path !== 'string' || !item.path) continue
    const entry = { abs: item.path, isDir: item.type === 'dir' }
    // 绝对路径键（模型有时输出完整路径）
    idx.set(item.path, entry)
    // 相对路径键（最常见的输出形态）
    if (item.path.startsWith(base + '/')) {
      idx.set(item.path.slice(base.length + 1), entry)
    }
  }
  return idx
}

/** 路径状 token：字母数字 + `_ @ . - /`。边界修剪交给下方 trim（尾点/尾连字符
 *  多为标点），真正的过滤靠白名单精确匹配 —— 多匹配无害，错匹配才致命。 */
const TOKEN_RE = /[A-Za-z0-9_@.\-/]+/g

/** mdast 内部链接的 URL scheme。非 http 的自定义协议需要配合
 *  `safeUrlTransform`（见下方）才能在 react-markdown 里存活。 */
export const REF_URL_SCHEME = 'aigent-ref:'

/** 编码一条内部链接 URL（abs + isDir）。 */
function refUrl(entry: { abs: string; isDir: boolean }): string {
  return `${REF_URL_SCHEME}${encodeURIComponent(entry.abs)}${entry.isDir ? '?d=1' : ''}`
}

/** 解码内部链接 URL → (abs, isDir)；非内部链接返回 null。 */
export function parseRefUrl(href: string): { abs: string; isDir: boolean } | null {
  if (typeof href !== 'string' || !href.startsWith(REF_URL_SCHEME)) return null
  const rest = href.slice(REF_URL_SCHEME.length)
  const q = rest.indexOf('?')
  const enc = q >= 0 ? rest.slice(0, q) : rest
  try {
    const abs = decodeURIComponent(enc)
    if (!abs) return null
    return { abs, isDir: q >= 0 && rest.slice(q) === '?d=1' }
  } catch {
    return null
  }
}

/** 在一段 text 里找白名单命中并拆分成 [text, link, …]；无命中返回 null。 */
function transformText(value: string, index: PathIndex): Array<Record<string, unknown>> | null {
  let out: Array<Record<string, unknown>> | null = null
  let last = 0
  let m: RegExpExecArray | null
  TOKEN_RE.lastIndex = 0
  while ((m = TOKEN_RE.exec(value))) {
    const raw = m[0]
    // 模型常写 `./data/attachments/full.png`（真实会话实测形态）——先精确剥掉
    // 开头的 "./"（只剥这一个组合，不碰 `../`：否则 ../foo.png 会错配顶层同名文件）
    const stripped = raw.replace(/^(?:\.\/)+/, '')
    const dotSlash = raw.length - stripped.length
    // 再修剪 token 首尾的标点类字符（句尾的 `。` 不在字符类里自然断开；
    // 句中的尾点/尾连字符在这里摘掉）
    const core = stripped.replace(/^[.\-_]+/, '').replace(/[.\-_]+$/, '')
    if (!core) continue
    const hit = index.get(core)
    if (!hit) continue
    out = out ?? []
    const start = m.index + dotSlash + (stripped.length - stripped.replace(/^[.\-_]+/, '').length)
    out.push({ type: 'text', value: value.slice(last, start) })
    out.push({
      type: 'link',
      url: refUrl(hit),
      data: { hProperties: { className: 'md-path-link' } },
      children: [{ type: 'text', value: core }]
    })
    last = start + core.length
  }
  if (!out) return null
  if (last < value.length) out.push({ type: 'text', value: value.slice(last) })
  return out
}

/** 这些节点的子树整个跳过：代码与已有链接/图片里的路径不是导航。 */
const SKIP_TYPES = new Set([
  'code',
  'inlineCode',
  'image',
  'linkReference',
  'imageReference',
  'definition',
  'html'
])

/** `file:///abs/path` → 本地绝对路径（decode 中文/空格）；非 file: 或解析失败返回 ''。 */
function filePathFromUrl(url: string): string {
  try {
    const u = new URL(url)
    if (u.protocol !== 'file:') return ''
    return decodeURIComponent(u.pathname)
  } catch {
    return ''
  }
}

/**
 * 链接 → **本地路径目标**（渲染层只管"送给右栏"，可读性由后端沙箱裁决）。
 *
 * 为什么需要这个兜底（2026-09-23 用户实测事故）：
 *
 * 正文里的 `[文件名](file:///abs/path)` 只有当路径**逐字命中**已加载的文件列表时
 * 才会被 `remapFileLink` 改写成内部链接；文件列表没加载（用户没开过右栏「文件」
 * 视图 —— 最常见的情形）时它保持 `file:` 原样，被 `safeUrlTransform` 清成空串，
 * 于是渲染出 `<a href="">`。而 `href=""` 在浏览器里等于"导航到当前文档 URL" ——
 * **点一下就整页重载，会话态全丢、回到新建任务态**。用户报的就是这个。
 *
 * 所以：本地路径形态的链接**永远不落回 `<a>`**，一律转成按钮送右栏；路径不在
 * 工作空间内时，后端 `resolve_within` 会拒绝并在预览区给出原因 —— 这是"看得懂的
 * 失败"，而整页重载是"灾难"。
 *
 * 判定（宁宽勿漏，漏一个就退回重载）：
 * - `file:///abs` → 绝对路径；**尾斜杠视为目录**（`file:///x/y/`）
 * - 无协议的相对路径（模型还会写 `[说明](./README.md)`）→ 原样交给后端按工作空间根解析
 * - `http(s)` / 协议相对 `//host` / `#锚点` / 空值 → null（不是本地路径，走别的分支）
 */
export function localTargetOf(url: string): { path: string; isDir: boolean } | null {
  const value = String(url ?? '').trim()
  if (!value || value.startsWith('#')) return null
  if (value.startsWith('file:')) {
    const p = filePathFromUrl(value)
    if (!p) return null
    const isDir = p.length > 1 && p.endsWith('/')
    return { path: isDir ? p.replace(/\/+$/, '') : p, isDir }
  }
  // 有协议（含协议相对 `//host`）的一律不是本地路径
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('//')) return null
  return { path: value, isDir: value.endsWith('/') }
}

/**
 * 把 `file://` 形态的 Markdown 链接**就地改写**成内部链接（命中工作空间列表时）。
 *
 * 背景（2026-09-23 用户实测）：大模型经常输出
 * `[病历AI精确生成-架构流程.pdf](file:///Users/.../data/attachments/xxx.pdf)` ——
 * 标准的 Markdown 链接，但 `file:///` 协议在渲染层既过不了 CSP 也过不了
 * `safeUrlTransform`，点击注定无效。既然路径就是工作空间内的绝对路径，直接
 * 在 mdast 层把它映射成 `aigent-ref:` 内部跳转；未命中列表（路径不在工作空间 /
 * 列表被截断）时保持原样 —— 最终被 urlTransform 清洗成死链接，与旧行为一致。
 * 返回是否发生了改写。
 */
function remapFileLink(node: Record<string, unknown>, index: PathIndex): boolean {
  const url = typeof node.url === 'string' ? node.url : ''
  if (!url.startsWith('file:')) return false
  const p = filePathFromUrl(url)
  const hit = p ? index.get(p) : undefined
  if (!hit) return false
  node.url = refUrl(hit)
  const data = (node.data && typeof node.data === 'object' ? node.data : {}) as Record<string, unknown>
  data.hProperties = { ...(data.hProperties as object | undefined), className: 'md-path-link' }
  node.data = data
  return true
}

function walk(node: unknown, index: PathIndex): void {
  if (!node || typeof node !== 'object') return
  const parent = node as { children?: Array<Record<string, unknown>> }
  const children = parent.children
  if (!Array.isArray(children)) return
  const next: Array<Record<string, unknown>> = []
  let changed = false
  for (const child of children) {
    const type = child && typeof child === 'object' ? String((child as { type?: unknown }).type) : ''
    if (type === 'link') {
      // file:// 链接（大模型爱写的形态）先尝试映射成内部跳转；子树不下钻
      // （链接文字保持原样，不做二次拆分）
      if (remapFileLink(child, index)) changed = true
      next.push(child)
      continue
    }
    if (type === 'text' && typeof (child as { value?: unknown }).value === 'string') {
      const parts = transformText((child as { value: string }).value, index)
      if (parts) {
        changed = true
        next.push(...parts)
        continue
      }
      next.push(child)
      continue
    }
    // 非文本：除跳过名单外继续下钻（paragraph / emphasis / tableCell / heading …）
    if (!SKIP_TYPES.has(type)) walk(child, index)
    next.push(child)
  }
  if (changed) parent.children = next
}

/**
 * remark 插件工厂。用法：
 * ```
 * remarkPlugins={[remarkGfm, ...(index.size ? [[remarkPathLinks, index]] : [])]}
 * ```
 * （index 为空时不进插件数组，整个变换零开销。）
 */
export function remarkPathLinks(index: PathIndex) {
  return (tree: unknown): void => {
    walk(tree, index)
  }
}

/**
 * react-markdown 的 URL 清洗（v9 `urlTransform`）。
 *
 * 为什么不用默认的 `defaultUrlTransform`：它只放行 http(s)/mailto 等已知协议，
 * 内部链接 `aigent-ref:` 会被清成空串。而完全放行（恒等返回）又会让模型正文里
 * 的 `javascript:` / `data:` 链接活过来。这里镜像默认行为、只多放行两类：
 *
 * - `aigent-ref:`（内部跳转）
 * - `file:`（**放行只为了让 `a` 组件拿得到原值**去走 `localTargetOf` 兜底）。
 *   放行本身不产生可导航的锚点：`a` 组件把本地路径一律渲染成按钮并自行拦截点击，
 *   永远不会有 `<a href="file://…">` 落到 DOM 上。**必须放行**——清成空串后
 *   组件再也认不出它是本地路径，就会退化成 `<a href="">` = 整页重载。
 */
export function safeUrlTransform(url: string): string {
  const value = String(url ?? '').trim()
  if (/^(aigent-ref|file):/i.test(value)) return value
  if (!/^[a-z][a-z0-9+.-]*:/i.test(value)) return value // 相对路径 / 锚点
  if (/^(https?|mailto|irc|ircs|xmpp):/i.test(value)) return value
  return ''
}
