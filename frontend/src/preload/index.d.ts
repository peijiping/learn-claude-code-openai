export interface AgentApi {
  send: (text: string, sessionId?: string | null, overrides?: { thinking_strength?: string; max_context?: string } | null, modelId?: string | null, projectId?: string | null) => Promise<void>
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