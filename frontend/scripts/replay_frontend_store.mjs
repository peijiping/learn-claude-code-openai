/**
 * 前端 store 离线回放：用后端复现脚本导出的真实事件时间线
 * （scripts/repro_timeline.json）驱动 agentStore 的纯函数
 * applyAgentEventBuffer / applySubagentEvent，检查子智能体卡片
 * 在「后台窗口」与「followup 轮」期间的状态是否正确。
 *
 * 运行：node scripts/replay_frontend_store.mjs（frontend 目录内，esbuild 来自 vite 依赖）
 */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const esbuild = require('esbuild')
const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const storeSrc = readFileSync(join(root, 'src/renderer/src/store/agentStore.ts'), 'utf8')

// ── 抽取纯函数片段（applyAgentEventBuffer → mapMsg，含子智能体分支）──
const startMark = 'function applyAgentEventBuffer'
const endMark = 'export const useAgentStore'
const frag = storeSrc.slice(storeSrc.indexOf(startMark), storeSrc.indexOf(endMark))
if (!frag.includes('applySubagentEvent') || !frag.includes('function mapMsg')) {
  throw new Error('片段抽取失败：检查 agentStore.ts 锚点')
}
const js = esbuild.transformSync(frag, { loader: 'ts', format: 'cjs' }).code
const mod = { exports: {} }
// mid / addUnique 在片段之外定义，这里补桩（消息 id 生成器）
const prelude = 'let __seq = 0; const mid = () => `g${++__seq}`;'
new Function('module', 'exports',
  prelude + '\n' + js + '\n;module.exports = { applyAgentEventBuffer, applySubagentEvent, mapMsg }'
)(mod, mod.exports)
const { applyAgentEventBuffer } = mod.exports

// ── 复现时间线 ──
const timeline = JSON.parse(readFileSync(join(root, '../scripts/repro_timeline.json'), 'utf8'))

// send() 造出的初始缓冲（user + 流式 assistant）
let msgs = [
  { id: 'm1', role: 'user', content: '看看我的pdf内容都有哪些？用子智能体看', thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: false, usage: {} },
  { id: 'm2', role: 'assistant', content: '', thinking: '', thinkingActive: false, toolCalls: [], subagents: [], activeToolId: null, streaming: true, usage: {} }
]

let turnCount = 0
const snap = (label, t) => {
  const subMsgs = msgs.filter((m) => (m.subagents ?? []).length > 0)
  console.log(`\n── [${t}s] ${label}`)
  console.log(`   消息数=${msgs.length} 流式中的 assistant=${msgs.filter((m) => m.streaming).length}`)
  for (const m of subMsgs) {
    for (const s of m.subagents) {
      console.log(`   卡片挂点=${m.id} id=${s.id} status=${s.status} streaming=${s.streaming} ` +
        `thinking=${s.thinking.length}ch 工具=${s.toolCalls.length}条 ` +
        `[${s.toolCalls.map((tc) => `${tc.name}:${tc.status}`).join(', ')}]`)
    }
  }
}

for (const { t, kind, payload } of timeline) {
  if (kind === 'event') {
    msgs = applyAgentEventBuffer(msgs, payload)
    const tp = payload.type
    if (tp === 'sub_agent_start') snap('sub_agent_start（后台线程开始）', t)
    else if (tp === 'tool_exec_start') snap(`tool_exec_start ${payload.tool_id?.slice(-6)}（工具开始执行）`, t)
    else if (tp === 'tool_exec_end') snap(`tool_exec_end ${payload.tool_id?.slice(-6)}（工具执行完成）`, t)
    else if (tp === 'sub_agent_end') snap('sub_agent_end（后台子智能体完成）', t)
    else if (tp === 'turn_end') { turnCount++; snap(`turn_end #${turnCount}`, t) }
  } else if (kind === 'session_status') {
    console.log(`\n── [${t}s] session_status: ${payload.status}`)
  }
}

snap('回放结束（最终态）', 0)
console.log('\n════════ 校验 ════════')
const card = msgs.flatMap((m) => m.subagents)[0]
const ok =
  card &&
  card.status === 'done' &&
  card.streaming === false &&
  card.toolCalls.length === 2 &&
  card.toolCalls.every((tc) => tc.status === 'done')
console.log(ok ? '✅ 卡片终态正确' : '❌ 卡片状态异常: ' + JSON.stringify(card, null, 1))
