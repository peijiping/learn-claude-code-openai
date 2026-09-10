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
    num?: number | null,
    overrides?: { thinking_strength?: string; max_context?: string } | null,
    modelId?: string | null
  ): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'chat',
      payload: {
        text,
        ...(typeof num === 'number' ? { num } : {}),
        ...(overrides ? { overrides } : {}),
        ...(modelId ? { model_id: modelId } : {})
      }
    }))
    return Promise.resolve()
  }
  setSessionModel(payload: Parameters<AgentApi['setSessionModel']>[0]): Promise<void> {
    this.sendRaw(JSON.stringify({
      kind: 'session_model',
      payload: {
        ...(typeof payload?.num === 'number' ? { num: payload.num } : {}),
        ...(payload?.model_id ? { model_id: payload.model_id } : {}),
        ...(payload?.overrides ? { overrides: payload.overrides } : {})
      }
    }))
    return Promise.resolve()
  }
  stop(num: number): Promise<void> {
    this.sendRaw(JSON.stringify({ kind: 'stop', payload: { num } }))
    return Promise.resolve()
  }
  switchSession(num: number): Promise<{ num: number; message_count: number }> {
    // 历史回放经由 session / session_history 事件信封驱动 store，无需等待应答
    this.sendRaw(JSON.stringify({ kind: 'session_switch', payload: { num } }))
    return Promise.resolve({ num, message_count: 0 })
  }
  clearSession(): Promise<{ deleted: number }> {
    this.sendRaw(JSON.stringify({ kind: 'session_clear' }))
    return Promise.resolve({ deleted: 0 })
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
  async renameSession(num: number, title: string): Promise<unknown> {
    return this.request('session_rename', 'sessions', { num, title })
  }
  trashSession(num: number): Promise<unknown> {
    return this.request('session_trash', 'sessions', { num })
  }
  restoreSession(num: number): Promise<unknown> {
    return this.request('session_restore', 'sessions', { num })
  }
  async deleteSessions(nums: number[]): Promise<unknown> {
    return this.request('session_delete', 'session_delete_result', { nums })
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
