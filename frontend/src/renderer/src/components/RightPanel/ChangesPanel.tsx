import { useMemo, useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { GitStatusFile } from '@protocols/agentProtocol'
import { basenameOf } from '@lib/rpanelTabs'
import { EMPTY_LIVE, useRightPanelStore } from '@store/rightPanelStore'
import RPanelState from './RPanelState'

interface ChangesPanelProps {
  sid: string
}

/** 分组定义（顺序即渲染顺序：冲突 → 已暂存 → 未暂存 → 未跟踪）。
 *
 *  冲突必须排最前：有冲突时其它改动都要让路，用户第一眼该看到"这里要我处理"。
 *  注意"已暂存"与"未暂存"**可以命中同一个文件**（暂存后又改）—— 这是 git 的真实
 *  状态，不是数据错误，所以按谓词分组、允许重复出现，而不是把文件塞进唯一分组。 */
const GROUPS: Array<{ key: string; label: string; match: (f: GitStatusFile) => boolean }> = [
  { key: 'conflict', label: '冲突', match: (f) => f.conflicted },
  { key: 'staged', label: '已暂存', match: (f) => !f.conflicted && f.staged },
  { key: 'unstaged', label: '未暂存', match: (f) => !f.conflicted && f.has_working_changes },
  { key: 'untracked', label: '未跟踪', match: (f) => !f.conflicted && f.untracked }
]

/** 把仓库相对路径拆成「目录 / 文件名」两段（目录可压缩，文件名永远完整）。 */
function splitPath(p: string): { dir: string; name: string } {
  const i = p.lastIndexOf('/')
  return i < 0 ? { dir: '', name: p } : { dir: p.slice(0, i + 1), name: p.slice(i + 1) }
}

type DiffLineKind = 'add' | 'del' | 'hunk' | 'meta' | 'ctx'

function diffLineKind(line: string): DiffLineKind {
  if (line.startsWith('@@')) return 'hunk'
  if (
    line.startsWith('+++') ||
    line.startsWith('---') ||
    line.startsWith('diff ') ||
    line.startsWith('index ') ||
    line.startsWith('new file') ||
    line.startsWith('deleted file') ||
    line.startsWith('old mode') ||
    line.startsWith('new mode') ||
    line.startsWith('similarity index') ||
    line.startsWith('rename ') ||
    line.startsWith('Binary files')
  ) {
    return 'meta'
  }
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return 'ctx'
}

/** 单个文件的 diff 正文（**内联展开**，不替换右侧内容 —— 360px 里整屏 diff
 *  要大量横向滚动，内联还能同时对比多个文件，见 19 篇 §5.8）。 */
function DiffBody({ sid, path, staged }: { sid: string; path: string; staged: boolean }): JSX.Element {
  const live = useRightPanelStore((s) => s.liveBySession[sid]) ?? EMPTY_LIVE
  const [showRaw, setShowRaw] = useState(false)
  const loading = !!live.diffLoading[path]
  const data = live.diffs[path]
  const lines = useMemo(() => (data?.diff ? data.diff.split('\n') : []), [data?.diff])

  if (loading && !data) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">正在读取 diff…</span>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">未能读取该文件的 diff</span>
      </div>
    )
  }
  if (!data.available) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">{data.reason || '无法读取该文件的 diff'}</span>
      </div>
    )
  }
  if (data.binary) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">二进制文件，无法显示 diff</span>
      </div>
    )
  }
  // `too_large` 时后端**故意**不回内容（半截 diff 会让人以为"改动就这么点"）
  // → 没有"显示原文"可给，只说明情况。
  if (data.too_large) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">
          该文件改动过大（超过 {Math.round(data.chars / 1024) || 0} KB 上限），已折叠
        </span>
      </div>
    )
  }
  if (!lines.length || (lines.length === 1 && !lines[0])) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">
          {data.untracked ? '新文件（未跟踪），暂无内容差异' : '没有可显示的差异'}
        </span>
      </div>
    )
  }
  // 行数上限：>3000 行先折叠（一次渲染三万行 <span> 会把右栏拖死）。
  // 这是**渲染保护**，与后端的字符上限是两回事 —— 所以这里给"显示原始 diff"的出口。
  if (lines.length > 3000 && !showRaw) {
    return (
      <div className="git-diff">
        <span className="git-diff-empty">
          该文件改动过大（{lines.length} 行），已折叠
        </span>
        <div className="rpanel-state__action" style={{ padding: '0 8px 8px' }}>
          <button className="rpanel-btn" onClick={() => setShowRaw(true)}>
            显示原始 diff
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="git-diff">
      {lines.map((line, i) => (
        <span key={`${i}:${line.slice(0, 12)}`} className={`git-diff-line ${diffLineKind(line)}`}>
          {line || ' '}
        </span>
      ))}
      {staged ? <span className="git-diff-line meta">（以上为暂存区内容）</span> : null}
    </div>
  )
}

/**
 * 「变更」视图：git 状态 + 内联 diff（19 篇 §5.8）。
 *
 * **不做写操作**（暂存 / 提交 / 丢弃）：写操作要过权限判定链（docs/frontend/17），
 * 那是另一个议题。本期这一屏是纯只读的"现在有什么变了"。
 *
 * 非 git 仓库 / 干净仓库都是**平级空态**，不是错误（`available:false` + reason）。
 */
export default function ChangesPanel({ sid }: ChangesPanelProps): JSX.Element {
  const live = useRightPanelStore((s) => s.liveBySession[sid]) ?? EMPTY_LIVE
  const ensureGit = useRightPanelStore((s) => s.ensureGit)
  const toggleGitDiff = useRightPanelStore((s) => s.toggleGitDiff)
  const openFileTab = useRightPanelStore((s) => s.openFileTab)
  const { git, gitLoading, gitError, gitExpanded } = live

  const grouped = useMemo(
    () => GROUPS.map((g) => ({ ...g, files: (git?.files ?? []).filter(g.match) })),
    [git?.files]
  )

  const head = (
    <div className="rpanel-head">
      <div className="rpanel-head-title">
        <span className="rpanel-head-name">变更</span>
        {git?.branch ? (
          <span className="rpanel-head-path" style={{ flex: 'none' }} title="当前分支">
            {git.branch}
          </span>
        ) : null}
        {git?.ahead || git?.behind ? (
          <span className="rpanel-head-path" style={{ flex: 'none' }} title="领先 / 落后上游的提交数">
            ↑{git?.ahead ?? 0} ↓{git?.behind ?? 0}
          </span>
        ) : null}
      </div>
      <div className="rpanel-head-actions">
        <button className="mini-btn" title="刷新变更" onClick={() => ensureGit(sid, true)}>
          {gitLoading ? <span className="rpanel-spinner" /> : <Icon name="refresh" size={14} />}
        </button>
      </div>
    </div>
  )

  if (gitError) {
    return (
      <div className="rpanel-git">
        {head}
        <RPanelState
          kind="error"
          icon="diff"
          title="无法读取 git 状态"
          sub={gitError}
          action={
            <button className="rpanel-btn" onClick={() => ensureGit(sid, true)}>
              重试
            </button>
          }
        />
      </div>
    )
  }
  if (!git) {
    return (
      <div className="rpanel-git">
        {head}
        <RPanelState kind="loading" title="正在读取变更…" />
      </div>
    )
  }
  if (!git.available) {
    return (
      <div className="rpanel-git">
        {head}
        <RPanelState
          kind="empty"
          icon="diff"
          title="当前工作空间不是 Git 仓库"
          sub={git.reason}
        />
      </div>
    )
  }
  if (!git.files.length) {
    return (
      <div className="rpanel-git">
        {head}
        <RPanelState kind="empty" icon="check" title="没有未提交的改动" />
      </div>
    )
  }

  return (
    <div className="rpanel-git">
      {head}
      <div className="rpanel-git-scroll">
        {grouped.map((group) =>
          group.files.length ? (
            <div key={group.key} className={`git-group git-group--${group.key}`}>
              <div className="git-group__head">
                <Icon name="chevronDown" size={12} />
                <span className="git-group__name">{group.label}</span>
                <span className="git-group__count">{group.files.length}</span>
              </div>
              {group.files.map((f) => {
                const expanded = gitExpanded === f.path
                const { dir, name } = splitPath(f.path)
                const abs = git.root ? `${git.root.replace(/\/+$/, '')}/${f.path}` : f.path
                return (
                  <div key={`${group.key}:${f.path}`}>
                    {/* 行本身是 div 而不是 button：行尾要放一枚**真按钮**（在右栏打开），
                        按钮套按钮是非法嵌套（点击与键盘都不可预期）。 */}
                    <div
                      className={`git-file${expanded ? ' expanded' : ''}`}
                      role="button"
                      tabIndex={0}
                      title={f.path}
                      onClick={() => toggleGitDiff(sid, f.path, f.staged && !f.has_working_changes)}
                      onKeyDown={(e) => {
                        if (e.key !== 'Enter') return
                        e.preventDefault()
                        toggleGitDiff(sid, f.path, f.staged && !f.has_working_changes)
                      }}
                    >
                      <span className={`git-file__code ${group.key}`}>{f.code || '??'}</span>
                      <span className="git-file__path">
                        <span className="git-file__dir">{dir}</span>
                        <span className="git-file__name">{name}</span>
                      </span>
                      <span className="git-file__actions">
                        <button
                          className="mini-btn"
                          title="在右栏打开该文件"
                          onClick={(e) => {
                            // 阻止冒泡：点"打开"不该顺带把 diff 展开/收起
                            e.stopPropagation()
                            openFileTab(sid, { path: abs, name: basenameOf(abs) }, 'tree')
                          }}
                        >
                          <Icon name="fileText" size={13} />
                        </button>
                      </span>
                    </div>
                    {expanded ? (
                      <DiffBody sid={sid} path={f.path} staged={f.staged && !f.has_working_changes} />
                    ) : null}
                  </div>
                )
              })}
            </div>
          ) : null
        )}
        {git.truncated ? (
          <div className="rpanel-tree-foot">文件列表已达上限，仅显示前一部分</div>
        ) : null}
      </div>
    </div>
  )
}
