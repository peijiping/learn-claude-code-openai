/** chat 携带的附件线索（结构与 renderer 侧 ChatAttachmentInput 一致；
 *  这里内联声明而非 import —— preload 属 node 侧 tsconfig，不跨到 renderer 取类型） */
export interface ChatAttachmentInput {
  att_id: string
  kind: '' | 'image' | 'document' | 'text'
  name: string
  mime?: string
  ext: string
  size?: number
  project_id?: string
}

export interface AgentApi {
  /** 发起一次对话。
   *  attachments=本轮附件（`attachment_stage` 登记得到的 att_id 列表）；
   *  **只有附件没有正文时 text 传空串**（后端据此只插附件块，不插空文本块）。 */
  send: (text: string, sessionId?: string | null, overrides?: { thinking_strength?: string; max_context?: string } | null, modelId?: string | null, projectId?: string | null, attachments?: ChatAttachmentInput[] | null) => Promise<void>
  setSessionModel: (payload: { session_id?: string | null; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }) => Promise<void>
  stop: (sessionId: string) => Promise<void>
  switchSession: (sessionId: string) => Promise<{ session_id: string; message_count: number }>
  clearSession: () => Promise<{ deleted: number }>
  listSessions: () => Promise<unknown[]>
  /** 标记会话未读/已读（进入会话=已读，后端写入元数据持久化） */
  setSessionUnread: (payload: { session_id?: string; unread?: boolean }) => Promise<unknown>
  renameSession: (sessionId: string, title: string) => Promise<unknown>
  trashSession: (sessionId: string) => Promise<unknown>
  restoreSession: (sessionId: string) => Promise<unknown>
  deleteSessions: (ids: string[]) => Promise<unknown>
  listTrash: () => Promise<unknown[]>
  /** 弹原生目录选择框（工作空间新增）；取消返回 null */
  pickFolder: () => Promise<string | null>
  /** ── 会话附件（「添加文件或图片」）───────────────────────────────────
   *  三类入口最终都收敛成"本地绝对路径"，由后端同机读取 —— **不过 WS 传字节**
   *  （websockets 默认帧上限 1 MiB，base64 内联图片必然超限）。 */
  /** 弹原生文件选择框（多选 + 类型白名单）；取消返回空数组 */
  pickFiles: () => Promise<string[]>
  /** 拖拽取路径：Electron 32+ 已移除 `File.path`，只此一法。
   *  ⚠️ 必须在渲染层（preload）内调用 —— DOM `File` 无法通过 IPC 传给主进程。 */
  getPathForFile: (file: File) => string
  /** 读系统剪贴板里的图片并落成临时文件，返回该文件路径；无图片/失败返回 null。
   *  截图没有磁盘路径，这是「粘贴截图」的唯一通路（Electron 44 主进程 Clipboard
   *  不再提供 readImage，故由渲染层把粘贴事件的字节交过来）。 */
  saveClipboardImage: (payload: { bytes: ArrayBuffer | Uint8Array; mime?: string }) => Promise<string | null>
  /** 把一批本地路径登记为草稿附件（后端复制 + 解析），结果经
   *  `attachments_staged` 信封异步回来 */
  stageAttachments: (payload: { paths: string[]; projectId?: string | null }) => Promise<unknown>
  /** 在系统文件管理器中定位目录（工作空间右键菜单） */
  openInFinder: (path: string) => Promise<{ ok: boolean; error?: string }>
  /** 工作空间列表（主要数据源是 `projects` 广播信封，这里是主动拉取的兜底） */
  listProjects: () => Promise<unknown>
  /** 把选定目录登记为工作空间（已登记则复用；后端同时置为活动空间） */
  addProject: (path: string) => Promise<unknown>
  /** 切换活动工作空间 */
  openProject: (projectId: string) => Promise<void>
  renameProject: (projectId: string, name: string) => Promise<unknown>
  /** 删除工作空间（只删元数据目录；调用方需先二次确认） */
  removeProject: (projectId: string) => Promise<unknown>
  goalStatus: () => Promise<string>
  tasks: () => Promise<string>
  skills: () => Promise<string>
  getConnectionStatus: () => Promise<string>
  /** 拉取后端仍在运行会话的执行状态（重放 session_status），恢复前端运行指示 */
  queryStatus: () => Promise<{ ok: boolean }>
  llmConfigGet: () => Promise<unknown>
  llmConfigSave: (config: unknown) => Promise<unknown>
  /** 刷新某连接可用模型列表（GET {base_url}/models；api_key 留空回退已保存密钥） */
  llmModelsFetch: (payload: {
    base_url?: string
    api_key?: string
    connection_id?: string
    api_format?: string
    models_path?: string
  }) => Promise<unknown>
  onEvent: (cb: (e: unknown) => void) => () => void
  onStatus: (cb: (status: string) => void) => () => void
  onPythonStatus: (cb: (status: string) => void) => () => void
}

declare global {
  interface Window {
    agent: AgentApi
  }
}

export {}