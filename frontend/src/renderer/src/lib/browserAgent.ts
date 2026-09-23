/** 与 preload 暴露的 AgentApi 同一形状（preload/index.d.ts 已入 web tsconfig，直接取其类型） */
type AgentApi = typeof window.agent

/**
 * browserAgent - 纯浏览器环境（无 Electron preload，如 dev 预览页）的回退桥。
 *
 * Electron 里渲染层只经 preload 桥（window.agent）与主进程通信；在普通浏览器
 * （vite dev server 直接打开渲染页）中 preload 不存在，window.agent 为 undefined，
 * 发消息会抛 TypeError 且连接状态永远"未连接"。本模块实现与 preload 完全相同的
 * AgentApi，但把命令直接发往后端 ws_bridge 的 WebSocket（与主进程 agentWS.ts
 * 同一协议：指数退避重连 + 断线排队补发），使浏览器预览具备完整对话能力。
 *
 * 注意：Electron 内 preload 先于渲染层脚本运行，installBrowserAgent() 会因
 * window.agent 已存在而跳过，行为不变。
 */

type Envelope = { kind: string; payload?: unknown }
type Pending = { kind: string; resolve: (v: unknown) => void; timer: ReturnType<typeof setTimeout> }

const WS_PORT = Number(import.meta.env.VITE_AGENT_WS_PORT || '8765')
const REQUEST_TIMEOUT_MS = 5000

class BrowserAgentBridge implements AgentApi {
  private ws: WebSocket | null = null
  private status: 'connecting' | 'connected' | 'disconnected' = 'disconnected'
  private retryMs = 1000
  private manualClose = false
  private queue: string[] = []
  private pending: Pending[] = []
  private eventCbs = new Set<(e: unknown) => void>()
  private statusCbs = new Set<(s: string) => void>()
  private pythonCbs = new Set<(s: string) => void>()

  constructor() {
    this.setPython('starting')
    this.connect()
  }

  connect(): void {
    // 重入守卫与主进程 agentWS 相同：防止重连定时器与 send 兜底并发建连产生僵尸连接
    if (this.ws) return
    this.manualClose = false
    this.setStatus('connecting')
    const ws = new WebSocket(`ws://127.0.0.1:${WS_PORT}`)
    this.ws = ws

    ws.onopen = (): void => {
      this.setStatus('connected')
      this.retryMs = 1000
      // ws_bridge 存活即代表 Python 后端在运行
      this.setPython('running')
      while (this.queue.length) {
        const raw = this.queue.shift()
        if (raw !== undefined) ws.send(raw)
      }
    }

    ws.onmessage = (ev: MessageEvent): void => {
      let envelope: Envelope
      try {
        envelope = JSON.parse(ev.data as string) as Envelope
      } catch {
        return
      }
      // 与主进程转发行为一致：所有信封（含应答类）都上行给订阅者
      this.eventCbs.forEach((cb) => cb(envelope))
      this.resolvePending(envelope)
    }

    ws.onclose = (): void => {
      if (this.ws !== ws) return // 迟到的旧 socket 关闭事件，忽略
      this.ws = null
      this.setStatus('disconnected')
      this.setPython('stopped')
      if (!this.manualClose) {
        const delay = this.retryMs
        this.retryMs = Math.min(this.retryMs * 2, 30_000)
        setTimeout(() => {
          if (!this.manualClose) this.connect()
        }, delay)
      }
    }

    ws.onerror = (): void => {
      try {
        ws.close()
      } catch {
        /* onclose 统一处理 */
      }
    }
  }

  private setStatus(s: 'connecting' | 'connected' | 'disconnected'): void {
    if (this.status === s) return
    this.status = s
    this.statusCbs.forEach((cb) => cb(s))
  }

  private setPython(s: string): void {
    this.pythonCbs.forEach((cb) => cb(s))
  }

  private resolvePending(envelope: Envelope): void {
    const match = this.pending.find((p) => p.kind === envelope.kind)
    if (!match) return
    this.pending.splice(this.pending.indexOf(match), 1)
    clearTimeout(match.timer)
    match.resolve(envelope.payload)
  }

  private request(
    outKind: string,
    matchKind: string = outKind,
    payload?: Record<string, unknown>,
    timeoutMs: number = REQUEST_TIMEOUT_MS
  ): Promise<unknown> {
    this.sendRaw(JSON.stringify({ kind: outKind, ...(payload ? { payload } : {}) }))
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        const i = this.pending.findIndex((p) => p.kind === matchKind && p.timer === timer)
        if (i >= 0) this.pending.splice(i, 1)
        resolve(null)
      }, timeoutMs)
      this.pending.push({
        kind: matchKind,
        resolve: (v) => {
          clearTimeout(timer)
          resolve(v)
        },
        timer
      })
    })
  }

  private sendRaw(raw: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(raw)
    } else {
      this.queue.push(raw)
      if (!this.ws) this.connect()
    }
  }

  // ── AgentApi 实现（与 preload/主进程 IPC 的语义一一对应） ──────────
  // 命令类（chat/stop/…）后端无点对点应答信封（结果由流式/广播事件驱动），fire-and-forget

  send(
    text: string,
    sessionId?: string | null,
    overrides?: { thinking_strength?: string; max_context?: string } | null,
    modelId?: string | null,
    projectId?: string | null,
    attachments?: Parameters<AgentApi['send']>[5],
    refs?: Parameters<AgentApi['send']>[6]
  ): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'chat',
      payload: {
        text,
        ...(typeof sessionId === 'string' && sessionId ? { session_id: sessionId } : {}),
        ...(typeof projectId === 'string' && projectId ? { project_id: projectId } : {}),
        ...(overrides ? { overrides } : {}),
        ...(modelId ? { model_id: modelId } : {}),
        ...(attachments?.length ? { attachments } : {}),
        ...(refs?.length ? { refs } : {})
      }
    }))
    return Promise.resolve()
  }
  setSessionModel(payload: Parameters<AgentApi['setSessionModel']>[0]): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'session_model',
      payload: {
        ...(typeof payload?.session_id === 'string' && payload.session_id ? { session_id: payload.session_id } : {}),
        ...(payload?.model_id ? { model_id: payload.model_id } : {}),
        ...(payload?.overrides ? { overrides: payload.overrides } : {})
      }
    }))
    return Promise.resolve()
  }
  stop(sessionId: string): Promise<void> {
    this.sendRaw(JSON.stringify({ kind: 'stop', payload: { session_id: sessionId } }))
    return Promise.resolve()
  }
  // 结构化提问作答 / 取消（ask_user）：与 stop 同类，后端无点对点应答信封，
  // 回执走 `ask_resolved` 广播 → fire-and-forget。
  answerAsk(sessionId: string, requestId: string, answers: unknown[]): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'ask_answer',
      payload: { session_id: sessionId, request_id: requestId, answers: Array.isArray(answers) ? answers : [] }
    }))
    return Promise.resolve()
  }
  cancelAsk(sessionId: string, requestId: string): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'ask_cancel',
      payload: { session_id: sessionId, request_id: requestId }
    }))
    return Promise.resolve()
  }
  // 权限管控（2026-09-22）：审批作答 / 切换会话权限档位 —— 同 ask_answer 的
  // fire-and-forget（后端无点对点应答信封，回执走 approval_resolved /
  // permission_changed 广播）。
  approvalAnswer(sessionId: string, requestId: string, decision: string): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'approval_answer',
      payload: { session_id: sessionId, request_id: requestId, decision }
    }))
    return Promise.resolve()
  }
  sessionPermission(sessionId: string, mode: string): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'session_permission',
      payload: { session_id: sessionId, mode }
    }))
    return Promise.resolve()
  }
  projectPermission(projectId: string, mode: string): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'project_permission',
      payload: { project_id: projectId, mode }
    }))
    return Promise.resolve()
  }
  switchSession(sessionId: string): Promise<{ session_id: string; message_count: number }> {
    // 历史回放经由 session / session_history 事件信封驱动 store，无需等待应答
    this.sendRaw(JSON.stringify({ kind: 'session_switch', payload: { session_id: sessionId } }))
    return Promise.resolve({ session_id: sessionId, message_count: 0 })
  }
  clearSession(): Promise<{ deleted: number }> {
    this.sendRaw(JSON.stringify({ kind: 'session_clear' }))
    return Promise.resolve({ deleted: 0 })
  }
  async setSessionUnread(payload: Parameters<AgentApi['setSessionUnread']>[0]): Promise<unknown> {
    if (typeof payload?.session_id !== 'string' || !payload.session_id) return null
    return this.request('session_set_unread', 'sessions', { session_id: payload.session_id, unread: Boolean(payload.unread) })
  }

  async listSessions(): Promise<unknown[]> {
    const payload = (await this.request('sessions_list', 'sessions')) as
      | { sessions?: unknown[] }
      | unknown[]
      | null
    return Array.isArray(payload) ? payload : (payload?.sessions ?? [])
  }
  async listTrash(): Promise<unknown[]> {
    const payload = (await this.request('trash_list', 'sessions_trashed')) as
      | { sessions?: unknown[] }
      | null
    return payload?.sessions ?? []
  }
  async renameSession(sessionId: string, title: string): Promise<unknown> {
    return this.request('session_rename', 'sessions', { session_id: sessionId, title })
  }
  trashSession(sessionId: string): Promise<unknown> {
    return this.request('session_trash', 'sessions', { session_id: sessionId })
  }
  restoreSession(sessionId: string): Promise<unknown> {
    return this.request('session_restore', 'sessions', { session_id: sessionId })
  }
  async deleteSessions(ids: string[]): Promise<unknown> {
    return this.request('session_delete', 'session_delete_result', { ids })
  }

  // ── 工作空间（浏览器无宿主能力，降级为可用的最小实现）────────────
  /** 浏览器无原生目录选择框：退化为输入路径（仍是本机后端进程去读写，语义一致） */
  pickFolder(): Promise<string | null> {
    const p = window.prompt('输入工作空间目录的绝对路径：')
    return Promise.resolve(p && p.trim() ? p.trim() : null)
  }
  /** 浏览器无法调起 Finder：仅记录告警，不阻断调用方 */
  openInFinder(_path: string): Promise<{ ok: boolean; error?: string }> {
    console.warn('[browserAgent] openInFinder 仅在 Electron 宿主中可用')
    return Promise.resolve({ ok: false, error: 'not supported in browser' })
  }

  // ── 会话附件（浏览器无宿主能力，降级为可用的最小实现）──────────────
  /** 浏览器拿不到本地文件路径（File 对象没有 path，也不允许 JS 读取磁盘）：
   *  与 pickFolder 同款降级 —— 让用户直接输入绝对路径，后端仍在同机读盘。 */
  pickFiles(): Promise<string[]> {
    const raw = window.prompt('输入要添加的文件绝对路径（多个用换行或逗号分隔，多个文件不可点击添加）：')
    if (!raw || !raw.trim()) return Promise.resolve([])
    return Promise.resolve(
      raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
    )
  }
  /** 浏览器里 DOM File 没有磁盘路径 → 恒为空串（调用方会改走 readClipboardImage 兜底） */
  getPathForFile(_file: File): string {
    return ''
  }
  /** 浏览器无法把字节写成临时文件（无 fs 权限）→ 明确降级 */
  saveClipboardImage(_payload: { bytes: ArrayBuffer | Uint8Array; mime?: string }): Promise<string | null> {
    console.warn('[browserAgent] 粘贴图片仅在 Electron 宿主中可用')
    return Promise.resolve(null)
  }
  async stageAttachments(payload: { paths: string[]; projectId?: string | null }): Promise<unknown> {
    if (!Array.isArray(payload?.paths) || payload.paths.length === 0) return null
    return this.request(
      'attachment_stage',
      'attachments_staged',
      {
        paths: payload.paths,
        ...(payload.projectId ? { project_id: payload.projectId } : {})
      },
      20000
    )
  }
  async listProjects(): Promise<unknown> {
    return this.request('projects_list', 'projects')
  }

  // ── 引用文件或文件夹（@-mention）────────────────────────────────
  /** 浏览器预览里同样能跑通：桥本身就连着 ws_bridge，直接发 refs_list 即可
   *  （后端同机读盘，与 Electron 路径语义一致；超时放宽到大仓库遍历的量级）。 */
  async listRefs(payload?: { projectId?: string | null; sessionId?: string | null }): Promise<unknown> {
    return this.request(
      'refs_list',
      'refs',
      {
        ...(typeof payload?.projectId === 'string' && payload.projectId
          ? { project_id: payload.projectId }
          : {}),
        ...(typeof payload?.sessionId === 'string' && payload.sessionId
          ? { session_id: payload.sessionId }
          : {})
      },
      20000
    )
  }
  // ── 右侧面板（2026-09-23，docs/frontend/19）────────────────────────
  /** 右栏状态上报：**fire-and-forget**（后端 `session_ui` 无点对点回执，同 stop）。
   *  状态本身以内存桶为准，这里只是把它写进会话元数据，丢了不影响当前体验。 */
  sessionUi(payload: { session_id: string; ui: unknown }): Promise<void> {
    if (!payload?.session_id) return Promise.resolve()
    this.sendRaw(JSON.stringify({
      kind: 'session_ui',
      payload: { session_id: payload.session_id, ui: payload.ui ?? null }
    }))
    return Promise.resolve()
  }
  async readFile(payload: { path: string; sessionId?: string | null; projectId?: string | null }): Promise<unknown> {
    return this.request('file_read', 'file_content', {
      path: payload?.path ?? '',
      ...(typeof payload?.projectId === 'string' && payload.projectId
        ? { project_id: payload.projectId }
        : {}),
      ...(typeof payload?.sessionId === 'string' && payload.sessionId
        ? { session_id: payload.sessionId }
        : {})
    }, 20000)
  }
  async gitStatus(payload?: { sessionId?: string | null; projectId?: string | null }): Promise<unknown> {
    return this.request('git_status', 'git_status', {
      ...(typeof payload?.projectId === 'string' && payload.projectId
        ? { project_id: payload.projectId }
        : {}),
      ...(typeof payload?.sessionId === 'string' && payload.sessionId
        ? { session_id: payload.sessionId }
        : {})
    }, 20000)
  }
  async gitDiff(payload: {
    path: string
    staged?: boolean
    sessionId?: string | null
    projectId?: string | null
  }): Promise<unknown> {
    return this.request('git_diff', 'git_diff', {
      path: payload?.path ?? '',
      ...(payload?.staged ? { staged: true } : {}),
      ...(typeof payload?.projectId === 'string' && payload.projectId
        ? { project_id: payload.projectId }
        : {}),
      ...(typeof payload?.sessionId === 'string' && payload.sessionId
        ? { session_id: payload.sessionId }
        : {})
    }, 20000)
  }
  async addProject(path: string): Promise<unknown> {
    return this.request('project_add', 'projects', { path })
  }
  openProject(projectId: string): Promise<void> {
    this.sendRaw(JSON.stringify({ kind: 'project_open', payload: { project_id: projectId } }))
    return Promise.resolve()
  }
  async renameProject(projectId: string, name: string): Promise<unknown> {
    return this.request('project_rename', 'projects', { project_id: projectId, name })
  }
  async removeProject(projectId: string): Promise<unknown> {
    return this.request('project_remove', 'projects', { project_id: projectId })
  }

  async goalStatus(): Promise<string> {
    const payload = (await this.request('goal_status')) as { text?: string } | null
    return payload?.text ?? ''
  }
  async tasks(): Promise<string> {
    const payload = (await this.request('tasks')) as { text?: string } | null
    return payload?.text ?? ''
  }
  async skills(): Promise<string> {
    const payload = (await this.request('skills')) as { text?: string } | null
    return payload?.text ?? ''
  }

  async getConnectionStatus(): Promise<string> {
    return this.status
  }
  /** 浏览器预览与 Electron 同语义：fire-and-forget 发 status_query，
   * 运行状态经后端重放的 session_status 广播信封驱动 store */
  queryStatus(): Promise<{ ok: boolean }> {
    this.sendRaw(JSON.stringify({ kind: 'status_query' }))
    return Promise.resolve({ ok: true })
  }
  async llmConfigGet(): Promise<unknown> {
    return this.request('llm_config_get', 'llm_config')
  }
  llmConfigSave(config: unknown): Promise<unknown> {
    return this.request('llm_config_save', 'llm_config', { config })
  }
  /** 权限配置（设置页「权限」页，docs/frontend/18） */
  async permissionConfigGet(): Promise<unknown> {
    return this.request('permission_config_get', 'permission_config')
  }
  permissionConfigSave(config: unknown): Promise<unknown> {
    return this.request('permission_config_save', 'permission_config', { config })
  }
  /** 沙盒设置（设置页「沙盒」页，docs/frontend/20）：save 是字段部分更新载荷 */
  async sandboxConfigGet(): Promise<unknown> {
    return this.request('sandbox_config_get', 'sandbox_config')
  }
  sandboxConfigSave(payload: object): Promise<unknown> {
    return this.request('sandbox_config_save', 'sandbox_config', payload as Record<string, unknown>)
  }
  /** 刷新远端模型列表：GET /models 可能较慢，超时放宽到 30s */
  llmModelsFetch(payload: {
    base_url?: string
    api_key?: string
    connection_id?: string
    api_format?: string
    models_path?: string
  }): Promise<unknown> {
    return this.request(
      'llm_models_fetch',
      'llm_models',
      {
        base_url: payload?.base_url ?? '',
        api_key: payload?.api_key ?? '',
        connection_id: payload?.connection_id ?? '',
        api_format: payload?.api_format ?? '',
        models_path: payload?.models_path ?? '/models'
      },
      30000
    )
  }

  onEvent(cb: (e: unknown) => void): () => void {
    this.eventCbs.add(cb)
    return () => this.eventCbs.delete(cb)
  }
  onStatus(cb: (status: string) => void): () => void {
    this.statusCbs.add(cb)
    cb(this.status)
    return () => this.statusCbs.delete(cb)
  }
  onPythonStatus(cb: (status: string) => void): () => void {
    this.pythonCbs.add(cb)
    return () => this.pythonCbs.delete(cb)
  }
}

/**
 * 安装浏览器回退桥：window.agent 已存在（Electron preload）时不做任何事。
 * 应在渲染层入口、React 挂载之前调用。
 */
export function installBrowserAgent(): void {
  if (typeof window === 'undefined' || window.agent) return
  window.agent = new BrowserAgentBridge()
}
