import { spawn, ChildProcess, execFileSync } from 'child_process'
import path from 'path'
import { flog } from './logger'

export type PythonStatus = 'starting' | 'running' | 'crashed' | 'stopped'

export interface PythonManagerOptions {
  onStatus: (status: PythonStatus) => void
  onLog?: (line: string) => void
}

/**
 * pythonManager - 拉起/监控/重启 Python 后端（ws_bridge.py）。
 * 子进程退出会被监控；调用方通过 onStatus 得知崩溃并决定是否一键重启。
 */
export class PythonManager {
  private child: ChildProcess | null = null
  private status: PythonStatus = 'stopped'
  private opts: PythonManagerOptions

  constructor(opts: PythonManagerOptions) {
    this.opts = opts
  }

  get isRunning(): boolean {
    return this.status === 'running' || this.status === 'starting'
  }

  private setStatus(s: PythonStatus): void {
    if (this.status !== s) {
      this.status = s
      this.opts.onStatus(s)
    }
  }

  private resolvePython(): string {
    // 优先用项目 venv（已安装 openai/websockets），再退回系统解释器
    const repoRoot = path.resolve(__dirname, '../../..')
    const venvCandidates = [
      path.join(repoRoot, '.venv', 'bin', 'python'),
      path.join(repoRoot, '.venv', 'bin', 'python3')
    ]
    for (const vp of venvCandidates) {
      try {
        execFileSync(vp, ['--version'], { stdio: 'ignore' })
        return vp
      } catch {
        /* 尝试下一个 */
      }
    }
    for (const cmd of ['python3', 'python']) {
      try {
        execFileSync(cmd, ['--version'], { stdio: 'ignore' })
        return cmd
      } catch {
        /* 尝试下一个 */
      }
    }
    throw new Error('未找到可用的 python3 / python 解释器')
  }

  start(): void {
    if (this.child) return
    this.setStatus('starting')

    // frontend/ 在仓库顶层，仓库根 = frontend 的两级上级向上（out/main → 仓库根）
    const repoRoot = path.resolve(__dirname, '../../..')
    const script = path.join(repoRoot, 'agents', 'ws_bridge.py')
    const port = process.env.AGENT_WS_PORT || '8765'

    let python: string
    try {
      python = this.resolvePython()
    } catch (err) {
      this.opts.onLog?.(`[python] ${(err as Error).message}`)
      this.setStatus('crashed')
      return
    }

    // -u 无缓冲：Python 输出到 pipe 默认全缓冲，不加 -u 时 "WS server listening"
    // 等 banner 会滞留缓冲区，readyProbe 永远探测不到，UI 误判后端未就绪/崩溃。
    const child = spawn(python, ['-u', script], {
      cwd: repoRoot,
      env: { ...process.env, AGENT_WS_PORT: port },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    this.child = child
    flog.info('python', `拉起后端: ${python} ${script} (port=${port}, pid=${child.pid})`)

    child.stdout?.on('data', (d: Buffer) => this.opts.onLog?.(d.toString()))
    child.stderr?.on('data', (d: Buffer) => this.opts.onLog?.(d.toString()))

    // 简单探测：桥起来后会打印 "WS server on ..."，据此标记 running。
    // 注意：stdout 已有统一的 onLog 监听器负责打印，此处只做探测，
    // 不能再调 onLog（历史 bug：双监听器各打印一次，"WS server listening"
    // 出现两行，排查时误判为拉起了两个 Python 进程）。
    const readyProbe = (d: Buffer): void => {
      if (d.toString().includes('WS server') || d.toString().includes('listening')) {
        this.setStatus('running')
        child.stdout?.removeListener('data', readyProbe)
      }
    }
    child.stdout?.on('data', readyProbe)

    child.on('exit', (code) => {
      this.child = null
      this.opts.onLog?.(`[python] 退出 code=${code}`)
      flog[code === 0 ? 'info' : 'error']('python', `后端进程退出 code=${code}`)
      this.setStatus(code === 0 ? 'stopped' : 'crashed')
    })
  }

  restart(): void {
    this.stop()
    this.start()
  }

  stop(): void {
    if (!this.child) return
    this.child.kill('SIGTERM')
    this.child = null
    this.setStatus('stopped')
  }
}