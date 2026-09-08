export interface AgentApi {
  send: (text: string, fresh?: boolean) => Promise<void>
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