import type { RefListItem } from '@protocols/agentProtocol'

/**
 * 扁平列表 → 文件树（纯函数，右栏「文件」视图用）。
 *
 * 后端 `refs.py::list_workspace()` 返回的是**扁平列表**（BFS、无层级，见
 * docs/frontend/13）：那是为 @-mention 的"按键过滤"设计的（过滤要的是扁平全集），
 * 而右栏要的是树。切分逻辑**只有这一处**，不复用后端发的相对路径字段。
 *
 * 两件必须做的事：
 * 1. **补虚拟目录**：BFS 碰到条数上限会截断，被截断的那一支可能出现"子孙在列表里、
 *    祖先不在"（例如只列到 `src/a/b/c.ts` 而没有 `src/a/b`）—— 不补的话这些文件
 *    在树里直接消失。补出来的节点标 `synthetic`。
 * 2. **稳定排序**：目录在前、再按 `localeCompare(..., 'zh')`。用中文排序规则是因为
 *    用户的工作空间里中文文件名很常见，拼音序比码点序符合直觉。
 */

export interface RPanelTreeNode {
  /** 绝对路径（打开预览、在 Finder 中显示都用它） */
  path: string
  /** 该层显示名（末段） */
  name: string
  isDir: boolean
  /** 中间层补出来的目录（后端列表里没有这一条），仅用于展示与展开 */
  synthetic?: boolean
  children: RPanelTreeNode[]
}

function splitRel(base: string, path: string): string[] | null {
  if (!base) return null
  if (path === base) return []
  if (!path.startsWith(base + '/')) return null
  return path.slice(base.length + 1).split('/').filter(Boolean)
}

/**
 * `items` 为后端 `refs` 回执的条目；`workdir` 为同一次回执的沙箱根。
 * 不在 `workdir` 之下的条目会被忽略（协议上不该出现，出现也不该把树画歪）。
 */
export function buildFileTree(workdir: string, items: RefListItem[] | null | undefined): RPanelTreeNode[] {
  const base = (workdir || '').replace(/\/+$/, '')
  const list = Array.isArray(items) ? items : []
  if (!base) return []

  const root: RPanelTreeNode = { path: base, name: '', isDir: true, children: [] }
  const byPath = new Map<string, RPanelTreeNode>([[base, root]])

  const ensureDir = (absPath: string, name: string, synthetic: boolean): RPanelTreeNode => {
    const found = byPath.get(absPath)
    if (found) {
      if (!synthetic) found.synthetic = undefined
      return found
    }
    const node: RPanelTreeNode = {
      path: absPath,
      name,
      isDir: true,
      ...(synthetic ? { synthetic: true } : {}),
      children: []
    }
    byPath.set(absPath, node)
    return node
  }

  for (const item of list) {
    const abs = String(item?.path ?? '')
    const segments = splitRel(base, abs)
    if (!segments || !segments.length) continue
    const isDir = item?.type === 'dir'
    let cursor = root
    let acc = base
    for (let i = 0; i < segments.length - 1; i += 1) {
      acc = `${acc}/${segments[i]}`
      const dir = ensureDir(acc, segments[i], true)
      // 同一目录可能被多条后代条目反复"路过"（补出来的中间层）→ 只挂一次
      if (!cursor.children.includes(dir)) cursor.children.push(dir)
      cursor = dir
    }
    const leafName = segments[segments.length - 1]
    if (isDir) {
      const dir = ensureDir(abs, leafName, false)
      if (!cursor.children.includes(dir)) cursor.children.push(dir)
    } else {
      const found = byPath.get(abs)
      if (found && !found.isDir) continue
      const file: RPanelTreeNode = { path: abs, name: item?.name || leafName, isDir: false, children: [] }
      byPath.set(abs, file)
      if (!cursor.children.includes(file)) cursor.children.push(file)
    }
  }

  sortTree(root.children)
  return root.children
}

/** 目录在前、再按中文排序（原地排序，递归） */
function sortTree(nodes: RPanelTreeNode[]): void {
  nodes.sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
    return a.name.localeCompare(b.name, 'zh')
  })
  for (const node of nodes) if (node.children.length) sortTree(node.children)
}

/** 展开某条绝对路径所需的全部祖先目录（用于"从会话点文件 → 树里展开到它"）。
 *  返回不含自身，从根下一层开始。 */
export function ancestorDirs(base: string, path: string): string[] {
  const root = (base || '').replace(/\/+$/, '')
  const segments = splitRel(root, path)
  if (!segments || segments.length <= 1) return []
  const out: string[] = []
  let acc = root
  for (let i = 0; i < segments.length - 1; i += 1) {
    acc = `${acc}/${segments[i]}`
    out.push(acc)
  }
  return out
}
