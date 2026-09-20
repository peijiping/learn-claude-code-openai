import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '@components/common/Icon'
import { hasImageInput } from '@components/Settings/llmShared'
import { attachmentUrl, isSendableAttachment, showToast, useAgentStore, resolveModelMeta, providerDot, projectDisplayName, type DraftAttachment } from '@store/agentStore'
import PlusMenu, { PLUS_MENU_LABELS, type PlusMenuKey } from './PlusMenu'
import AttachmentBar, { type AttachmentView } from './AttachmentBar'
import DropOverlay from './DropOverlay'

interface InputBoxProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  /** 附件草稿（三条入口都写入 store，这里只读展示） */
  attachments: DraftAttachment[]
  /** 把一批本地绝对路径登记为附件（原生对话框 / 拖拽 / 粘贴共用） */
  onStagePaths: (paths: string[]) => void
  /** 移除一个草稿附件 */
  onRemoveAttachment: (key: string) => void
  /** 在系统文件管理器中定位附件原文件 */
  onOpenAttachment: (sourcePath: string) => void
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

/** 中央核心输入区：textarea + 附件条 + 工具栏 + 上下文条 */
export default function InputBox({
  value,
  onChange,
  onSend,
  attachments,
  onStagePaths,
  onRemoveAttachment,
  onOpenAttachment
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
  const taRef = useRef<HTMLTextAreaElement>(null)
  const [modelOpen, setModelOpen] = useState(false)
  // 加号「添加内容」菜单（2026-09-20）：本期只做外壳，各条目按 key 后续接线
  const [plusOpen, setPlusOpen] = useState(false)
  // 工作空间下拉（chip 点开）：上部分 = 已打开过的空间，末尾固定项 = 选择文件夹
  const [wsOpen, setWsOpen] = useState(false)
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

  // 会话建成即锁空间（2026-09-20）：工作空间选择只属于「新会话」。已有会话
  // 一律不可改归属 —— 下拉隐藏，chip 退化为只读展示；后端同样拒绝改归属，
  // 前端隐藏只是交互层的第一道门。
  useEffect(() => {
    if (hasSession) setWsOpen(false)
  }, [hasSession])
  const autoGrow = (el: HTMLTextAreaElement): void => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (!isSending && canSend) onSend()
    }
  }

  // ── 附件：拖拽 + 粘贴（两个入口都收敛到 onStagePaths）────────────────
  // 拖拽计数：dragenter/dragleave 会在进入子元素时成对冒泡，只用布尔量会让遮罩
  // 疯狂闪断 —— 用深度计数，归零才收起。
  const dragDepth = useRef(0)
  const [dragging, setDragging] = useState(false)

  /** 文件列表 → 本地路径列表（三条入口共用的收敛函数）。
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

  const onPaste = async (e: React.ClipboardEvent): Promise<void> => {
    const files = Array.from(e.clipboardData?.files ?? [])
    // 只在确实粘贴了文件/图片时才拦截默认行为，否则会把纯文本粘贴吃掉
    if (files.length === 0) return
    e.preventDefault()
    const paths = await pathsFromFiles(files)
    if (paths.length) onStagePaths(paths)
    else showToast('未能读取剪贴板内容', 'error', 3000)
  }

  // ── 附件能力预检（前后端双重守卫里的第一道）────────────────────────
  // 图片走 vision，需要模型声明 image 输入能力；文本/文档类不受限（走文本内联）。
  // 后端在 chat 分支还会再判一次（前端隐藏/禁用只是交互层，后端才是最终守卫）。
  const hasImageDraft = attachments.some((a) => a.kind === 'image')
  const imageUnsupported = hasImageDraft && !hasImageInput(active?.capabilities)
  // 按钮可用性与 ChatPanel 构造 payload 共用同一判据（`isSendableAttachment`）：
  // degraded 也算"可发送"—— 解析不完整不等于不能用。两处各写一遍正是附件被
  // 静默丢掉的原因，别再拆开。
  const readyCount = attachments.filter(isSendableAttachment).length
  const stagingCount = attachments.filter((a) => a.status === 'staging').length
  // 发送可用：有正文，或至少有一个就绪附件；且没有"正在读取"的附件（避免半成品发出去）
  const canSend = (value.trim().length > 0 || readyCount > 0) && stagingCount === 0 && !imageUnsupported

  // 加号「添加内容」菜单条目点击：先收起菜单，再分发。
  // 逐项接线的唯一落点 —— 按 key 分支即可（见 docs/frontend/02 §3.3）：
  //   attachFile → 已接线（原生文件对话框，2026-09-20）
  //   其余四项仍为占位 toast（各自是独立特性，见 docs/frontend/04 后续增量）
  const handlePlusPick = async (key: PlusMenuKey): Promise<void> => {
    setPlusOpen(false)
    if (key === 'attachFile') {
      const paths = await window.agent.pickFiles().catch(() => [] as string[])
      if (paths.length) onStagePaths(paths)
      return
    }
    showToast(`「${PLUS_MENU_LABELS[key]}」功能待开发`, 'info', 2000)
  }

  // 上下文圆圈：仅当有选中会话时才显示；数据来自后端 context_stats 事件
  const stats = currentContextStats
  const usedPct = stats ? stats.used_percent : 0
  const indicatorColor = usedPct >= 90 ? '#e5484d' : usedPct >= 70 ? '#f5a623' : '#2ea043'
  // 本会话 token 消耗累计（usage_stats 事件 / 切会话 usage_totals 恢复）：tooltip 后三行数据源
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

      <textarea
        ref={taRef}
        className="composer-input"
        placeholder="有什么我可以帮你的吗？"
        value={value}
        rows={1}
        onChange={(e) => {
          onChange(e.target.value)
          autoGrow(e.target)
        }}
        onKeyDown={onKeyDown}
        onPaste={(e) => void onPaste(e)}
      />

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
            {plusOpen && <PlusMenu onPick={handlePlusPick} onClose={() => setPlusOpen(false)} />}
          </span>
          <button className="tool-btn access">
            完全访问 <Icon name="chevronDown" size={12} />
          </button>
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
                  {enabled.map((m) => {
                    const meta = resolveModelMeta(llmConfig, m)
                    return (
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
                    )
                  })}
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