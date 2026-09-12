import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { installBrowserAgent } from './lib/browserAgent'
import './styles/tokens.css'
import './styles/global.css'
import './styles/layout.css'
import './styles/sidebar.css'
import './styles/chat.css'
import './styles/settings.css'

// 纯浏览器预览（无 Electron preload）时安装直连 WS 的回退桥；Electron 内自动跳过
installBrowserAgent()

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)