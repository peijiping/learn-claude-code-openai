import { app, shell, BrowserWindow, dialog, ipcMain, nativeImage, net, protocol } from 'electron'
import { existsSync, mkdirSync, readdirSync, realpathSync, statSync, unlinkSync, writeFileSync } from 'fs'
import { homedir, tmpdir } from 'os'
import { join, sep } from 'path'
import { pathToFileURL } from 'url'
import { randomUUID } from 'crypto'
import { PythonManager, PythonStatus } from './pythonManager'
import { AgentWS, ConnStatus } from './agentWS'
import { flog } from './logger'

const WS_PORT = Number(process.env.AGENT_WS_PORT || '8765')

// ── 会话附件（「添加文件或图片」）──────────────────────────────────────
// 三类入口（原生对话框 / 拖拽 / 粘贴）都收敛成"本地绝对路径"，交后端读盘。
// 主进程只负责：弹对话框、给拖拽取路径、把剪贴板图片落成临时文件、
// 以及用自定义协议把会话内副本喂给渲染层的 <img>。
// 设计见 docs/frontend/12-附件与文件输入.md。

/** 附件缩略图自定义协议名（CSP 里 img-src 需放行同名 scheme） */
const ATT_SCHEME = 'aigent-att'
/** 后端附件目录名（与 agents/paths.ATTACHMENTS_DIRNAME 一致） */
const ATTACHMENTS_DIRNAME = '.attachments'
/** 未发送附件的草稿区（与 agents/paths.DRAFT_ATTACHMENTS_DIRNAME 一致） */
const DRAFT_DIRNAME = '_draft'
/** 解析产物后缀：不是"原文件"，展示时优先跳过 */
const ATT_META_SUFFIX = '.meta.json'
const ATT_SEND_IMAGE_SUFFIX = '.send.jpg'
/** att_id / project_id 形状白名单（与后端 attachments._ATT_ID_RE 同口径）。
 *  只接受这种固定形状 = 根本不存在 `..` 或分隔符注入的空间。 */
const ATT_ID_RE = /^att_[0-9A-Za-z]{6,32}$/
const PROJECT_ID_RE = /^(default|ws[0-9A-Za-z]{10})$/
/** 剪贴板图片的临时落点（无磁盘路径的截图先落这里，再交给后端登记） */
const CLIP_TMP_DIR = join(tmpdir(), 'aigent-attachments')
/** 临时文件存活时间：超过即清（后端登记时会把它复制进会话目录，这里只是中转） */
const CLIP_TMP_TTL_MS = 24 * 3600 * 1000
/** 剪贴板图片体积上限（与后端单图上限同量级；超大图不该走"粘贴"这条路） */
const CLIP_MAX_BYTES = 10 * 1024 * 1024
/** MIME → 扩展名：后端按扩展名判类型，落盘时写对后缀是必须的 */
const EXT_BY_MIME: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/jpg': 'jpg',
  'image/webp': 'webp',
  'image/gif': 'gif',
  'image/bmp': 'bmp',
  'image/tiff': 'png' // macOS 截图可能是 TIFF：Chromium 粘贴时会转成 PNG，兜底写 png
}

/** 文件选择框白名单（**只是建议**：macOS 的 filters 只隐藏不禁止，拖拽可绕过；
 *  真正的守卫在后端 —— 按扩展名 + 体积逐条校验并回可读原因）。 */
const TEXT_EXTENSIONS = [
  'txt', 'md', 'markdown', 'rst', 'log', 'csv', 'tsv', 'json', 'jsonl',
  'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'xml', 'html', 'htm', 'css',
  'scss', 'less', 'svg', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'svelte',
  'java', 'kt', 'go', 'rs', 'rb', 'php', 'cs', 'swift', 'c', 'h', 'cpp',
  'hpp', 'sql', 'sh', 'bash', 'zsh', 'bat', 'ps1', 'lua', 'r', 'pl', 'dart'
]
const FILE_FILTERS: Electron.FileFilter[] = [
  {
    name: '文档与图片',
    extensions: ['pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', ...TEXT_EXTENSIONS]
  },
  { name: '文档', extensions: ['pdf', 'docx', 'xlsx', 'pptx'] },
  { name: '图片', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'] },
  { name: '文本与代码', extensions: TEXT_EXTENSIONS },
  { name: '所有文件', extensions: ['*'] }
]

/** 某个目录里 att_id 对应的原文件（跳过 .meta.json / .send.jpg 这类派生文件）。
 *  同一 att_id 可能同时有原件与抽取出的 .txt（文档类）→ 优先非 .txt。 */
function pickAttachmentFile(dir: string, attId: string): string | null {
  let names: string[]
  try {
    if (!statSync(dir).isDirectory()) return null
    names = readdirSync(dir)
  } catch {
    return null
  }
  const owned = names.filter(
    (n) =>
      n.startsWith(`${attId}.`) &&
      !n.endsWith(ATT_META_SUFFIX) &&
      !n.endsWith(ATT_SEND_IMAGE_SUFFIX)
  )
  if (owned.length === 0) return null
  const preferred = owned.find((n) => !n.endsWith('.txt')) ?? owned[0]
  return join(dir, preferred)
}

/**
 * 按 **att_id** 定位会话内附件副本（不是按路径）。
 *
 * 为什么按 id 而不是路径：附件的落点在发送时会从 `.attachments/_draft/<att_id>/`
 * 原子迁移到 `.attachments/<session_id>/`。若把路径写进 URL，用户刚发出去的图片
 * 缩略图会瞬间 404（迁移后旧路径不存在）。按 id 检索则跨越迁移稳定。
 *
 * 安全：`project_id` / `att_id` 都是**固定形状白名单**（不接受分隔符与 `..`），
 * 且解引符号链接后仍要求落在 `.attachments` 目录内 —— 三层校验，缺一不可。
 */
function resolveAttachmentFile(projectId: string, attId: string): string | null {
  if (!PROJECT_ID_RE.test(projectId) || !ATT_ID_RE.test(attId)) return null
  const root = join(homedir(), '.aigent', 'projects', projectId, ATTACHMENTS_DIRNAME)
  const dirs: string[] = [join(root, DRAFT_DIRNAME, attId)]
  try {
    for (const name of readdirSync(root)) {
      if (name === DRAFT_DIRNAME) continue
      dirs.push(join(root, name, attId))
    }
  } catch {
    /* 还没有任何会话目录：只试草稿区 */
  }
  for (const dir of dirs) {
    const found = pickAttachmentFile(dir, attId)
    if (!found) continue
    let real: string
    try {
      real = realpathSync(found)
    } catch {
      continue
    }
    if (!real.split(sep).includes(ATTACHMENTS_DIRNAME)) continue
    try {
      if (!statSync(real).isFile()) continue
    } catch {
      continue
    }
    return real
  }
  return null
}

/** 清理剪贴板临时文件（启动时 + 每次读取前节流调用；中转文件不该长期留着） */
function sweepClipboardTemp(): void {
  try {
    if (!existsSync(CLIP_TMP_DIR)) return
    const cutoff = Date.now() - CLIP_TMP_TTL_MS
    for (const name of readdirSync(CLIP_TMP_DIR)) {
      const p = join(CLIP_TMP_DIR, name)
      try {
        if (statSync(p).mtimeMs < cutoff) unlinkSync(p)
      } catch {
        /* 单个失败不影响其余 */
      }
    }
  } catch (err) {
    flog.warn('attachments', `剪贴板临时目录清理失败: ${String(err)}`)
  }
}

// scheme 必须在 app ready **之前**声明为 privileged：否则渲染层的 <img src="aigent-att://…">
// 会被当作不认识的 scheme 直接拦掉（standard=true 才能被 new URL 正常解析）。
protocol.registerSchemesAsPrivileged([
  {
    scheme: ATT_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true }
  }
])

// 应用名称：macOS Dock / 菜单栏 / Cmd+Tab 等所有系统展示处统一命名为「个人AI助手」
app.setName('个人AI助手')

// 应用图标：透明背景的浅蓝机器人头像（dev 下位于工程根 build/icon.png；打包后位于安装资源目录）
const APP_ICON = join(app.getAppPath(), 'build/icon.png')

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
    height: 900,
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

  ipcMain.handle('agent:send', (e, payload: { text?: string; session_id?: string | null; project_id?: string | null; overrides?: { thinking_strength?: string; max_context?: string } | null; model_id?: string | null; attachments?: unknown[] | null }) => {
    // ⚠️ 不能只判 text：**纯附件消息（正文为空）是合法发送**。
    // 历史 bug 就是这里把"只发了图片没打字"的消息静默丢掉。
    if (!isTrustedSender(e) || (!payload?.text && !payload?.attachments?.length)) return
    // session_id 缺省/null = 新建任务（后端惰性生成短 id 建会话）；否则定位到目标会话
    const sessionId = typeof payload.session_id === 'string' && payload.session_id ? payload.session_id : undefined
    // project_id：新建任务的归属工作空间（已有会话由后端按 session_id 解析归属）
    const projectId = typeof payload.project_id === 'string' && payload.project_id ? payload.project_id : undefined
    ws.send(JSON.stringify({
      kind: 'chat',
      payload: {
        text: payload.text ?? '',
        ...(sessionId !== undefined ? { session_id: sessionId } : {}),
        ...(projectId !== undefined ? { project_id: projectId } : {}),
        ...(payload.overrides ? { overrides: payload.overrides } : {}),
        ...(payload.model_id ? { model_id: payload.model_id } : {}),
        ...(payload.attachments?.length ? { attachments: payload.attachments } : {})
      }
    }))
  })

  ipcMain.handle('agent:setSessionModel', (e, payload: { session_id?: string; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }) => {
    if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id) return
    ws.send(JSON.stringify({
      kind: 'session_model',
      payload: {
        session_id: payload.session_id,
        ...(payload.model_id ? { model_id: payload.model_id } : {}),
        ...(payload.overrides ? { overrides: payload.overrides } : {})
      }
    }))
  })

  ipcMain.handle('agent:stop', (e, payload: { session_id?: string }) => {
    if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id) return
    ws.send(JSON.stringify({ kind: 'stop', payload: { session_id: payload.session_id } }))
  })

  ipcMain.handle('agent:switchSession', (e, payload: { session_id?: string }) => {
    if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id) return
    ws.send(JSON.stringify({ kind: 'session_switch', payload: { session_id: payload.session_id } }))
    return { ok: true }
  })
  ipcMain.handle('agent:clearSession', (e) => {
    if (!isTrustedSender(e)) return
    ws.send(JSON.stringify({ kind: 'session_clear' }))
    return { ok: true }
  })

  ipcMain.handle('agent:setSessionUnread', (e, payload: { session_id?: string; unread?: boolean }) => {
    if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id) return null
    return request('session_set_unread', 'sessions', { session_id: payload.session_id, unread: Boolean(payload.unread) })
  })

  ipcMain.handle('agent:listSessions', (e) =>
    isTrustedSender(e) ? request('sessions_list') : null
  )

  // ── 工作空间（多项目）──────────────────────────────────────────────
  // 目录选择与"在 Finder 中打开"属于**宿主能力**（原生对话框 / 文件管理器），
  // 只能由主进程提供：渲染层拿到路径后再发 project_add 给后端登记。
  ipcMain.handle('agent:pickFolder', async (e) => {
    if (!isTrustedSender(e) || !mainWindow) return null
    const r = await dialog.showOpenDialog(mainWindow, {
      title: '选择工作空间目录',
      // createDirectory：macOS 上允许在对话框里新建文件夹（选到的新目录也能直接用）
      properties: ['openDirectory', 'createDirectory'],
      buttonLabel: '选择'
    })
    if (r.canceled || !r.filePaths.length) return null
    return r.filePaths[0]
  })

  ipcMain.handle('agent:openInFinder', async (e, payload: { path?: string }) => {
    if (!isTrustedSender(e) || typeof payload?.path !== 'string' || !payload.path) {
      return { ok: false, error: '空路径' }
    }
    // openPath 在系统文件管理器中定位；失败返回非空错误串（不抛异常）
    const err = await shell.openPath(payload.path)
    return err ? { ok: false, error: err } : { ok: true }
  })

  // ── 会话附件（「添加文件或图片」）──────────────────────────────────
  // 三类入口都收敛成"本地绝对路径列表"，由后端（同机进程）自己读盘：
  // **不经 IPC/WS 传文件字节**（websockets 默认帧上限 1 MiB，base64 图片必然超限）。
  ipcMain.handle('agent:pickFiles', async (e) => {
    if (!isTrustedSender(e) || !mainWindow) return []
    const r = await dialog.showOpenDialog(mainWindow, {
      title: '添加文件或图片',
      // multiSelections：一次可多选；filters 只是建议（拖拽可绕过），真守卫在后端
      properties: ['openFile', 'multiSelections'],
      filters: FILE_FILTERS,
      buttonLabel: '添加'
    })
    if (r.canceled) return []
    return r.filePaths
  })

  ipcMain.handle('agent:saveClipboardImage', (e, payload: { bytes?: ArrayBuffer | Uint8Array; mime?: string }) => {
    if (!isTrustedSender(e) || !payload?.bytes) return null
    try {
      const buf = Buffer.from(
        payload.bytes instanceof Uint8Array ? payload.bytes : new Uint8Array(payload.bytes)
      )
      if (buf.length === 0 || buf.length > CLIP_MAX_BYTES) {
        flog.warn('attachments', `剪贴板图片体积不合法：${buf.length} bytes`)
        return null
      }
      // 扩展名按 MIME 定（截图基本是 png）；后端按扩展名分类，写错会导致"不支持的类型"
      const ext = EXT_BY_MIME[String(payload.mime ?? '').toLowerCase()] ?? 'png'
      mkdirSync(CLIP_TMP_DIR, { recursive: true })
      const file = join(CLIP_TMP_DIR, `clip-${Date.now()}-${randomUUID().slice(0, 8)}.${ext}`)
      writeFileSync(file, buf)
      flog.info('attachments', `剪贴板图片已落盘: ${file} (${buf.length} bytes)`)
      return file
    } catch (err) {
      flog.warn('attachments', `剪贴板图片落盘失败: ${String(err)}`)
      return null
    }
  })

  ipcMain.handle('agent:stageAttachments', (e, payload: { paths?: string[]; projectId?: string | null }) => {
    if (!isTrustedSender(e) || !Array.isArray(payload?.paths) || payload.paths.length === 0) return null
    const projectId = typeof payload.projectId === 'string' && payload.projectId ? payload.projectId : undefined
    // 与其它 request/response 命令同管道：后端回 `attachments_staged`，由 resolvePending 兑现
    return request(
      'attachment_stage',
      'attachments_staged',
      { paths: payload.paths, ...(projectId ? { project_id: projectId } : {}) },
      20000
    )
  })

  ipcMain.handle('agent:listProjects', (e) =>
    isTrustedSender(e) ? request('projects_list', 'projects') : null
  )
  ipcMain.handle('agent:addProject', (e, payload: { path?: string }) =>
    isTrustedSender(e) && typeof payload?.path === 'string' && payload.path
      ? request('project_add', 'projects', { path: payload.path })
      : null
  )
  ipcMain.handle('agent:openProject', (e, payload: { project_id?: string }) => {
    if (!isTrustedSender(e) || typeof payload?.project_id !== 'string' || !payload.project_id) return
    ws.send(JSON.stringify({ kind: 'project_open', payload: { project_id: payload.project_id } }))
  })
  ipcMain.handle('agent:renameProject', (e, payload: { project_id?: string; name?: string }) =>
    isTrustedSender(e) && typeof payload?.project_id === 'string' && payload.project_id
      ? request('project_rename', 'projects', {
          project_id: payload.project_id,
          name: payload.name ?? ''
        })
      : null
  )
  ipcMain.handle('agent:removeProject', (e, payload: { project_id?: string }) =>
    isTrustedSender(e) && typeof payload?.project_id === 'string' && payload.project_id
      ? request('project_remove', 'projects', { project_id: payload.project_id })
      : null
  )
  // 会话管理：重命名 / 软删除（回收站）/ 还原 / 批量永久删除 / 回收站列表
  ipcMain.handle(
    'agent:renameSession',
    (e, payload: { session_id?: string; title?: string }) => {
      if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id || !payload?.title) return null
      return request('session_rename', 'sessions', { session_id: payload.session_id, title: payload.title })
    }
  )
  ipcMain.handle('agent:trashSession', (e, payload: { session_id?: string }) => {
    if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id) return null
    return request('session_trash', 'sessions', { session_id: payload.session_id })
  })
  ipcMain.handle('agent:restoreSession', (e, payload: { session_id?: string }) => {
    if (!isTrustedSender(e) || typeof payload?.session_id !== 'string' || !payload.session_id) return null
    return request('session_restore', 'sessions', { session_id: payload.session_id })
  })
  ipcMain.handle('agent:deleteSessions', (e, payload: { ids?: string[] }) => {
    if (!isTrustedSender(e) || !Array.isArray(payload?.ids) || payload.ids.length === 0) return null
    return request('session_delete', 'session_delete_result', { ids: payload.ids })
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
  // 附件缩略图协议：渲染层 <img src="aigent-att://local/?pid=<空间>&id=<att_id>"> 由此兑现。
  // 按 att_id 检索（不是按路径）→ 草稿区 → 会话目录的迁移不会让缩略图失效。
  // 渲染层拿不到 file://（contextIsolation + CSP），这是图片显示的唯一通路。
  protocol.handle(ATT_SCHEME, (req) => {
    let pid = ''
    let attId = ''
    try {
      const u = new URL(req.url)
      pid = u.searchParams.get('pid') ?? ''
      attId = u.searchParams.get('id') ?? ''
    } catch {
      /* 非法 URL → 走下面的 404 */
    }
    const real = resolveAttachmentFile(pid, attId)
    if (!real) return new Response('not found', { status: 404 })
    return net.fetch(pathToFileURL(real).toString())
  })
  sweepClipboardTemp()
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