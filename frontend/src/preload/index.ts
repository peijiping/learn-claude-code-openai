import { contextBridge, ipcRenderer, webUtils } from 'electron'

/** chat 携带的附件线索（与 renderer 侧 ChatAttachmentInput 同构） */
interface ChatAttachmentInput {
  att_id: string
  kind: '' | 'image' | 'document' | 'text'
  name: string
  mime?: string
  ext: string
  size?: number
  project_id?: string
}

/** chat 携带的引用线索（与 renderer 侧 RefInput 同构）。**只有路径**，不含内容。 */
interface RefInput {
  path: string
  name?: string
  is_dir?: boolean
}

/**
 * preload - 渲染进程与主进程之间唯一的"合规通道"。
 * 只暴露白名单 API（不透出原始 ipcRenderer），contextIsolation 开启下安全。
 */
const agent = {
  /** 发起一次对话；sessionId=目标会话 id（新建任务时传 null/缺省，后端惰性生成短 id 建会话）。
   * overrides=当前会话请求级覆盖（思考强度/更大上下文），随本轮请求带上。
   * modelId=当前会话绑定模型，新建任务随首条消息持久化。
   * projectId=新建任务的归属工作空间 id（缺省 = 后端当前活动空间）。
   * attachments=本轮附件（附件登记得到的 att_id 列表）；**只有附件无正文时 text 传空串**。
   * refs=本轮引用的工作空间路径（**零复制**：只传路径，后端校验越界后挂中性引用块）；
   * **只有引用无正文同样是合法发送**。 */
  send: (text: string, sessionId?: string | null, overrides?: { thinking_strength?: string; max_context?: string } | null, modelId?: string | null, projectId?: string | null, attachments?: ChatAttachmentInput[] | null, refs?: RefInput[] | null): Promise<void> =>
    ipcRenderer.invoke('agent:send', { text, session_id: sessionId, project_id: projectId, ...(overrides ? { overrides } : {}), ...(modelId ? { model_id: modelId } : {}), ...(attachments?.length ? { attachments } : {}), ...(refs?.length ? { refs } : {}) }),

  /** 记录/更新某会话选择的模型与参数到后端元数据（无需等待下一条消息）。
   * overrides 为按模型 id 的 UI 档位 map：{ [modelId]: { thinking_strength?, max_context_option? } } */
  setSessionModel: (payload: { session_id?: string | null; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }): Promise<void> =>
    ipcRenderer.invoke('agent:setSessionModel', payload),

  /** 停止指定会话正在执行的那一轮（其它后台会话不受影响） */
  stop: (sessionId: string): Promise<void> => ipcRenderer.invoke('agent:stop', { session_id: sessionId }),

  /** ── 结构化提问作答（ask_user，2026-09-21）─────────────────────────
   * 模型用 `ask_user` 工具提问并阻塞等待；这里提交答案 / 取消。
   * **fire-and-forget**：回执走 `ask_resolved` 广播（渲染层从 onEvent 收），
   * 这两个 invoke 的返回值无意义、也不该等 —— 别改成 request()。 */
  answerAsk: (sessionId: string, requestId: string, answers: unknown[]): Promise<void> =>
    ipcRenderer.invoke('agent:answerAsk', { session_id: sessionId, request_id: requestId, answers }),
  cancelAsk: (sessionId: string, requestId: string): Promise<void> =>
    ipcRenderer.invoke('agent:cancelAsk', { session_id: sessionId, request_id: requestId }),

  /** ── 权限管控（2026-09-22，docs/frontend/17）────────────────────────
   * 审批作答 / 切换会话权限档位。与 ask_answer 同款 **fire-and-forget**：
   * 回执分别走 `approval_resolved` / `permission_changed` 广播，
   * 这两个 invoke 的返回值无意义、也不该等 —— 别改成 request()。 */
  approvalAnswer: (sessionId: string, requestId: string, decision: string): Promise<void> =>
    ipcRenderer.invoke('agent:approvalAnswer', { session_id: sessionId, request_id: requestId, decision }),
  sessionPermission: (sessionId: string, mode: string): Promise<void> =>
    ipcRenderer.invoke('agent:sessionPermission', { session_id: sessionId, mode }),
  projectPermission: (projectId: string, mode: string): Promise<void> =>
    ipcRenderer.invoke('agent:projectPermission', { project_id: projectId, mode }),

  /** 会话操作（新建任务是纯前端行为：store 清空消息并把 activeSession 置 null，不走 IPC） */
  switchSession: (sessionId: string): Promise<{ session_id: string; message_count: number }> =>
    ipcRenderer.invoke('agent:switchSession', { session_id: sessionId }),
  clearSession: (): Promise<{ deleted: number }> =>
    ipcRenderer.invoke('agent:clearSession'),
  listSessions: (): Promise<unknown[]> => ipcRenderer.invoke('agent:listSessions'),
  /** 标记会话未读/已读（进入会话=已读，后端写入元数据持久化） */
  setSessionUnread: (payload: { session_id?: string; unread?: boolean }): Promise<unknown> =>
    ipcRenderer.invoke('agent:setSessionUnread', payload),

  /** 会话管理：重命名 / 软删除（回收站）/ 还原 / 批量永久删除 / 回收站列表 */
  renameSession: (sessionId: string, title: string): Promise<unknown> =>
    ipcRenderer.invoke('agent:renameSession', { session_id: sessionId, title }),
  trashSession: (sessionId: string): Promise<unknown> =>
    ipcRenderer.invoke('agent:trashSession', { session_id: sessionId }),
  restoreSession: (sessionId: string): Promise<unknown> =>
    ipcRenderer.invoke('agent:restoreSession', { session_id: sessionId }),
  deleteSessions: (ids: string[]): Promise<unknown> =>
    ipcRenderer.invoke('agent:deleteSessions', { ids }),
  listTrash: (): Promise<unknown[]> => ipcRenderer.invoke('agent:listTrash'),

  /** ── 工作空间（多项目）─────────────────────────────────────────────
   * 目录选择与"在 Finder 中打开"是宿主能力，必须走主进程原生对话框/文件管理器。 */
  /** 弹原生目录选择框；用户取消返回 null */
  pickFolder: (): Promise<string | null> => ipcRenderer.invoke('agent:pickFolder'),

  /** ── 会话附件（「添加文件或图片」，2026-09-20）─────────────────────
   * 三类入口（原生对话框 / 拖拽 / 粘贴）最终都收敛成"本地绝对路径列表"，交给
   * 后端（同机进程）自己读盘：**不经 IPC/WS 传文件字节**。
   * 设计见 docs/frontend/12-附件与文件输入.md。 */
  /** 弹原生文件选择框（多选 + 类型白名单）；取消返回空数组 */
  pickFiles: (): Promise<string[]> => ipcRenderer.invoke('agent:pickFiles'),
  /** 拖拽取路径。Electron 32+ 移除了 `File.path`，`webUtils.getPathForFile` 是唯一
   * 途径，且**必须在渲染层调用**（DOM `File` 不能通过 IPC 序列化给主进程）。
   * 截图/剪贴板图片没有磁盘路径 → 返回空串，调用方改走 readClipboardImage。 */
  getPathForFile: (file: File): string => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  /** 把剪贴板图片的字节落成临时文件，返回该文件路径；失败返回 null。
   *  截图没有磁盘路径，且 Electron 44 的主进程 Clipboard 已改为 W3C 风格异步 API
   *  （不再提供 readImage）→ 由渲染层从粘贴事件取到 File 后把**字节**交过来。
   *  ArrayBuffer/Uint8Array 都是 IPC 可结构化克隆的类型，不受"File 不能过 IPC"限制。 */
  saveClipboardImage: (payload: { bytes: ArrayBuffer | Uint8Array; mime?: string }): Promise<string | null> =>
    ipcRenderer.invoke('agent:saveClipboardImage', payload),
  /** 把一批本地路径登记为草稿附件（后端复制 + 解析）；结果经 `attachments_staged`
   *  信封异步回来，渲染层按 source_path 与本地占位项配对 */
  stageAttachments: (payload: { paths: string[]; projectId?: string | null }): Promise<unknown> =>
    ipcRenderer.invoke('agent:stageAttachments', payload),
  /** 在系统文件管理器中定位该目录 */
  openInFinder: (path: string): Promise<{ ok: boolean; error?: string }> =>
    ipcRenderer.invoke('agent:openInFinder', { path }),

  /** ── 引用文件或文件夹（@-mention，2026-09-21）─────────────────────
   * 与附件**完全独立**的一条通道：**不复制、不存储**，只把工作空间内的路径清单
   * 交给模型，内容由模型自己用 run_read 按需读取。
   * 设计见 docs/frontend/13-引用文件与文件夹（@-mention）.md。 */
  /** 拉取当前工作空间的可引用条目（**扁平、一次全量**；打开 `@` 时拉一次，
   *  之后按键在前端本地过滤）。返回 `RefsPayload`；主进程等待超时返回 null。 */
  listRefs: (payload?: { projectId?: string | null; sessionId?: string | null }): Promise<unknown> =>
    ipcRenderer.invoke('agent:listRefs', payload ?? {}),
  /** 工作空间列表（`projects` 信封为主要数据源，这里是主动拉取的兜底） */
  listProjects: (): Promise<unknown> => ipcRenderer.invoke('agent:listProjects'),
  /** 把选定目录登记为工作空间（已登记过则复用；后端同时把它设为活动空间） */
  addProject: (path: string): Promise<unknown> => ipcRenderer.invoke('agent:addProject', { path }),
  /** 切换活动工作空间（后端持久化，广播 projects） */
  openProject: (projectId: string): Promise<void> => ipcRenderer.invoke('agent:openProject', { project_id: projectId }),
  /** 重命名（默认空间后端会拒绝并回 error 信封） */
  renameProject: (projectId: string, name: string): Promise<unknown> =>
    ipcRenderer.invoke('agent:renameProject', { project_id: projectId, name }),
  /** 删除工作空间（只删元数据目录，真实目录保留；不可恢复，调用方必须先确认） */
  removeProject: (projectId: string): Promise<unknown> =>
    ipcRenderer.invoke('agent:removeProject', { project_id: projectId }),

  /** 状态类查询 */
  goalStatus: (): Promise<string> => ipcRenderer.invoke('agent:goalStatus'),
  tasks: (): Promise<string> => ipcRenderer.invoke('agent:tasks'),
  skills: (): Promise<string> => ipcRenderer.invoke('agent:skills'),
  getConnectionStatus: (): Promise<string> => ipcRenderer.invoke('agent:connectionStatus'),
  /** 拉取后端仍在运行会话的执行状态（重放 session_status），恢复前端运行指示 */
  queryStatus: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('agent:queryStatus'),

  /** 大模型配置（llmconfig.json）：读 / 保存（保存即热切换生效） */
  llmConfigGet: (): Promise<unknown> => ipcRenderer.invoke('agent:llmConfigGet'),
  llmConfigSave: (config: unknown): Promise<unknown> =>
    ipcRenderer.invoke('agent:llmConfigSave', { config }),

  /** 权限配置（~/.aigent/config/permissions.json）：读 / 保存。
   *  保存后判定侧即时生效（无需重启后端），额外目录会推给在途会话。 */
  permissionConfigGet: (): Promise<unknown> => ipcRenderer.invoke('agent:permissionConfigGet'),
  permissionConfigSave: (config: unknown): Promise<unknown> =>
    ipcRenderer.invoke('agent:permissionConfigSave', { config }),

  /** 刷新某连接的可用模型列表（GET {base_url}/models）；api_key 留空时后端回退已保存密钥 */
  llmModelsFetch: (payload: {
    base_url?: string
    api_key?: string
    connection_id?: string
    api_format?: string
    models_path?: string
  }): Promise<unknown> => ipcRenderer.invoke('agent:llmModelsFetch', payload),

  /** 订阅后端事件与连接状态变化（返回取消订阅函数） */
  onEvent: (cb: (e: unknown) => void): (() => void) => {
    const listener = (_: Electron.IpcRendererEvent, data: unknown): void => cb(data)
    ipcRenderer.on('agent:event', listener)
    return () => ipcRenderer.removeListener('agent:event', listener)
  },
  onStatus: (cb: (status: string) => void): (() => void) => {
    const listener = (_: Electron.IpcRendererEvent, status: string): void => cb(status)
    ipcRenderer.on('agent:status', listener)
    return () => ipcRenderer.removeListener('agent:status', listener)
  },
  onPythonStatus: (cb: (status: string) => void): (() => void) => {
    const listener = (_: Electron.IpcRendererEvent, status: string): void => cb(status)
    ipcRenderer.on('python:status', listener)
    return () => ipcRenderer.removeListener('python:status', listener)
  }
}

contextBridge.exposeInMainWorld('agent', agent)

export type AgentApi = typeof agent