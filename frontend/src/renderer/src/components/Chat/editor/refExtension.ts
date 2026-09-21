import Mention from '@tiptap/extension-mention'
import { ReactNodeViewRenderer } from '@tiptap/react'
import { filterRefs, type RefCandidate } from '@lib/refFilter'
import RefCapsule from '../RefCapsule'
import { REF_NODE_NAME } from './serializeDoc'

/**
 * 输入区的 `@` 引用扩展：在 Tiptap 的 Mention 基础上换成自己的胶囊 NodeView。
 *
 * 为什么直接用 `@tiptap/extension-mention` 而不是从 `Node.create` 手搓：
 * 它已经把「原子 inline 节点 + Suggestion 接线 + renderText 序列化」都做好了
 * （节点配置正是我们想要的：`group:'inline' / inline / atom / selectable:false`），
 * 且支持多触发字符（`suggestion` 单触发 / `suggestions` 多触发）—— 将来挂
 * `/` 命令、`#` 会话引用时，加一个 suggestion 条目即可，不用另造节点类型。
 *
 * 三个刻意的配置：
 * - `allowedPrefixes: null`：默认只允许"行首或空格后"触发，中文用户写
 *   「看看@文件」不会触发 —— 必须关掉这个前缀校验。
 * - `allowSpaces: false`：空格终止检索（否则 `@src a` 会被当成一个查询串）。
 * - `onKeyDown` 首行放行输入法：**中文用户的生命线**。拼音候选框里的 ↑↓/Enter
 *   若被当成列表导航/选中，输入法直接不可用。判据与既有 Enter 守卫同源
 *   （`isComposing` / `keyCode === 229`）。
 */

export interface RefPanelState {
  /** 跟随 `@` 之后输入的检索串 */
  query: string
  /** 选中一项时把它插成胶囊（Suggestion 给的回调） */
  command: (item: RefCandidate) => void
}

export interface RefExtensionOptions {
  /** 候选全集（`useWorkspaceRefs` 缓存的完整扁平列表）；过滤在前端本地做 */
  candidates: () => RefCandidate[]
  /** 面板状态变化：非 null = 打开/更新，null = 关闭。
   *  **刻意不带 items**：候选列表是异步到位的，若在这里快照一份 items，
   *  列表到货后面板不会刷新（只能等用户再敲一个字）。条目一律由渲染方从
   *  `candidates()` 现算，见 InputBox。 */
  onChange: (state: RefPanelState | null) => void
  /** 面板打开期间的按键拦截（返回 true = 已消费） */
  onKeyDown: (event: KeyboardEvent) => boolean
}

/** Suggestion 的 render 回调 → 我们的面板状态。
 *
 *  形参用 `any`：Suggestion 的泛型在 Mention 里被固定成 `MentionNodeAttrs`，
 *  而我们的条目是 `RefCandidate`，硬套泛型只会得到一堆不可满足的逆变约束，
 *  收益为零。真实契约由 `RefCandidate` 的类型与本文件的调用点保证。 */
function toPanelState(props: any): RefPanelState {
  return {
    query: String(props?.query ?? ''),
    command: (item: RefCandidate) => props?.command?.(item)
  }
}

export function createRefExtension(opts: RefExtensionOptions) {
  return Mention.extend({
    addAttributes() {
      // 在 Mention 自带的 id / label / mentionSuggestionChar 之上补三个业务属性
      return {
        ...(this.parent?.() ?? {}),
        /** 绝对路径（权威；tooltip 与发送给后端的都是它） */
        path: { default: '' },
        /** 相对工作空间的路径（序列化成 `@token` 用它；也是过滤时的路径匹配字段） */
        rel: { default: '' },
        /** 是否目录（决定图标与后端注入块里的「文件 / 目录」字样） */
        isDir: { default: false }
      }
    },
    addNodeView() {
      return ReactNodeViewRenderer(RefCapsule)
    },
    /** 序列化契约：胶囊在纯文本里就是 `@相对路径`（给模型看的定位提示） */
    renderText({ node }) {
      const rel = String(node.attrs.rel ?? '')
      const label = String(node.attrs.label ?? '')
      return '@' + (rel || label)
    }
  }).configure({
    HTMLAttributes: { class: 'ref-capsule-html' },
    suggestion: {
      char: '@',
      allowSpaces: false,
      allowedPrefixes: null,
      items: ({ query }) => filterRefs(opts.candidates(), query),
      render: () => ({
        onStart: (props) => opts.onChange(toPanelState(props)),
        onUpdate: (props) => opts.onChange(toPanelState(props)),
        onExit: () => opts.onChange(null),
        onKeyDown: ({ event }) => {
          // 输入法组合期间的按键一律还给输入法（先于一切自定义导航）
          if (event.isComposing || event.keyCode === 229) return false
          return opts.onKeyDown(event)
        }
      }),
      command: ({ editor, range, props: item }) => {
        const node = item as unknown as RefCandidate
        editor
          .chain()
          .focus()
          .insertContentAt(range, [
            {
              type: REF_NODE_NAME,
              attrs: {
                // id 与 path 同值：id 是 Mention 的"标识"字段，留着它便于将来
                // 用 Mention 自带的解析/序列化能力；path 是我们自己的权威字段。
                id: node.path,
                label: node.name,
                path: node.path,
                rel: node.rel,
                isDir: node.isDir,
                mentionSuggestionChar: '@'
              }
            },
            // 补一个空格，光标自然落在胶囊之后，用户可以接着打字
            { type: 'text', text: ' ' }
          ])
          .run()
      }
    }
  })
}
