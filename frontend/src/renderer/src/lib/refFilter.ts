import type { RefListItem } from '@protocols/agentProtocol'

/**
 * 引用候选的**前端本地过滤**（纯函数，无副作用）。
 *
 * 为什么在前端做：`refs_list` 一次拉回完整扁平列表后，按键过滤必须"零延迟"才有
 * 实时感；每次按键回后端查会带来网络往返与请求乱序，还得处理竞态。
 * 代价是**被上限截断的那部分搜不到** —— 后端在列表底部给"已截断"提示来兜住这个
 * 认知缺口（见 docs/frontend/13 的取舍一节）。
 */

/** 一条可直接渲染 / 序列化的候选。
 *
 *  `rel` / `dir` 都由 `toCandidates` 从 `workdir` + `path` 推导，**不让后端重复发**
 *  （同一份切分逻辑只写一次）。 */
export interface RefCandidate {
  /** 绝对路径（权威；发送时给后端、tooltip 显示） */
  path: string
  /** 带后缀的文件名（胶囊与列表显示） */
  name: string
  isDir: boolean
  /** 相对工作空间的路径（胶囊序列化出的 `@token` 用它；同名文件靠它区分） */
  rel: string
  /** 所在目录的绝对路径（末尾带 `/`）—— 平铺列表里靠它消歧 */
  dir: string
}

/** 列表条目的渲染上限。
 *
 *  后端上限 3000 条，但**全量 DOM 渲染**会让面板明显卡顿。这里只画前 200 条，
 *  并在底部提示"继续输入以缩小范围" —— 比引入虚拟滚动库简单得多，也够用
 *  （用户真要找某个文件时本来就会打字过滤）。 */
export const REF_RENDER_LIMIT = 200

/** 后端条目 → 可渲染候选（顺带算出 rel / dir）。 */
export function toCandidates(workdir: string, items: RefListItem[] | null | undefined): RefCandidate[] {
  const base = (workdir || '').replace(/\/+$/, '')
  const list = Array.isArray(items) ? items : []
  return list.map((it) => {
    const path = String(it?.path ?? '')
    const name = String(it?.name ?? '')
    // rel：剥掉工作空间前缀；不在工作空间内（理论上不会）就退回绝对路径
    let rel = path
    if (base && path.startsWith(base + '/')) rel = path.slice(base.length + 1)
    // dir：path 去掉结尾的 name（保留尾斜杠，与列表展示口径一致）
    let dir = ''
    if (name && path.length > name.length) dir = path.slice(0, path.length - name.length)
    if (!dir) dir = base ? base + '/' : '/'
    return { path, name, isDir: it?.type === 'dir', rel, dir }
  })
}

/** 单条候选的匹配打分：越小越靠前，`-1` = 不匹配。
 *
 *  优先级（刻意如此，不是随手排的）：
 *    ① 文件名**前缀**命中 —— 打 `re` 想找 `README.md` 是最常见的心智；
 *    ② 文件名子串命中；
 *    ③ 相对路径子串命中 —— 让 `@src/` 能一次缩到那个目录；
 *  各级内部按"命中位置越靠前越优先"。
 */
function scoreOf(c: RefCandidate, q: string): number {
  const name = c.name.toLowerCase()
  const rel = c.rel.toLowerCase()
  const inName = name.indexOf(q)
  if (inName === 0) return 0
  if (inName > 0) return 100 + inName
  const inRel = rel.indexOf(q)
  if (inRel >= 0) return 200 + inRel
  return -1
}

/**
 * 过滤 + 排序。空 query 直接返回原列表（保持后端的 BFS 顺序：浅层在前）。
 *
 *  **不做拼音匹配**：v1 明确不做（成本与收益不成比例），别当成 bug。
 */
export function filterRefs(list: RefCandidate[], query: string): RefCandidate[] {
  const q = (query || '').trim().toLowerCase()
  if (!q) return list
  const hits: Array<{ c: RefCandidate; s: number }> = []
  for (const c of list) {
    const s = scoreOf(c, q)
    if (s >= 0) hits.push({ c, s })
  }
  hits.sort(
    (a, b) =>
      a.s - b.s ||
      // 同级：浅层优先（路径短的更靠近根，通常更可能是目标）
      a.c.rel.length - b.c.rel.length ||
      a.c.rel.localeCompare(b.c.rel)
  )
  return hits.map((h) => h.c)
}

/** 胶囊里的显示名（带后缀；目录加尾斜杠便于区分）。 */
export function capsuleLabel(c: { name: string; isDir: boolean }): string {
  const name = c.name || ''
  return c.isDir && !name.endsWith('/') ? name + '/' : name
}
