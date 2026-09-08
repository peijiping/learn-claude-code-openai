import { contextBridge, ipcRenderer } from 'electron'

/**
 * preload - 渲染进程与主进程之间唯一的"合规通道"。
 * 只暴露白名单 API（不透出原始 ipcRenderer），contextIsolation 开启下安全。
 */
const agent = {
  /** 发起一次对话；fresh=true 表示当前无激活会话（新建任务后的首条消息），后端据此惰性建会话 */
  send: (text: string, fresh = false): Promise<void> =>
    ipcRenderer.invoke('agent:send', { text, fresh }),

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

  /** 大模型配置（llmconfig.json）：读 / 保存（保存即热切换生效） */
  llmConfigGet: (): Promise<unknown> => ipcRenderer.invoke('agent:llmConfigGet'),
  llmConfigSave: (config: unknown): Promise<unknown> =>
    ipcRenderer.invoke('agent:llmConfigSave', { config }),

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