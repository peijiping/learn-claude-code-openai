import type { Editor } from '@tiptap/react'
import type { JSONContent } from '@tiptap/core'
import type { RefInput } from '@protocols/agentProtocol'

/**
 * 富文本输入区 → `{ text, refs }` 的**唯一序列化出处**。
 *
 * 契约（改动前务必读，见 docs/frontend/13）：
 *
 * - `text` 就是 `chat.payload.text` —— **恒为字符串**（标题生成、首轮判定、日志切片
 *   全依赖这条）。胶囊在文本里序列化成 `@相对路径`，**只是给模型看的定位提示**；
 *   权威信息在 `refs[]` 里。**不要从 text 反解路径**（相对路径可能同名）。
 * - `refs[]` 按**文档顺序**收集并按 `path` 去重：同一个文件引用两次只发一次
 *   （后端的引用清单是按路径渲染的，重复毫无信息量）。
 * - 本模块**不 import 任何会执行 Tiptap/React 的模块**（只有类型导入会被擦除），
 *   这样它保持可单测的纯函数性质。
 */

/** 胶囊的 ProseMirror 节点名。
 *
 *  直接沿用 Tiptap `@tiptap/extension-mention` 的节点名 `mention`（不重命名）：
 *  那是官方支持路径，且该扩展的多触发字符设计（`mentionSuggestionChar` 属性 +
 *  `suggestions` 选项）正好是将来挂 `/` 命令、`#` 会话引用要用的东西 ——
 *  同一节点类型 + 不同触发字符，比每种 token 各造一个节点更省事。 */
export const REF_NODE_NAME = 'mention'

export interface EditorSnapshot {
  text: string
  refs: RefInput[]
}

/** 从编辑器文档 JSON 里按顺序收集引用（去重）。 */
export function collectRefs(doc: JSONContent | null | undefined): RefInput[] {
  const out: RefInput[] = []
  const seen = new Set<string>()
  const walk = (node: JSONContent | null | undefined): void => {
    if (!node || typeof node !== 'object') return
    if (node.type === REF_NODE_NAME) {
      const attrs = (node.attrs ?? {}) as Record<string, unknown>
      const path = String(attrs.path ?? attrs.id ?? '')
      if (path && !seen.has(path)) {
        seen.add(path)
        out.push({
          path,
          name: String(attrs.label ?? ''),
          is_dir: Boolean(attrs.isDir)
        })
      }
    }
    const children = node.content
    if (Array.isArray(children)) for (const child of children) walk(child)
  }
  walk(doc)
  return out
}

/** 编辑器 → 发送用的快照。编辑器还没建好时返回空快照（不抛）。 */
export function serializeEditor(editor: Editor | null | undefined): EditorSnapshot {
  if (!editor) return { text: '', refs: [] }
  let text = ''
  try {
    // getText() 会走各节点的 renderText —— 胶囊被渲染成 `@相对路径`
    text = editor.getText()
  } catch {
    text = ''
  }
  let refs: RefInput[] = []
  try {
    refs = collectRefs(editor.getJSON() as JSONContent)
  } catch {
    refs = []
  }
  return { text, refs }
}
