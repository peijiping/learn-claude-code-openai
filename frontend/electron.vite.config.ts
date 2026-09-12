import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import { type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// dev 模式下移除 CSP：Vite dev 依赖内联脚本（react-refresh preamble），
// 与 index.html 的 CSP script-src 'self' 冲突会导致白屏；生产构建保留 CSP。
const stripCspInDev = (): Plugin => ({
  name: 'strip-csp-in-dev',
  apply: 'serve',
  transformIndexHtml(html: string) {
    return html.replace(/\s*<meta[^>]+http-equiv="Content-Security-Policy"[^>]*\/>/, '')
  }
})

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()]
  },
  preload: {
    plugins: [externalizeDepsPlugin()]
  },
  renderer: {
    resolve: {
      alias: {
        '@renderer': resolve('src/renderer/src'),
        '@protocols': resolve('src/renderer/src/protocols'),
        '@components': resolve('src/renderer/src/components'),
        '@store': resolve('src/renderer/src/store'),
        '@hooks': resolve('src/renderer/src/hooks')
      }
    },
    plugins: [react(), stripCspInDev()]
  }
})