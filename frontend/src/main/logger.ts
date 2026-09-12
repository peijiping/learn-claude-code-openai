import { appendFile, mkdir } from 'fs/promises'
import { homedir } from 'os'
import path from 'path'

/**
 * logger - Electron 主进程统一日志（与 Python 后端共用 ~/.aigent/logs 目录）。
 *
 * - 按日期落盘：~/.aigent/logs/frontend_YYYY-MM-DD.log，跨天自动切换新文件；
 * - 只写文件不抢 console：console.log 已承担终端观察职责；
 * - 异步 append、失败静默：日志绝不能反向打断主进程逻辑。
 *
 * 用法：flog.info('python', '后端启动 pid=123')
 */

const LOG_DIR = path.join(homedir(), '.aigent', 'logs')

function pad(n: number): string {
  return n < 10 ? `0${n}` : `${n}`
}

/** 本地时间 YYYY-MM-DD HH:mm:ss */
function timestamp(): string {
  const d = new Date()
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

function write(level: string, tag: string, msg: string): void {
  const today = new Date()
  const date = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`
  const file = path.join(LOG_DIR, `frontend_${date}.log`)
  const line = `${timestamp()} [${level}] [${tag}] ${msg}\n`
  mkdir(LOG_DIR, { recursive: true })
    .then(() => appendFile(file, line, 'utf8'))
    .catch(() => {
      /* 日志失败静默，不打断业务 */
    })
}

export const flog = {
  info: (tag: string, msg: string): void => write('INFO', tag, msg),
  warn: (tag: string, msg: string): void => write('WARN', tag, msg),
  error: (tag: string, msg: string): void => write('ERROR', tag, msg)
}
