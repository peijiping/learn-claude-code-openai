import { useEffect } from 'react'
import { useAgentStream } from '@hooks/useAgentStream'
import { useAgentStore } from '@store/agentStore'
import Sidebar from '@components/Sidebar/Sidebar'
import ChatPanel from '@components/Chat/ChatPanel'
import RightPanel from '@components/RightPanel/RightPanel'
import TitleBar from '@components/TitleBar/TitleBar'
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
        {/* 自绘窗口标题栏（`.titlebar`）：**在 `.app-body` 之外、之上一层** ——
            它是窗口的属性（应用名 + 右栏开关），不属于任何一栏。原生标题栏
            已在主进程隐去（`titleBarStyle: 'hidden'`），标题由本组件渲染。 */}
        <TitleBar />
        <div className="app-body">
          <Sidebar />
          <ChatPanel />
          {/* 右侧面板：自带按会话隔离（`activeSession === null` / 该会话右栏关着时
              自行返回 null），所以这里无条件挂载即可 —— 它也需要"始终在树上"才能
              在收起时仍持有内存桶（切回来标签栏才能无闪动恢复）。 */}
          <RightPanel />
        </div>
        <StatusBar />
        {settingsOpen && <SettingsModal />}
        <Toast />
        <StartupOverlay />
      </div>
    </ErrorBoundary>
  )
}