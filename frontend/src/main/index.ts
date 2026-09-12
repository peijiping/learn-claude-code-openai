import { app, shell, BrowserWindow, ipcMain, nativeImage } from 'electron'
import { join } from 'path'
import { PythonManager, PythonStatus } from './pythonManager'
import { AgentWS, ConnStatus } from './agentWS'
import { flog } from './logger'

const WS_PORT = Number(process.env.AGENT_WS_PORT || '8765')

// 应用图标：机器人头像（dev 下位于工程根 build/icon.jpg；打包后位于安装资源目录）
const APP_ICON = join(app.getAppPath(), 'build/icon.jpg')

// macOS Dock 图标：nativeImage 可直接用该路径加载（PNG/JPG 均可）
function ensureAppIcon(): void {
  if (process.platform === 'darwin' && app.dock) {
    const icon = nativeImage.createFromPath(APP_ICON)
    if (!icon.isEmpty()) app.dock.setIcon(icon)
  }
}

let mainWindow: BrowserWindow | null = null
const python = new PythonManager({
  onStatus: (s: PythonStatus) => {
    flog.info('python', `后端状态: ${s}`)
    mainWindow?.webContents.send('python:status', s)
  },
  onLog: (line: string) => console.log(line)
})
const ws = new AgentWS({
  port: WS_PORT,
  onEvent: (envelope) => {
    mainWindow?.webContents.send('agent:event', envelope)
    resolvePending(envelope)
  },
  onStatus: (s: ConnStatus) => {
    flog.info('ws', `WS 连接状态: ${s}`)
    mainWindow?.webContents.send('agent:status', s)
  }
})

type Pending = { kind: string; resolve: (v: unknown) => void; timer: NodeJS.Timeout }
const pending: Pending[] = []

function broadcastStatus(status: string): void {
  mainWindow?.webContents.send('agent:status', status)
}

function request(
  outKind: string,
  matchKind: string = outKind,
  payload?: Record<string, unknown>,
  timeoutMs = 5000
): Promise<unknown> {
  ws.send(JSON.stringify({ kind: outKind, ...(payload ? { payload } : {}) }))
  return new Promise((resolve) => {
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      const i = pending.findIndex((p) => p.kind === matchKind && p.timer === timer)
      if (i >= 0) pending.splice(i, 1)
      resolve(null)
    }, timeoutMs)
    const handleResult = (v: unknown): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(v)
    }
    pending.push({ kind: matchKind, resolve: handleResult, timer })
  })
}

function resolvePending(envelope: unknown): void {
  const kind = (envelope as { kind?: string })?.kind
  if (!kind) return
  const match = pending.find((p) => p.kind === kind)
  if (!match) return
  pending.splice(pending.indexOf(match), 1)
  clearTimeout(match.timer)
  match.resolve((envelope as { payload?: unknown }).payload)
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1380,
    height: 800,
    minWidth: 860,
    minHeight: 600,
    show: false,
    icon: APP_ICON, // Windows/Linux 窗口与任务栏图标
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  mainWindow.on('ready-to-show', () => mainWindow?.show())

  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  // 渲染层来源校验：只处理本窗口的 IPC
  const isTrustedSender = (event: Electron.IpcMainInvokeEvent): boolean =>
    event.sender === mainWindow?.webContents

  ipcMain.handle('agent:send', (e, payload: { text?: string; num?: number | null; overrides?: { thinking_strength?: string; max_context?: string } | null; model_id?: string | null }) => {
    if (!isTrustedSender(e) || !payload?.text) return
    // num 缺省/null = 新建任务（后端惰性领号建会话）；否则定位到目标会话
    const num = typeof payload.num === 'number' ? payload.num : undefined
    ws.send(JSON.stringify({
      kind: 'chat',
      payload: {
        text: payload.text,
        ...(num !== undefined ? { num } : {}),
        ...(payload.overrides ? { overrides: payload.overrides } : {}),
        ...(payload.model_id ? { model_id: payload.model_id } : {})
      }
    }))
  })

  ipcMain.handle('agent:setSessionModel', (e, payload: { num?: number; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }) => {
    if (!isTrustedSender(e) || typeof payload?.num !== 'number') return
    ws.send(JSON.stringify({
      kind: 'session_model',
      payload: {
        num: payload.num,
        ...(payload.model_id ? { model_id: payload.model_id } : {}),
        ...(payload.overrides ? { overrides: payload.overrides } : {})
      }
    }))
  })

  ipcMain.handle('agent:stop', (e, payload: { num?: number }) => {
    if (!isTrustedSender(e) || typeof payload?.num !== 'number') return
    ws.send(JSON.stringify({ kind: 'stop', payload: { num: payload.num } }))
  })

  ipcMain.handle('agent:switchSession', (e, payload: { num?: number }) => {
    if (!isTrustedSender(e) || typeof payload?.num !== 'number') return
    ws.send(JSON.stringify({ kind: 'session_switch', payload: { num: payload.num } }))
    return { ok: true }
  })
  ipcMain.handle('agent:clearSession', (e) => {
    if (!isTrustedSender(e)) return
    ws.send(JSON.stringify({ kind: 'session_clear' }))
    return { ok: true }
  })

  ipcMain.handle('agent:listSessions', (e) =>
    isTrustedSender(e) ? request('sessions_list') : null
  )
  // 会话管理：重命名 / 软删除（回收站）/ 还原 / 批量永久删除 / 回收站列表
  ipcMain.handle(
    'agent:renameSession',
    (e, payload: { num?: number; title?: string }) => {
      if (!isTrustedSender(e) || typeof payload?.num !== 'number' || !payload?.title) return null
      return request('session_rename', 'sessions', { num: payload.num, title: payload.title })
    }
  )
  ipcMain.handle('agent:trashSession', (e, payload: { num?: number }) => {
    if (!isTrustedSender(e) || typeof payload?.num !== 'number') return null
    return request('session_trash', 'sessions', { num: payload.num })
  })
  ipcMain.handle('agent:restoreSession', (e, payload: { num?: number }) => {
    if (!isTrustedSender(e) || typeof payload?.num !== 'number') return null
    return request('session_restore', 'sessions', { num: payload.num })
  })
  ipcMain.handle('agent:deleteSessions', (e, payload: { nums?: number[] }) => {
    if (!isTrustedSender(e) || !Array.isArray(payload?.nums) || payload.nums.length === 0) return null
    return request('session_delete', 'session_delete_result', { nums: payload.nums })
  })
  ipcMain.handle('agent:listTrash', (e) =>
    isTrustedSender(e) ? request('trash_list', 'sessions_trashed') : null
  )
  ipcMain.handle('agent:goalStatus', (e) =>
    isTrustedSender(e) ? request('goal_status') : null
  )
  ipcMain.handle('agent:tasks', (e) => (isTrustedSender(e) ? request('tasks') : null))
  ipcMain.handle('agent:skills', (e) => (isTrustedSender(e) ? request('skills') : null))
  ipcMain.handle('agent:llmConfigGet', (e) =>
    isTrustedSender(e) ? request('llm_config_get', 'llm_config') : null
  )
  ipcMain.handle('agent:llmConfigSave', (e, payload: { config?: unknown }) => {
    if (!isTrustedSender(e) || payload?.config === undefined) return null
    return request('llm_config_save', 'llm_config', { config: payload.config })
  })
  // 「刷新模型列表」：远端 GET /models 可能较慢，超时放宽到 30s
  ipcMain.handle(
    'agent:llmModelsFetch',
    (
      e,
      payload?: {
        base_url?: string
        api_key?: string
        connection_id?: string
        api_format?: string
        models_path?: string
      }
    ) => {
      if (!isTrustedSender(e)) return null
      return request(
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
  )
  ipcMain.handle('agent:connectionStatus', (e) => {
    if (!isTrustedSender(e)) return 'disconnected'
    return ws.currentStatus
  })
  // 前端主动拉取运行状态：渲染进程刷新/HMR 不重建主进程 WS 连接，
  // 后端"新连接重放"覆盖不到该场景，由前端就绪后发 status_query 补偿，
  // 后端把仍在运行会话的 session_status（running/background）重放回来。
  ipcMain.handle('agent:queryStatus', (e) => {
    if (!isTrustedSender(e)) return { ok: false }
    ws.send(JSON.stringify({ kind: 'status_query' }))
    return { ok: true }
  })

  // dev 模式加载 Vite dev server，生产加载构建产物
  if (process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  flog.info('app', `Electron 主进程启动 (electron=${process.versions.electron}, node=${process.versions.node}, pid=${process.pid})`)
  ensureAppIcon() // macOS Dock 图标
  createWindow()
  // 方案B：先建窗显示界面；后端冷启动期间由渲染进程的"启动画面"遮罩覆盖
  //（见 02-界面功能设计.md 启动遮罩），直至 WS 首次 connected 后切主界面。
  python.start()
  ws.connect()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  flog.info('app', '所有窗口关闭，退出应用')
  python.stop()
  ws.close()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  flog.info('app', '应用退出清理（before-quit）')
  python.stop()
  ws.close()
})