// 仅用于调试复现：单独跑渲染层（浏览器模式），直连本机 ws_bridge。
// 不影响 electron.vite.config.ts 的正常构建。
import { resolve } from 'path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// dev 调试去掉 index.html 的 CSP，允许 ws://127.0.0.1:<任意端口>（与 electron.vite.config.ts 同理）
const stripCspInDev = (): Plugin => ({
  name: 'strip-csp-in-dev',
  apply: 'serve',
  transformIndexHtml(html: string) {
    return html.replace(/\s*<meta[^>]+http-equiv="Content-Security-Policy"[^>]*\/>/, '')
  }
})

export default defineConfig({
  root: resolve(__dirname, 'src/renderer'),
  resolve: {
    alias: {
      '@renderer': resolve(__dirname, 'src/renderer/src'),
      '@protocols': resolve(__dirname, 'src/renderer/src/protocols'),
      '@components': resolve(__dirname, 'src/renderer/src/components'),
      '@store': resolve(__dirname, 'src/renderer/src/store'),
      '@hooks': resolve(__dirname, 'src/renderer/src/hooks')
    }
  },
  plugins: [react(), stripCspInDev()],
  server: { port: 5183, strictPort: true }
})
