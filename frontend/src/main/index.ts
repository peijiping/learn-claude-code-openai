import { app, shell, BrowserWindow, ipcMain } from 'electron'
import { join } from 'path'
import { PythonManager, PythonStatus } from './pythonManager'
import { AgentWS, ConnStatus } from './agentWS'

const WS_PORT = Number(process.env.AGENT_WS_PORT || '8765')

let mainWindow: BrowserWindow | null = null
const python = new PythonManager({
  onStatus: (s: PythonStatus) =>
    mainWindow?.webContents.send('python:status', s),
  onLog: (line: string) => console.log(line)
})
const ws = new AgentWS({
  port: WS_PORT,
  onEvent: (envelope) => {
    mainWindow?.webContents.send('agent:event', envelope)
    resolvePending(envelope)
  },
  onStatus: (s: ConnStatus) => mainWindow?.webContents.send('agent:status', s)
})

type Pending = { kind: string; resolve: (v: unknown) => void; timer: NodeJS.Timeout }
const pending: Pending[] = []

function broadcastStatus(status: string): void {
  mainWindow?.webContents.send('agent:status', status)
}

function request(
  outKind: string,
  matchKind: string = outKind,
  payload?: Record<string, unknown>
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
    }, 5000)
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
    width: 1180,
    height: 800,
    minWidth: 860,
    minHeight: 600,
    show: false,
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

  ipcMain.handle('agent:send', (e, payload: { text?: string }) => {
    if (!isTrustedSender(e) || !payload?.text) return
    ws.send(JSON.stringify({ kind: 'chat', payload: { text: payload.text } }))
  })

  ipcMain.handle('agent:newSession', (e) => {
    if (!isTrustedSender(e)) return
    ws.send(JSON.stringify({ kind: 'session_new' }))
    return { ok: true }
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
  ipcMain.handle('agent:connectionStatus', (e) => {
    if (!isTrustedSender(e)) return 'disconnected'
    return ws.currentStatus
  })

  // dev 模式加载 Vite dev server，生产加载构建产物
  if (process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  createWindow()
  // 拉起后端并连接
  python.start()
  ws.connect()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  python.stop()
  ws.close()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  python.stop()
  ws.close()
})