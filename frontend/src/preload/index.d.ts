export interface AgentApi {
  send: (text: string, num?: number | null, overrides?: { thinking_strength?: string; max_context?: string } | null, modelId?: string | null) => Promise<void>
  setSessionModel: (payload: { num?: number | null; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }) => Promise<void>
  stop: (num: number) => Promise<void>
  switchSession: (num: number) => Promise<{ num: number; message_count: number }>
  clearSession: () => Promise<{ deleted: number }>
  listSessions: () => Promise<unknown[]>
  renameSession: (num: number, title: string) => Promise<unknown>
  trashSession: (num: number) => Promise<unknown>
  restoreSession: (num: number) => Promise<unknown>
  deleteSessions: (nums: number[]) => Promise<unknown>
  listTrash: () => Promise<unknown[]>
  goalStatus: () => Promise<string>
  tasks: () => Promise<string>
  skills: () => Promise<string>
  getConnectionStatus: () => Promise<string>
  llmConfigGet: () => Promise<unknown>
  llmConfigSave: (config: unknown) => Promise<unknown>
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