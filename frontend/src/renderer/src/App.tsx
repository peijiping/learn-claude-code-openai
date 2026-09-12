import { useEffect } from 'react'
import { useAgentStream } from '@hooks/useAgentStream'
import { useAgentStore } from '@store/agentStore'
import Sidebar from '@components/Sidebar/Sidebar'
import ChatPanel from '@components/Chat/ChatPanel'
import SettingsModal from '@components/SettingsModal'
import StatusBar from '@components/StatusBar'
import ErrorBoundary from '@components/common/ErrorBoundary'
import Toast from '@components/common/Toast'
import StartupOverlay from '@components/common/StartupOverlay'

export default function App(): JSX.Element {
  useAgentStream()

  const settingsOpen = useAgentStore((s) => s.settingsOpen)
  const connection = useAgentStore((s) => s.connection)

  // 后端就绪后拉取一次大模型配置，保证对话区模型下拉与已存配置实时同步
  useEffect(() => {
    if (connection === 'connected') void useAgentStore.getState().loadLlConfig()
  }, [connection])

  return (
    <ErrorBoundary>
      <div className="app">
        <div className="app-body">
          <Sidebar />
          <ChatPanel />
        </div>
        <StatusBar />
        {settingsOpen && <SettingsModal />}
        <Toast />
        <StartupOverlay />
      </div>
    </ErrorBoundary>
  )
}