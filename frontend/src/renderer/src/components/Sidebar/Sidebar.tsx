import QuickActions from './QuickActions'
import WorkspaceTree from './WorkspaceTree'
import UserCard from './UserCard'

/** 左侧边栏：快捷操作 / 任务列表（工作空间树）/ 用户信息 */
export default function Sidebar(): JSX.Element {
  return (
    <aside className="sidebar">
      <QuickActions />
      <WorkspaceTree />
      <UserCard />
    </aside>
  )
}