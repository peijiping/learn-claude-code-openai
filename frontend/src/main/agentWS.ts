export type ConnStatus = 'connecting' | 'connected' | 'disconnected'

export interface AgentWSOptions {
  port: number
  onEvent: (envelope: unknown) => void
  onStatus: (status: ConnStatus) => void
}

/**
 * agentWS - 主进程里的 WebSocket 客户端，连 Python 后端的 ws_bridge。
 * 职责：连接/自动重连（指数退避）、把渲染进程命令发给后端、把后端事件转发给渲染进程。
 */
export class AgentWS {
  private ws: WebSocket | null = null
  private status: ConnStatus = 'disconnected'
  private retryMs = 1000
  private opts: AgentWSOptions
  private manualClose = false
  private queue: string[] = []
  /** 是否成功连上过：区分"后端尚未就绪的首连失败（正常等待）"与"运行中断开（异常）" */
  private everConnected = false

  constructor(opts: AgentWSOptions) {
    this.opts = opts
  }

  get url(): string {
    return `ws://127.0.0.1:${this.opts.port}`
  }

  get currentStatus(): ConnStatus {
    return this.status
  }

  private setStatus(s: ConnStatus): void {
    if (this.status !== s) {
      this.status = s
      this.opts.onStatus(s)
    }
  }

  connect(): void {
    // 重入守卫：已有 socket（OPEN/CONNECTING）时不重复建连。
    // 历史 bug：scheduleReconnect 的 setTimeout 与 send() 的兜底 connect()
    // 竞争并发调用时 this.ws 被覆盖、旧 socket 不关闭 → 产生僵尸 ESTABLISHED
    // 连接，其 onmessage 事件被静默丢弃（表现为"已连接但事件断流"）。
    if (this.ws) return
    this.manualClose = false
    this.setStatus('connecting')
    const ws = new WebSocket(this.url)
    this.ws = ws

    ws.onopen = (): void => {
      this.everConnected = true
      console.log(`[agentWS] connected -> ${this.url}`)
      this.setStatus('connected')
      this.retryMs = 1000
      // 连接建立后补发排队中的命令
      while (this.queue.length) {
        const raw = this.queue.shift()
        if (raw !== undefined) ws.send(raw)
      }
    }

    ws.onmessage = (ev: MessageEvent): void => {
      try {
        this.opts.onEvent(JSON.parse(ev.data as string))
      } catch (err) {
        this.opts.onEvent({ kind: 'error', payload: { msg: `解析失败: ${(err as Error).message}` } })
      }
    }

    ws.onclose = (ev: CloseEvent): void => {
      // 诊断日志：code/reason 直接暴露客户端侧断连的触发原因。
      // 首连失败（Python 还在 import、端口未 listen）是启动期正常现象，
      // 单独标注，避免每次 dev 启动都出现误导性的 1006 报错。
      const notReady = !this.everConnected
      console.log(
        `[agentWS] onclose: code=${ev.code} reason=${ev.reason || '(empty)'} wasClean=${ev.wasClean}` +
          (notReady ? ' (python bridge not ready yet, will retry)' : '')
      )
      // 迟到的旧 socket 关闭事件：已被新连接替换，忽略（防止误置状态/触发多余重连）
      if (this.ws !== ws) return
      this.ws = null
      this.setStatus('disconnected')
      if (this.manualClose) return
      this.scheduleReconnect()
    }

    ws.onerror = (ev: Event): void => {
      console.log(`[agentWS] onerror: ${(ev as ErrorEvent).message || ev.type}`)
      // onclose 会随后触发，交给 onclose 统一处理
      try {
        ws.close()
      } catch {
        /* 忽略 */
      }
    }
  }

  private scheduleReconnect(): void {
    const delay = this.retryMs
    this.retryMs = Math.min(this.retryMs * 2, 30_000)
    setTimeout(() => {
      if (!this.manualClose) this.connect()
    }, delay)
  }

  /** 发送一条命令；未连接时进入队列，连接后立刻补发 */
  send(raw: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(raw)
    } else {
      this.queue.push(raw)
      if (!this.ws) this.connect()
    }
  }

  close(): void {
    this.manualClose = true
    if (this.ws) this.ws.close()
    this.ws = null
    this.setStatus('disconnected')
  }
}