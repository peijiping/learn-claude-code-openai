import { contextBridge, ipcRenderer } from 'electron'

/**
 * preload - 渲染进程与主进程之间唯一的"合规通道"。
 * 只暴露白名单 API（不透出原始 ipcRenderer），contextIsolation 开启下安全。
 */
const agent = {
  /** 发起一次对话；num=目标会话号（新建任务时传 null/缺省，后端惰性领号建会话）。
   * overrides=当前会话请求级覆盖（思考强度/更大上下文），随本轮请求带上。
   * modelId=当前会话绑定模型，新建任务随首条消息持久化。 */
  send: (text: string, num?: number | null, overrides?: { thinking_strength?: string; max_context?: string } | null, modelId?: string | null): Promise<void> =>
    ipcRenderer.invoke('agent:send', { text, num, ...(overrides ? { overrides } : {}), ...(modelId ? { model_id: modelId } : {}) }),

  /** 记录/更新某会话选择的模型与参数到后端元数据（无需等待下一条消息）。
   * overrides 为按模型 id 的 UI 档位 map：{ [modelId]: { thinking_strength?, max_context_option? } } */
  setSessionModel: (payload: { num?: number | null; model_id?: string | null; overrides?: { [modelId: string]: { thinking_strength?: string; max_context_option?: 'standard' | 'extended' } } | null }): Promise<void> =>
    ipcRenderer.invoke('agent:setSessionModel', payload),

  /** 停止指定会话正在执行的那一轮（其它后台会话不受影响） */
  stop: (num: number): Promise<void> => ipcRenderer.invoke('agent:stop', { num }),

  /** 会话操作（新建任务是纯前端行为：store 清空消息并把 activeSession 置 null，不走 IPC） */
  switchSession: (num: number): Promise<{ num: number; message_count: number }> =>
    ipcRenderer.invoke('agent:switchSession', { num }),
  clearSession: (): Promise<{ deleted: number }> =>
    ipcRenderer.invoke('agent:clearSession'),
  listSessions: (): Promise<unknown[]> => ipcRenderer.invoke('agent:listSessions'),

  /** 会话管理：重命名 / 软删除（回收站）/ 还原 / 批量永久删除 / 回收站列表 */
  renameSession: (num: number, title: string): Promise<unknown> =>
    ipcRenderer.invoke('agent:renameSession', { num, title }),
  trashSession: (num: number): Promise<unknown> =>
    ipcRenderer.invoke('agent:trashSession', { num }),
  restoreSession: (num: number): Promise<unknown> =>
    ipcRenderer.invoke('agent:restoreSession', { num }),
  deleteSessions: (nums: number[]): Promise<unknown> =>
    ipcRenderer.invoke('agent:deleteSessions', { nums }),
  listTrash: (): Promise<unknown[]> => ipcRenderer.invoke('agent:listTrash'),

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