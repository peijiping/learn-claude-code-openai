import QuickActions from './QuickActions'
import TaskTree from './TaskTree'
import UserCard from './UserCard'

/** 左侧边栏：快捷操作 / 任务列表 / 用户信息 */
export default function Sidebar(): JSX.Element {
  return (
    <aside className="sidebar">
      <QuickActions />
      <TaskTree />
      <UserCard />
    </aside>
  )
}