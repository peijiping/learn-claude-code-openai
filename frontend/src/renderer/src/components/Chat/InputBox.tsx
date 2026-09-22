import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { EditorContent, useEditor, type Editor } from '@tiptap/react'
import Document from '@tiptap/extension-document'
import Paragraph from '@tiptap/extension-paragraph'
import Text from '@tiptap/extension-text'
import HardBreak from '@tiptap/extension-hard-break'
import History from '@tiptap/extension-history'
import Placeholder from '@tiptap/extension-placeholder'
import { Icon } from '@components/common/Icon'
import { hasImageInput } from '@components/Settings/llmShared'
import type { PermissionMode } from '@protocols/agentProtocol'
import {
  attachmentUrl,
  hasSendableContent,
  showToast,
  useAgentStore,
  resolveModelMeta,
  providerDot,
  projectDisplayName,
  type DraftAttachment
} from '@store/agentStore'
import { useWorkspaceRefs } from '@hooks/useWorkspaceRefs'
import PlusMenu, { PLUS_MENU_LABELS, type PlusMenuKey } from './PlusMenu'
import AttachmentBar, { type AttachmentView } from './AttachmentBar'
import DropOverlay from './DropOverlay'
import RefPicker from './RefPicker'
import { createRefExtension, type RefPanelState } from './editor/refExtension'
import { serializeEditor, type EditorSnapshot } from './editor/serializeDoc'
import { filterRefs, type RefCandidate } from '@lib/refFilter'

/** 默认工作空间 id：它的沙箱根是临时草稿目录（`~/.aigent/projects/default/scratch`），
 *  里面没有用户的项目文件可引用 → `@` 入口在这里置灰。 */
const DEFAULT_PROJECT = 'default'

interface InputBoxProps {
  /** 正文 + 引用的快照。**只用于发送判据，绝不回灌进编辑器** —— 把父 state
   *  同步回编辑器（setContent）会冲掉光标、打断拼音输入、清空撤销栈。 */
  value: EditorSnapshot
  /** 编辑器内容变化时回传快照（父组件据此更新发送判据） */
  onChange: (v: EditorSnapshot) => void
  onSend: () => void
  /** 发送后自增 → 清空编辑器并聚焦（清空走命令，不走受控同步） */
  clearSignal: number
  /** 附件草稿（三条入口都写入 store，这里只读展示） */
  attachments: DraftAttachment[]
  /** 把一批本地绝对路径登记为附件（原生对话框 / 拖拽 / 粘贴共用） */
  onStagePaths: (paths: string[]) => void
  /** 移除一个草稿附件 */
  onRemoveAttachment: (key: string) => void
  /** 在系统文件管理器中定位附件原文件 */
  onOpenAttachment: (sourcePath: string) => void
  /** 输入区被临时"让位"（本会话有在途的 ask_user 提问）时为 true。
   *
   *  隐藏由父级的 CSS 承担（`.chat--asking`），这里只做一件事：**把焦点收回来**。
   *  否则用户盲打的内容会进到一个看不见的编辑器里 —— 敲 Enter 时虽然被
   *  store 的 isSending / 父级的发送守卫拦下，但"字打进去了却看不见"本身就是事故。
   *  编辑器**不卸载**：它是非受控的（内容只在内部），卸载即丢草稿。 */
  suspended?: boolean
}

/** 草稿附件 → 统一视图（图片走自定义协议显示缩略图） */
function draftToView(a: DraftAttachment): AttachmentView {
  return {
    key: a.key,
    status: a.status,
    kind: a.kind,
    name: a.name,
    size: a.size,
    // 就绪后才有 att_id（staging 期无法寻址，显示 clock 占位）
    url:
      a.kind === 'image' && a.attId
        ? attachmentUrl(a.projectId, a.attId)
        : null,
    stats: {
      text_chars: a.textChars,
      text_truncated: a.textTruncated,
      pages: a.pages,
      images: a.images,
      tables: a.tables,
      converter: a.converter,
      warnings: a.warnings
    },
    error: a.error,
    sourcePath: a.sourcePath,
    storedPath: a.storedPath
  }
}

// 思考强度档位展示映射（与后端 PROVIDERS thinking_strengths 对齐）
const THINKING_LABELS: Record<string, string> = {
  low: '轻',
  high: '高',
  very_high: '极高'
}

/** 中央核心输入区：富文本编辑器（正文 + 引用胶囊）+ 附件条 + 工具栏 + 上下文条 */
export default function InputBox({
  value,
  onChange,
  onSend,
  clearSignal,
  attachments,
  onStagePaths,
  onRemoveAttachment,
  onOpenAttachment,
  suspended = false
}: InputBoxProps): JSX.Element {
  const isSending = useAgentStore((s) => s.isSending)
  const stop = useAgentStore((s) => s.stop)
  const llmConfig = useAgentStore((s) => s.llmConfig)
  const llmSaving = useAgentStore((s) => s.llmSaving)
  const setSessionModel = useAgentStore((s) => s.setSessionModel)
  const setSessionOverrides = useAgentStore((s) => s.setSessionOverrides)
  const sessionModelId = useAgentStore((s) => s.sessionModelId)
  const currentContextStats = useAgentStore((s) => s.currentContextStats)
  const sessionUsageBySession = useAgentStore((s) => s.sessionUsageBySession)
  const overridesByModel = useAgentStore((s) => s.overridesByModel)
  const activeSession = useAgentStore((s) => s.activeSession)
  const projects = useAgentStore((s) => s.projects)
  const activeProject = useAgentStore((s) => s.activeProject)
  const pendingProjectId = useAgentStore((s) => s.pendingProjectId)
  const newSession = useAgentStore((s) => s.newSession)
  const openProject = useAgentStore((s) => s.openProject)
  const addProjectFromPicker = useAgentStore((s) => s.addProjectFromPicker)
  const switchPermission = useAgentStore((s) => s.switchPermission)
  const switchProjectPermission = useAgentStore((s) => s.switchProjectPermission)
  /** 权限档位（2026-09-22 权限管控，docs/frontend/17）：盾牌 chip 的选中态。
   *  fallback 链：permissionModeBySession（permission_changed 广播 /
   *  session_history 恢复）→ sessions 列表该会话的 permission_mode →
   *  所属工作空间 permission_mode → 'default'。无会话（新建任务）时展示
   *  目标空间的档位作预告（新会话默认档位取所属空间的最后更改值）。 */
  const permMode: PermissionMode = useAgentStore((s) => {
    const sid = s.activeSession
    if (sid) {
      return (
        s.permissionModeBySession[sid] ??
        s.sessions.find((x) => x.id === sid)?.permission_mode ??
        'default'
      )
    }
    const pid = s.pendingProjectId ?? s.activeProject
    return s.projects.find((p) => p.id === pid)?.permission_mode ?? 'default'
  })
  /** 本会话是否有在途审批（PreToolUse 判定 ask）：有 → 发送按钮禁用
   *  （title=「等待权限审批」）。与 ask 的整块让位（chat--asking）刻意不同：
   *  审批挂起时**输入框可打字、停止可用** —— 停止按钮就在输入区。 */
  const approvalPending = useAgentStore((s) =>
    s.activeSession ? Object.keys(s.approvalBySession[s.activeSession] ?? {}).length > 0 : false
  )
  const [modelOpen, setModelOpen] = useState(false)
  // 加号「添加内容」菜单：条目按 key 分发，见 handlePlusPick
  const [plusOpen, setPlusOpen] = useState(false)
  // 工作空间下拉（chip 点开）：上部分 = 已打开过的空间，末尾固定项 = 选择文件夹
  const [wsOpen, setWsOpen] = useState(false)
  // 权限档位下拉（盾牌 chip 点开）：两档（默认 / 完全访问），仿 ws-picker
  const [permOpen, setPermOpen] = useState(false)
  const [hoveredPanel, setHoveredPanel] = useState<{ id: string; x: number; y: number } | null>(null)
  const [ctxTooltip, setCtxTooltip] = useState(false)
  // 面板以 Portal 渲染在 body 顶层，离开菜单项会先触发 onMouseLeave，
  // 用短延迟给鼠标留出"跨过间隙进入面板"的时间，避免面板闪断。
  const hideTimer = useRef<number | null>(null)
  const clearHide = (): void => {
    if (hideTimer.current !== null) {
      window.clearTimeout(hideTimer.current)
      hideTimer.current = null
    }
  }
  const scheduleHide = (id: string): void => {
    clearHide()
    hideTimer.current = window.setTimeout(() => {
      hideTimer.current = null
      setHoveredPanel((v) => (v?.id === id ? null : v))
    }, 180)
  }
  // 悬浮坐标：优先在菜单项右侧弹出；右/下空间不足时智能翻转，保证面板完整显示在窗口内
  const resolvePanelPos = (r: DOMRect): { x: number; y: number } => {
    const W = 240
    const H = 110
    const margin = 8
    let x = r.right + margin
    if (x + W > window.innerWidth) x = Math.max(margin, r.left - margin - W)
    let y = r.top
    if (y + H > window.innerHeight) y = Math.max(margin, window.innerHeight - H - margin)
    return { x, y }
  }

  // 推导当前高亮的模型：会话绑定模型优先，其次全局 active_model_id（与后端 primary 推导一致）
  const enabled = (llmConfig?.models ?? []).filter((m) => m.enabled)
  const active =
    enabled.find((m) => m.id === sessionModelId) ??
    enabled.find((m) => m.id === llmConfig?.active_model_id) ??
    enabled[0] ??
    null

  // 当前工作空间：有会话时 = 该会话所属空间（切会话时 store 已同步 activeProject）；
  // 空态时 = 「+」/chip 选定的目标空间，缺省跟随后端的活动空间。
  const currentProjectId =
    activeSession === null ? (pendingProjectId ?? activeProject) : activeProject
  const currentProjectPath = projects.find((p) => p.id === currentProjectId)?.path ?? null
  const hasSession = activeSession !== null

  // 会话建成即锁空间（2026-09-20）：工作空间选择只属于「新会话」。已有会话一律
  // 不可改归属 —— 下拉隐藏，chip 退化为只读展示；后端同样拒绝改归属。
  useEffect(() => {
    if (hasSession) setWsOpen(false)
  }, [hasSession])
  // 切换会话时收起权限菜单（选中项随会话变化，避免半开状态指错对象）
  useEffect(() => {
    setPermOpen(false)
  }, [activeSession])

  // ── 引用候选（@-mention）────────────────────────────────────────
  // 一次拉全量 + 前端本地过滤；沙箱根由 (空间, 会话) 共同决定，换空间自动重拉。
  const refs = useWorkspaceRefs(currentProjectId ?? null, activeSession)

  // 编辑器**只创建一次**，它捕获的回调全都会固化成首帧闭包 —— 因此凡是会随
  // 渲染变化的值/函数，一律经 ref 读取，绝不直接闭包捕获。
  const refsRef = useRef(refs)
  refsRef.current = refs
  const liveRef = useRef({
    onChange,
    onSend,
    onStagePaths,
    canSend: false,
    isSending: false
  })
  liveRef.current.onChange = onChange
  liveRef.current.onSend = onSend
  liveRef.current.onStagePaths = onStagePaths
  liveRef.current.isSending = isSending

  const editorRef = useRef<Editor | null>(null)
  const pickerRef = useRef<RefPanelState | null>(null)
  const [picker, setPicker] = useState<RefPanelState | null>(null)
  const selectedRef = useRef(0)
  const [selected, setSelected] = useState(0)
  // 当前空间是否不可引用（键盘处理在编辑器创建时就固化了闭包 → 必须走 ref）
  const refDisabledRef = useRef(false)
  const setSel = (i: number): void => {
    selectedRef.current = i
    setSelected(i)
  }

  // 面板条目**每次渲染现算**，不存进 picker 状态：候选列表是异步到位的，
  // 存一份快照会导致"列表到货了面板却不刷新"——用户只能再敲一个字才看见结果。
  const pickerItems = picker ? filterRefs(refs.items, picker.query) : []
  const pickerItemsRef = useRef<RefCandidate[]>([])
  pickerItemsRef.current = pickerItems

  /** 面板状态变化：非 null = 打开/更新，null = 关闭 */
  const handlePickerChange = (next: RefPanelState | null): void => {
    if (!next) {
      pickerRef.current = null
      setPicker(null)
      return
    }
    // 首次打开才开始拉取（之后复用缓存）
    refsRef.current.ensure()
    const prev = pickerRef.current
    pickerRef.current = next
    setPicker(next)
    // 只有 query 变了才回到第一项：需求要的"默认选中第一项"针对的是一次新的检索，
    // 而不是用户刚用方向键选好又被重置
    if (!prev || prev.query !== next.query) setSel(0)
  }

  /** 面板打开期间的按键（由 Suggestion 转交；返回 true = 已消费） */
  const handlePickerKey = (event: KeyboardEvent): boolean => {
    // 空间不可引用时面板只显示一行原因，键盘也**不能盲选** ——
    // 否则会出现"面板说不可用、回车却插进来一个胶囊"的自相矛盾。
    if (refDisabledRef.current) return false
    const items = pickerItemsRef.current
    if (!pickerRef.current) return false
    if (event.key === 'ArrowDown') {
      if (items.length === 0) return false
      // clamp 不环绕：越界不动，符合列表直觉
      setSel(Math.min(selectedRef.current + 1, items.length - 1))
      event.preventDefault()
      return true
    }
    if (event.key === 'ArrowUp') {
      if (items.length === 0) return false
      setSel(Math.max(selectedRef.current - 1, 0))
      event.preventDefault()
      return true
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
      // 空结果时不消费：让 Enter 继续走"发送"（否则用户被困在面板里出不来）
      if (items.length === 0) return false
      const item = items[selectedRef.current]
      if (!item) return false
      event.preventDefault()
      pickerRef.current.command(item)
      return true
    }
    // 其余（含 Escape）交给 Suggestion 自己处理：Escape 会关面板并**保留已输入的
    // `@query` 文本**，符合"Esc 只是取消这次选择"的预期
    return false
  }

  /** 编辑器级按键。
   *
   *  ⚠️ 顺序事实：`editorProps.handleKeyDown` 比插件的 handleKeyDown **先**执行
   *  （prosemirror-view 的 someProp 先查直接 props、再查插件）。所以面板开着且
   *  有条目时，这里必须 `return false` 把 Enter 让给 Suggestion 去"选中"——否则
   *  会出现"面板开着却把消息发出去了"。 */
  const handleEditorKeyDown = (event: KeyboardEvent): boolean => {
    // 输入法组合期间一律不拦（拼音候选框里的 Enter 不是"发送"）。
    // 与改造前的 textarea 守卫同源，另加 view.composing 这层保险见下。
    if (event.isComposing || (event as KeyboardEvent & { keyCode?: number }).keyCode === 229) {
      return false
    }
    if (event.key !== 'Enter' || event.shiftKey) return false
    if (editorRef.current?.view.composing) return false
    const panel = pickerRef.current
    if (panel && pickerItemsRef.current.length > 0) return false // 交给 Suggestion 选中
    if (panel) handlePickerChange(null) // 空结果：先关面板，再按"发送"处理
    event.preventDefault()
    if (!liveRef.current.isSending && liveRef.current.canSend) liveRef.current.onSend()
    return true
  }

  /** 文件列表 → 本地路径列表（拖拽 / 粘贴共用的收敛函数）。
   *  能拿到路径的直接用；拿不到的（截图等剪贴板图片没有磁盘路径）把**字节**交给
   *  主进程落成临时文件，再按路径走同一条后端流程。 */
  const pathsFromFiles = async (files: File[]): Promise<string[]> => {
    const paths: string[] = []
    const pathlessImages: File[] = []
    for (const f of files) {
      const p = window.agent.getPathForFile(f)
      if (p) paths.push(p)
      else if (f.type.startsWith('image/')) pathlessImages.push(f)
    }
    for (const f of pathlessImages) {
      try {
        const bytes = await f.arrayBuffer()
        const tmp = await window.agent.saveClipboardImage({ bytes, mime: f.type })
        if (tmp) paths.push(tmp)
      } catch {
        /* 单张失败不影响其它文件 */
      }
    }
    return paths
  }

  const editor = useEditor(
    {
      // 刻意不用 starter-kit：聊天输入框不需要标题/列表/加粗，装上只会把 schema
      // 与样式面撑大。这里只要「一个段落 + 文本 + 软换行 + 撤销栈 + 占位符 + 引用」。
      extensions: [
        Document,
        Paragraph,
        Text,
        HardBreak,
        History,
        Placeholder.configure({ placeholder: '有什么我可以帮你的吗？' }),
        createRefExtension({
          candidates: () => refsRef.current.items,
          onChange: handlePickerChange,
          onKeyDown: handlePickerKey
        })
      ],
      editorProps: {
        attributes: { class: 'composer-input composer-editor' },
        handleKeyDown: (_view, event) => handleEditorKeyDown(event),
        // 粘贴：**只在确实带了文件时才拦截**，否则会把纯文本粘贴吃掉
        handlePaste: (_view, event) => {
          const files = Array.from(event.clipboardData?.files ?? [])
          if (files.length === 0) return false
          event.preventDefault()
          void (async () => {
            const paths = await pathsFromFiles(files)
            if (paths.length) liveRef.current.onStagePaths(paths)
            else showToast('未能读取剪贴板内容', 'error', 3000)
          })()
          return true
        },
        // 拖放：只**阻止 ProseMirror 把文件名当文字插进来**，登记仍由 .composer 的
        // onDrop 统一负责（事件继续冒泡）—— 两处都登记会把同一批文件提交两次
        handleDrop: (_view, event) => {
          const files = (event as DragEvent).dataTransfer?.files
          if (!files || files.length === 0) return false
          event.preventDefault()
          return true
        }
      },
      onUpdate: ({ editor: ed }) => liveRef.current.onChange(serializeEditor(ed))
    },
    []
  )
  editorRef.current = editor

  // 让位期间把手上的焦点收回（见 props.suspended）：编辑器仍挂在 DOM 里，
  // 只是被父级 CSS 藏起来 —— 留着焦点的结果是"盲打"。
  useEffect(() => {
    if (suspended && editor?.isFocused) editor.commands.blur()
  }, [suspended, editor])

  // 发送完成 → 清空正文与胶囊并聚焦。**不用 setContent 做同步**（见 props 注释）
  useEffect(() => {
    if (clearSignal <= 0) return
    const ed = editorRef.current
    if (!ed) return
    ed.commands.clearContent(true)
    ed.commands.focus()
  }, [clearSignal])

  // 拖拽计数：dragenter/dragleave 会在进入子元素时成对冒泡，只用布尔量会让遮罩
  // 疯狂闪断 —— 用深度计数，归零才收起。
  const dragDepth = useRef(0)
  const [dragging, setDragging] = useState(false)

  const onDrop = async (e: React.DragEvent): Promise<void> => {
    e.preventDefault()
    e.stopPropagation()
    dragDepth.current = 0
    setDragging(false)
    const files = Array.from(e.dataTransfer?.files ?? [])
    if (files.length === 0) return
    const paths = await pathsFromFiles(files)
    if (paths.length) onStagePaths(paths)
    else showToast('未能读取拖入的内容', 'error', 3000)
  }

  // ── 附件能力预检（前后端双重守卫里的第一道）────────────────────────
  // 图片走 vision，需要模型声明 image 输入能力；文本/文档类不受限（走文本内联）。
  // 后端在 chat 分支还会再判一次（前端隐藏/禁用只是交互层，后端才是最终守卫）。
  const hasImageDraft = attachments.some((a) => a.kind === 'image')
  const imageUnsupported = hasImageDraft && !hasImageInput(active?.capabilities)
  // 发送可用：正文 / 就绪附件 / 引用**任一非空**即可（判据只有一处：
  // store 里的 `hasSendableContent`，InputBox 的按钮与 ChatPanel 构造 payload 共用）；
  // 且不能有"正在读取"的附件（避免半成品发出去）；
  // 且本会话没有在途审批（等待权限审批期间只禁发送，输入/停止不受影响）
  const stagingCount = attachments.filter((a) => a.status === 'staging').length
  const canSend =
    hasSendableContent(value.text, attachments, value.refs) &&
    stagingCount === 0 &&
    !imageUnsupported &&
    !approvalPending
  liveRef.current.canSend = canSend

  // 引用在当前空间不可用（默认草稿空间，或后端已明确 disabled）
  const refPathDisabled = currentProjectId === DEFAULT_PROJECT || refs.disabled
  refDisabledRef.current = refPathDisabled

  // 加号「添加内容」菜单条目点击：先收起菜单，再分发。
  // 逐项接线的唯一落点 —— 按 key 分支即可（见 docs/frontend/02 §3.3）。
  const handlePlusPick = async (key: PlusMenuKey): Promise<void> => {
    setPlusOpen(false)
    if (key === 'attachFile') {
      const paths = await window.agent.pickFiles().catch(() => [] as string[])
      if (paths.length) onStagePaths(paths)
      return
    }
    if (key === 'refPath') {
      // 「引用文件或文件夹」**只做一件事**：往输入框插一个 `@`，之后与手工输入的
      // `@` 完全同一条路径（候选面板 / 过滤 / 胶囊都是同一套）。
      // ⚠️ 绝不能改成 pickFiles：那会走附件的复制链路，把"引用"变成"上传"。
      if (refPathDisabled) return
      editorRef.current?.chain().focus().insertContent('@').run()
      return
    }
    showToast(`「${PLUS_MENU_LABELS[key]}」功能待开发`, 'info', 2000)
  }

  // 上下文圆圈：仅当有选中会话时才显示；数据来自后端 context_stats 事件
  const stats = currentContextStats
  const usedPct = stats ? stats.used_percent : 0
  const indicatorColor = usedPct >= 90 ? '#e5484d' : usedPct >= 70 ? '#f5a623' : '#2ea043'
  // 本会话 token 消耗累计（usage_stats 事件 / 切会话 usage_totals 恢复）
  const sesUsage = activeSession !== null ? sessionUsageBySession[activeSession] ?? null : null
  const sesHasData = !!sesUsage && sesUsage.total_tokens > 0
  const ctxMultiple =
    sesUsage && stats && stats.max_tokens > 0 ? (sesUsage.total_tokens / stats.max_tokens).toFixed(2) : null
  const sesCachePct =
    sesUsage && sesUsage.cached_tokens && sesUsage.prompt_tokens
      ? `${Math.round((sesUsage.cached_tokens / sesUsage.prompt_tokens) * 100)}%`
      : '—'

  return (
    <div
      className={`composer ${dragging ? 'dragover' : ''}`}
      onDragEnter={(e) => {
        // 必须 preventDefault：否则 Electron 会把 drop 当"导航到 file://"，
        // 整个窗口被替换成一个文件内容页面（白屏事故）。
        e.preventDefault()
        dragDepth.current += 1
        setDragging(true)
      }}
      onDragOver={(e) => {
        e.preventDefault()
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
      }}
      onDragLeave={(e) => {
        e.preventDefault()
        dragDepth.current = Math.max(0, dragDepth.current - 1)
        if (dragDepth.current === 0) setDragging(false)
      }}
      onDrop={(e) => void onDrop(e)}
    >
      {/* 附件条：草稿项（可删 + 点开原文件位置） */}
      <AttachmentBar
        items={attachments.map(draftToView)}
        onRemove={onRemoveAttachment}
        onOpen={(v) => v.sourcePath && onOpenAttachment(v.sourcePath)}
      />

      {/* 引用候选面板：在输入框**上方**向上弹出（定位由 CSS 承担） */}
      <RefPicker
        state={picker}
        items={pickerItems}
        selected={selected}
        loading={refs.loading}
        error={refs.error}
        disabled={refPathDisabled}
        reason={refs.reason}
        truncated={refs.truncated}
        onHover={setSel}
        onPick={(item: RefCandidate) => pickerRef.current?.command(item)}
      />

      <EditorContent editor={editor} />

      {/* 附件相关的内联提示（比 toast 更贴近操作点） */}
      {imageUnsupported && (
        <div className="composer-hint error">
          当前模型「{active?.display_name ?? active?.id ?? '未配置'}」不支持图片输入，
          请切换到带「图片」能力的模型，或移除图片附件
        </div>
      )}

      <div className="composer-toolbar">
        <div className="toolbar-left">
          {/* 加号 = 添加内容菜单：向上弹出的两段式面板（添加内容 / 执行方式） */}
          <span className="plus-select">
            <button
              className={`tool-btn ${plusOpen ? 'open' : ''}`}
              title="添加内容"
              aria-haspopup="menu"
              aria-expanded={plusOpen}
              onClick={() => setPlusOpen((v) => !v)}
            >
              <Icon name="plus" size={16} />
            </button>
            {plusOpen && (
              <PlusMenu
                onPick={handlePlusPick}
                onClose={() => setPlusOpen(false)}
                disabledKeys={refPathDisabled ? ['refPath'] : []}
                disabledReason={
                  currentProjectId === DEFAULT_PROJECT
                    ? '默认工作空间是临时草稿目录，没有可引用的文件；请先切换到自定义工作空间'
                    : refs.reason
                }
              />
            )}
          </span>
          {/* 权限档位盾牌 chip（2026-09-22 权限管控，docs/frontend/17）：两档
              （默认 = 敏感操作逐次审批 / 完全访问 = 跳过审批）。切换是
              fire-and-forget —— 会话态 chip 选中态只认 permission_changed 广播；
              无会话（新建任务）时也可切换，写的是**目标工作空间**的最后更改值
              （projects.json，该空间新会话的默认档位），chip 由 projects 广播驱动。 */}
          <span className="perm-select">
            <button
              className={`tool-btn access perm-chip${permMode === 'full_access' ? ' full' : ''}`}
              title={
                hasSession
                  ? permMode === 'full_access'
                    ? '完全访问：跳过审批（硬拒绝仍生效）。点击切换'
                    : '默认：敏感操作逐次审批。点击切换'
                  : permMode === 'full_access'
                    ? '完全访问：跳过审批（硬拒绝仍生效）。点击切换本工作空间新会话的默认档位'
                    : '默认：敏感操作逐次审批。点击切换本工作空间新会话的默认档位'
              }
              aria-haspopup="menu"
              aria-expanded={permOpen}
              onClick={() => setPermOpen((v) => !v)}
            >
              <Icon name="shieldCheck" size={13} />
              {permMode === 'full_access' ? '完全访问' : '默认'}
              <Icon name="chevronDown" size={11} />
            </button>
            {permOpen && (
              <>
                <div className="perm-menu-mask" onClick={() => setPermOpen(false)} />
                <div className="perm-menu">
                  <div
                    role="menuitem"
                    className={`perm-menu-item ${permMode === 'default' ? 'active' : ''}`}
                    onClick={() => {
                      setPermOpen(false)
                      if (permMode !== 'default') {
                        if (hasSession) switchPermission('default')
                        else switchProjectPermission('default')
                      }
                    }}
                  >
                    <Icon name="shieldCheck" size={13} />
                    <span className="perm-menu-name">默认</span>
                    <span className="perm-menu-desc">敏感操作逐次审批</span>
                    {permMode === 'default' && <Icon name="check" size={13} />}
                  </div>
                  <div
                    role="menuitem"
                    className={`perm-menu-item ${permMode === 'full_access' ? 'active' : ''}`}
                    onClick={() => {
                      setPermOpen(false)
                      if (permMode !== 'full_access') {
                        if (hasSession) switchPermission('full_access')
                        else switchProjectPermission('full_access')
                      }
                    }}
                  >
                    <Icon name="shieldCheck" size={13} />
                    <span className="perm-menu-name">完全访问</span>
                    <span className="perm-menu-desc">跳过审批（硬拒绝仍生效）</span>
                    {permMode === 'full_access' && <Icon name="check" size={13} />}
                  </div>
                </div>
              </>
            )}
          </span>
          <span className="ws-select">
            <span
              className={`ctx-chip ${!hasSession && wsOpen ? 'open' : ''} ${hasSession ? '' : 'clickable'}`}
              title={
                currentProjectPath
                  ? `工作空间目录：${currentProjectPath}`
                  : hasSession
                    ? '默认工作空间 · 临时草稿目录 ~/.aigent/projects/default/scratch'
                    : '默认工作空间（新会话将使用临时草稿目录 scratch）'
              }
              onClick={hasSession ? undefined : () => setWsOpen((v) => !v)}
            >
              <Icon name="folder" size={13} />
              <span className="ctx-chip-label">{projectDisplayName(projects, currentProjectId)}</span>
              {!hasSession && <Icon name="chevronDown" size={11} />}
            </span>
            {/* 工作空间下拉：**仅新会话（无激活会话）可开** —— 已有会话的归属在
                创建时锁定，不可迁移（会话建成即锁空间）。上部分 = 已打开过的空间
                （点即切到该空间并新建会话），末尾固定项 = 选择文件夹。 */}
            {wsOpen && !hasSession && (
              <>
                <div className="ws-picker-mask" onClick={() => setWsOpen(false)} />
                <div className="ws-picker">
                  <div className="ws-picker-group">已打开的工作空间</div>
                  {projects.map((p) => (
                    <div
                      key={p.id}
                      role="menuitem"
                      className={`ws-picker-item ${p.id === currentProjectId ? 'active' : ''} ${p.exists ? '' : 'missing'}`}
                      title={p.path ?? '默认工作空间'}
                      onClick={() => {
                        setWsOpen(false)
                        if (p.exists) void openProject(p.id)
                        void newSession(p.id)
                      }}
                    >
                      <Icon name="folder" size={13} />
                      <span className="ws-picker-name">{p.name}</span>
                      {p.id === currentProjectId && <Icon name="check" size={13} />}
                    </div>
                  ))}
                  {projects.length === 0 && (
                    <div className="ws-picker-empty">暂无工作空间</div>
                  )}
                  <div className="ws-picker-sep" />
                  <div
                    role="menuitem"
                    className="ws-picker-item pick"
                    onClick={() => {
                      setWsOpen(false)
                      void addProjectFromPicker()
                    }}
                  >
                    <Icon name="plus" size={13} />
                    <span className="ws-picker-name">选择文件夹…</span>
                  </div>
                </div>
              </>
            )}
          </span>
        </div>

        <div className="toolbar-right">
          <div className="model-select">
            <button
              className="tool-btn model"
              title="切换模型"
              disabled={enabled.length === 0 || llmSaving}
              onClick={() => setModelOpen((v) => !v)}
            >
              <span className="model-name">{llmSaving ? '切换中…' : active?.display_name ?? '未配置模型'}</span>
              <Icon name="chevronDown" size={12} />
            </button>
            {modelOpen && enabled.length > 0 && (
              <>
                <div className="model-menu-mask" onClick={() => { setModelOpen(false); setHoveredPanel(null) }} />
                <div className="model-menu">
                  {enabled.map((m) => (
                    // 用 div 而非 button：面板内的思考强度 chip / 开关也是 button，
                    // button 嵌套 button 非法会被浏览器修复，导致面板不弹出。
                    <div
                      key={m.id}
                      role="menuitem"
                      className={`model-menu-item ${m.id === active?.id ? 'active' : ''}`}
                      onMouseEnter={(e) => {
                        clearHide()
                        const r = e.currentTarget.getBoundingClientRect()
                        setHoveredPanel({ id: m.id, ...resolvePanelPos(r) })
                      }}
                      onMouseLeave={() => scheduleHide(m.id)}
                      onClick={() => {
                        clearHide()
                        setModelOpen(false)
                        setHoveredPanel(null)
                        if (m.id !== active?.id) setSessionModel(m.id)
                      }}
                    >
                      <span className={`model-dot ${providerDot(m.provider)}`} />
                      <span className="model-menu-name">{m.display_name || m.id}</span>
                      {m.id === active?.id && <Icon name="check" size={13} />}
                    </div>
                  ))}
                </div>
                {/* 使用 Portal 把悬浮配置面板渲染到 body 顶层，脱离 .model-menu 及其祖先的
                    overflow / transform / 裁剪限制，面板可完整显示、必要时浮出菜单甚至窗口边界外的可视区。
                    新会话（activeSession === null，即无会话）依然允许设置参数 → 只要求模型有元数据即可，不依赖有会话。 */}
                {hoveredPanel &&
                  (() => {
                    const m = enabled.find((mm) => mm.id === hoveredPanel.id)
                    if (!m) return null
                    const meta = resolveModelMeta(llmConfig, m)
                    return meta ? (
                      createPortal(
                        // 仅负责定位的包装层；面板视觉样式由内部 .model-hover-panel 提供
                        <div
                          style={{
                            position: 'fixed',
                            left: hoveredPanel.x,
                            top: hoveredPanel.y,
                            zIndex: 9999
                          }}
                          onClick={(e) => e.stopPropagation()}
                          onMouseEnter={clearHide}
                          onMouseLeave={() => scheduleHide(hoveredPanel.id)}
                        >
                          <ModelConfigPanel
                            meta={meta}
                            overrides={overridesByModel[m.id] ?? null}
                            onChange={(ov) => setSessionOverrides(ov, m.id)}
                          />
                        </div>,
                        document.body
                      )
                    ) : null
                  })()}
              </>
            )}
          </div>
          {/* 上下文使用量圆圈指示器：仅选中会话时显示，用自定义悬浮提示替代不可靠的原生 title */}
          {hasSession && stats && (
            <div
              className="context-indicator"
              onMouseEnter={() => setCtxTooltip(true)}
              onMouseLeave={() => setCtxTooltip(false)}
            >
              <svg viewBox="0 0 36 36" className="context-circle">
                <path
                  className="context-bg"
                  d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                  fill="none"
                  strokeWidth="3.2"
                />
                <path
                  className="context-progress"
                  d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                  fill="none"
                  strokeWidth="3.2"
                  stroke={indicatorColor}
                  strokeLinecap="round"
                  strokeDasharray={`${Math.max(0, Math.min(100, usedPct))} ${100 - Math.max(0, Math.min(100, usedPct))}`}
                />
              </svg>
              {ctxTooltip && (
                <div className="context-tooltip">
                  <div>
                    上下文：已用 {stats.used_tokens.toLocaleString()} / 总计 {stats.max_label} tokens（{Math.round(usedPct)}%）
                  </div>
                  {sesHasData && sesUsage && (
                    <>
                      <div>
                        本会话累计输入：{sesUsage.prompt_tokens.toLocaleString()} tokens
                        {ctxMultiple ? `（约为上下文窗口的 ${ctxMultiple} 倍）` : ''}
                      </div>
                      <div>
                        缓存命中：{sesUsage.cached_tokens.toLocaleString()} tokens（命中率 {sesCachePct}）
                      </div>
                      <div>输出：{sesUsage.completion_tokens.toLocaleString()} tokens</div>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
          {isSending ? (
            <button className="send-btn stop" onClick={stop} title="停止">
              <Icon name="stop" size={15} />
              <span>停止</span>
            </button>
          ) : (
            <button
              className="send-btn"
              onClick={onSend}
              disabled={!canSend}
              title={
                stagingCount > 0
                  ? '附件正在读取…'
                  : approvalPending
                    ? '等待权限审批'
                    : imageUnsupported
                      ? '当前模型不支持图片输入'
                      : '发送'
              }
            >
              <Icon name="send" size={15} />
            </button>
          )}
        </div>
      </div>

      {/* 拖拽遮罩：pointer-events:none，不能挡住 drop 的落点 */}
      <DropOverlay visible={dragging} />
    </div>
  )
}

interface ModelMetaData {
  max_context?: string
  max_context_extended?: string
  thinking_strengths?: string[]
  default_thinking?: string
}

/** 模型悬浮配置面板：思考强度档位选择 + 更大上下文开关（仅作用当前会话下一轮） */
function ModelConfigPanel(props: {
  meta: ModelMetaData
  overrides: { thinkingStrength?: string; maxContextOption?: 'standard' | 'extended' } | null
  onChange: (ov: { thinkingStrength?: string; maxContextOption?: 'standard' | 'extended' } | null) => void
}): JSX.Element {
  const { meta, overrides, onChange } = props
  const strengths = meta.thinking_strengths ?? ['high']
  const defaultStrength = meta.default_thinking ?? 'high'
  const strength = overrides?.thinkingStrength ?? defaultStrength
  const extOption = overrides?.maxContextOption
  const canExtend = !!meta.max_context_extended

  return (
    <div className="model-hover-panel" onClick={(e) => e.stopPropagation()}>
      <div className="mhp-section">
        <span className="mhp-title">思考强度</span>
        <div className="mhp-strengths">
          {strengths.map((s) => (
            <button
              key={s}
              type="button"
              className={`mhp-chip ${s === strength ? 'active' : ''}`}
              onClick={() => onChange({ ...(overrides ?? {}), thinkingStrength: s })}
            >
              {THINKING_LABELS[s] ?? s}
            </button>
          ))}
        </div>
      </div>
      {canExtend && (
        <div className="mhp-section">
          <span className="mhp-title">更大上下文</span>
          <button
            type="button"
            className={`mhp-switch ${extOption === 'extended' ? 'on' : ''}`}
            role="switch"
            aria-checked={extOption === 'extended'}
            onClick={() => onChange({ ...(overrides ?? {}), maxContextOption: extOption === 'extended' ? 'standard' : 'extended' })}
          >
            <span className="mhp-switch-knob" />
          </button>
          <span className="mhp-hint">
            {extOption === 'extended'
              ? `已扩展至 ${meta.max_context_extended}`
              : `标准 ${meta.max_context ?? '默认'}`}
          </span>
        </div>
      )}
    </div>
  )
}
