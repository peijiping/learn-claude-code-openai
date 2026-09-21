import type { MessageRef } from '@protocols/agentProtocol'

/**
 * 正文里的 `@相对路径` token → 「文本 / 引用胶囊」片段序列（**纯函数**）。
 *
 * 背景：胶囊在输入区序列化成 `@相对路径` 存进 `chat.payload.text`（见
 * `editor/refExtension.ts` 的 `renderText`），那是**给模型看的定位提示**。
 * 但消息气泡若把这串 token 原样显示，用户看到的就是一条刺眼的路径 ——
 * 同一个引用在输入区是胶囊、发出去却变成裸路径，前后不一致。
 * 渲染时把 token 还原成胶囊，气泡里就只出现文件名（与输入区同款视觉）。
 *
 * 三个刻意的约定：
 *
 * - **只认 `refs[]` 里存在的路径**：匹配不上的 `@xxx` 原样留作正文。
 *   `@` 在这套输入里是普通字符（邮箱、`@media` 之类都可能出现），
 *   绝不能"看到 @ 就当引用"把正文吃掉。
 * - **不从 text 反解权威路径**：命中的是 `refs[]` 里的那条记录（path 来自后端
 *   以磁盘为准的规范化结果），token 只用来**定位**它在正文中的位置。
 * - **无用例时零成本**：没有 `refs` 直接返回单段文本，不做任何扫描。
 */

export interface RefTextSegment {
  kind: 'text'
  text: string
}

export interface RefChipSegment {
  kind: 'ref'
  ref: MessageRef
  /** 渲染 key：同一条引用可以在正文里出现多次，用下标保证唯一 */
  key: string
}

export type RefSegment = RefTextSegment | RefChipSegment

export interface RefTextRender {
  /** 按正文顺序切好的片段 */
  segments: RefSegment[]
  /** 已在正文里内联渲染的引用 path（去重、按出现顺序）。RefBar 据此只显示"没内联的" */
  inlinePaths: string[]
}

/** 路径 token 的尾部标点：`@a.ts，帮我看看` 里的 `，帮我看看` 不该被吞进路径 */
const TRAILING_PUNCT = /[.,;:!?，。；：！？、）)】」》"'…]+$/

/** 前缀匹配时，token 里紧跟文件名之后的第一个字符若属于这些，说明还在路径内部
 *  （`@a.tsx` 不该被 `a.ts` 这条引用吃掉） */
const PATH_CHAR = /[A-Za-z0-9_.\-/]/

/** token → 引用：path 相等，或 path 以 `/token` 结尾
 *  （token 是相对工作空间的路径，而 ref.path 是绝对路径 —— 用后缀对齐） */
function matchRefExact(token: string, refs: MessageRef[]): MessageRef | null {
  if (!token) return null
  for (const r of refs) {
    const path = r?.path ?? ''
    if (!path) continue
    if (path === token || path.endsWith('/' + token)) return r
  }
  return null
}

/** 兜底：`@full.png这个图片里…`（用户删掉了胶囊后的空格）—— 用**最长**的文件名
 *  前缀匹配，且要求剩余部分从"非路径字符"开始。返回命中的引用与消耗的字符数。 */
function matchRefPrefix(token: string, refs: MessageRef[]): { ref: MessageRef; len: number } | null {
  let best: { ref: MessageRef; len: number } | null = null
  for (const r of refs) {
    const name = r?.name ?? ''
    if (!r?.path || !name || token.length <= name.length) continue
    if (!token.startsWith(name)) continue
    if (PATH_CHAR.test(token[name.length])) continue
    if (!best || name.length > best.len) best = { ref: r, len: name.length }
  }
  return best
}

/**
 * 正文 → 片段序列。
 *
 * @param content 消息正文（`chat.payload.text` 的原样存留）
 * @param refs    该消息的引用记录（`msg.refs`；可能缺省）
 */
export function renderRefText(content: string, refs?: MessageRef[] | null): RefTextRender {
  const text = content ?? ''
  const list = (Array.isArray(refs) ? refs : []).filter((r) => !!r?.path)
  if (!text) return { segments: [], inlinePaths: [] }
  if (list.length === 0) return { segments: [{ kind: 'text', text }], inlinePaths: [] }

  const segments: RefSegment[] = []
  const inlinePaths: string[] = []
  let buf = ''
  const flush = (): void => {
    if (buf) {
      segments.push({ kind: 'text', text: buf })
      buf = ''
    }
  }

  let i = 0
  while (i < text.length) {
    if (text[i] !== '@') {
      buf += text[i]
      i += 1
      continue
    }
    // token = `@` 之后到空白/行尾（与 serializer 的 `@相对路径` 同一形态）
    let j = i + 1
    while (j < text.length && !/\s/.test(text[j])) j += 1
    const raw = text.slice(i + 1, j)
    const punct = raw.match(TRAILING_PUNCT)?.[0] ?? ''
    const trimmed = punct ? raw.slice(0, -punct.length) : raw

    // 命中优先级：整个 token → 剥掉尾部标点 → 文件名前缀。
    // `tail` 是"胶囊之后仍属于正文"的残余字符（标点 / 用户紧贴着打的字），
    // 必须原样留在文本里，否则 `@a.ts，帮我看看` 会变成 `[a.ts] 帮我看看`。
    let hit = matchRefExact(raw, list)
    let tail = ''
    if (!hit && trimmed !== raw) {
      hit = matchRefExact(trimmed, list)
      if (hit) tail = punct
    }
    if (!hit) {
      const pre = matchRefPrefix(raw, list)
      if (pre) {
        hit = pre.ref
        tail = raw.slice(pre.len)
      }
    }

    if (!hit) {
      // 不认识这个 @：当普通字符继续走（绝不吞正文）
      buf += '@'
      i += 1
      continue
    }
    flush()
    segments.push({ kind: 'ref', ref: hit, key: `${hit.path}#${segments.length}` })
    if (!inlinePaths.includes(hit.path)) inlinePaths.push(hit.path)
    buf += tail
    i = j
  }
  flush()
  return { segments, inlinePaths }
}
