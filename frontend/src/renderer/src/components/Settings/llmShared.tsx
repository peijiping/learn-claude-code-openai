import type { LlmAdvanced, LlmCapabilities, LlmProvider, LlmProviderModel } from '@protocols/agentProtocol'

/** 输入能力候选项（文本为默认） */
export const CAP_INPUT: { key: string; label: string }[] = [
  { key: 'text', label: '文本' },
  { key: 'image', label: '图片' },
  { key: 'video', label: '视频' },
  { key: 'pdf', label: 'PDF' }
]

/** 输出能力候选项 */
export const CAP_OUTPUT: { key: string; label: string }[] = [{ key: 'text', label: '文本' }]

export const CAP_LABEL: Record<string, string> = {
  text: '文本',
  image: '图片',
  video: '视频',
  pdf: 'PDF'
}

export const DEFAULT_CAPS: LlmCapabilities = { input: ['text'], output: ['text'] }

/** 默认「模型列表」接口路径（写进连接的兼容设置） */
export const DEFAULT_MODELS_PATH = '/models'

/** 上下文窗口候选（token 数） */
export const CONTEXT_IN_OPTIONS: { value: string; label: string }[] = [
  { value: '131072', label: '131,072' },
  { value: '262144', label: '262,144' },
  { value: '524288', label: '524,288' },
  { value: '1048576', label: '1,048,576' },
  { value: '2097152', label: '2,097,152' }
]

/** 输出上限候选（token 数） */
export const CONTEXT_OUT_OPTIONS: { value: string; label: string }[] = [
  { value: '4096', label: '4,096' },
  { value: '16384', label: '16,384' },
  { value: '32768', label: '32,768' },
  { value: '65536', label: '65,536' }
]

/** "1M" / "128k" / "8000" → token 数（与后端 llm_config._parse_tokens 对齐） */
export function tokensOf(label?: string | null): number | null {
  const s = (label ?? '').trim().toUpperCase()
  if (!s) return null
  let mult = 1
  let body = s
  if (s.endsWith('K')) {
    mult = 1000
    body = s.slice(0, -1)
  } else if (s.endsWith('M')) {
    mult = 1000000
    body = s.slice(0, -1)
  }
  const n = Number(body)
  return Number.isFinite(n) && n > 0 ? Math.round(n * mult) : null
}

export function formatTokens(n: number | null | undefined): string {
  return n ? n.toLocaleString('en-US') : ''
}

/** 厂商首字母（左侧列表 / 头像） */
export function providerInitial(name: string | undefined | null): string {
  return (name || '?').trim().slice(0, 1).toUpperCase()
}

/** 生成短 id（与后端 c_ / m_ 前缀风格一致） */
export function genId(prefix: string): string {
  return prefix + Math.random().toString(36).slice(2, 10)
}

/** 高级（进阶）设置是否有任一项被填写 */
export function hasAdvancedValue(adv: LlmAdvanced | undefined | null): boolean {
  if (!adv) return false
  return Object.values(adv).some((v) => typeof v === 'string' && v.trim() !== '')
}

/** 数值型进阶项校验：留空合法；填了必须是 min~max 的数字 */
export function numOk(v: string | undefined, min: number, max: number): boolean {
  const s = (v ?? '').trim()
  if (!s) return true
  const n = Number(s)
  return Number.isFinite(n) && n >= min && n <= max
}

/** 在预置目录里按模型 id 查「自动识别」的元数据 */
export function detectPresetModel(
  providerPreset: LlmProvider | null | undefined,
  modelId: string
): LlmProviderModel | undefined {
  const id = modelId.trim()
  if (!id) return undefined
  return providerPreset?.models?.find((m) => m.id === id)
}

/** 预置目录里已识别的模型 id 集合（用于列表里标记「图片能力未识别」） */
export function knownModelIdsOf(providerPreset: LlmProvider | null | undefined): Set<string> {
  return new Set((providerPreset?.models ?? []).map((m) => m.id))
}

/** 该模型是否被标记为支持图片输入 */
export function hasImageInput(caps: LlmCapabilities | undefined): boolean {
  return !!caps?.input?.includes('image')
}
