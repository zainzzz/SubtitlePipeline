import { createContext, useContext, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { getSystemStatus, SystemStatus } from './api'
import { useEventStream } from './hooks'
import { ModelManagerPage } from './pages/ModelManagerPage'
import { SettingsPage } from './pages/SettingsPage'
import { SetupWizard } from './pages/SetupWizard'
import { TaskDetailPage } from './pages/TaskDetailPage'
import { TasksPage } from './pages/TasksPage'

type SseEvent = { type: string; data: unknown; ts: number }
type EventContextValue = { events: SseEvent[]; lastEvent: SseEvent | null }

const EventContext = createContext<EventContextValue>({ events: [], lastEvent: null })
export const useSseEvents = () => useContext(EventContext)

function SidebarLayout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">SubPipeline</div>
        <nav>
          <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>
            任务列表
          </NavLink>
          <NavLink to="/models" className={({ isActive }) => (isActive ? 'active' : '')}>
            模型管理
          </NavLink>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? 'active' : '')}>
            设置
          </NavLink>
        </nav>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<TasksPage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/models" element={<ModelManagerPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [fatalError, setFatalError] = useState<string>('')
  const location = useLocation()

  const [events, setEvents] = useState<SseEvent[]>([])
  useEventStream((type, data) => {
    setEvents((prev) => {
      const next = [...prev, { type, data, ts: Date.now() }]
      return next.length > 200 ? next.slice(-200) : next
    })
  })

  // Fatal-error banner: fed by global window.error/unhandledrejection handlers
  // in main.tsx and by ErrorBoundary via the `app:fatal-error` CustomEvent.
  useEffect(() => {
    const onFatal = (event: Event) => {
      const detail = (event as CustomEvent).detail as { source?: string; message?: string } | undefined
      const source = detail?.source ?? 'unknown'
      const message = detail?.message ?? '发生未知错误'
      setFatalError(`[${source}] ${message}`)
    }
    window.addEventListener('app:fatal-error', onFatal as EventListener)
    return () => window.removeEventListener('app:fatal-error', onFatal as EventListener)
  }, [])

  const loadStatus = async () => {
    setLoading(true)
    try {
      setStatus(await getSystemStatus())
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '系统状态读取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadStatus()
  }, [])

  if (loading) {
    return <div className="app-loading">系统状态加载中…</div>
  }

  if (error) {
    return (
      <div className="app-loading">
        <div className="alert error">{error}</div>
        <button onClick={() => void loadStatus()}>重试</button>
      </div>
    )
  }

  if (!status) {
    return <div className="app-loading">缺少系统状态</div>
  }

  if (!status.setup_complete) {
    return (
      <Routes>
        <Route path="/setup" element={<SetupWizard onCompleted={loadStatus} />} />
        <Route path="*" element={<Navigate to="/setup" replace />} />
      </Routes>
    )
  }

  if (location.pathname === '/setup') {
    return <Navigate to="/" replace />
  }

  const ctx: EventContextValue = { events, lastEvent: events.length ? events[events.length - 1] : null }
  return (
    <EventContext.Provider value={ctx}>
      {fatalError ? (
        <div className="fatal-error-banner" role="alert" aria-live="assertive">
          <span>{fatalError}</span>
          <button aria-label="关闭致命错误提示" onClick={() => setFatalError('')}>×</button>
        </div>
      ) : null}
      <SidebarLayout />
    </EventContext.Provider>
  )
}
