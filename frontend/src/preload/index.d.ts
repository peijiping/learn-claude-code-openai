export interface AgentApi {
  send: (text: string, sessionId?: string | null, overrides?: { thinking_strength?: string; max_context?: string } | null, modelId?: string | null) => Promise<void>
  setSessionModel: (payload: { session_id?: string | null; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }) => Promise<void>
  stop: (sessionId: string) => Promise<void>
  switchSession: (sessionId: string) => Promise<{ session_id: string; message_count: number }>
  clearSession: () => Promise<{ deleted: number }>
  listSessions: () => Promise<unknown[]>
  renameSession: (sessionId: string, title: string) => Promise<unknown>
  trashSession: (sessionId: string) => Promise<unknown>
  restoreSession: (sessionId: string) => Promise<unknown>
  deleteSessions: (ids: string[]) => Promise<unknown>
  listTrash: () => Promise<unknown[]>
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