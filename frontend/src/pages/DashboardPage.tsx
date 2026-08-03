import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  getDashboardStats,
  getSuspectTasks,
  DashboardStats,
  SuspectTaskItem,
} from '../api'

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function basename(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

const stageLabels: Record<string, string> = {
  extract_audio: '音频提取',
  run_asr: '语音识别',
  align_segments: '时间轴对齐',
  text_process: '文本处理',
  translate: '翻译',
  subtitle_render: '字幕渲染',
  output_finalize: '最终输出',
  mux: '字幕封装',
  queued: '排队中',
}

export function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [suspectItems, setSuspectItems] = useState<SuspectTaskItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const [statsResult, suspectResult] = await Promise.all([
        getDashboardStats(),
        getSuspectTasks(20),
      ])
      setStats(statsResult)
      setSuspectItems(suspectResult.items)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  if (loading) return <div className="page-loading">加载中…</div>
  if (error) return (
    <div className="page-loading">
      <div className="alert error">{error}</div>
      <button onClick={() => void load()}>重试</button>
    </div>
  )
  if (!stats) return null

  const maxDailyCount = Math.max(...stats.daily_trend.map((d) => d.count), 1)

  return (
    <div className="dashboard-page">
      <h2>统计面板</h2>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-value">{stats.total}</div>
          <div className="stat-label">总任务</div>
        </div>
        <div className="stat-card stat-done">
          <div className="stat-value">{stats.done}</div>
          <div className="stat-label">已完成</div>
        </div>
        <div className="stat-card stat-failed">
          <div className="stat-value">{stats.failed}</div>
          <div className="stat-label">失败</div>
        </div>
        <div className="stat-card stat-pending">
          <div className="stat-value">{stats.pending + stats.processing}</div>
          <div className="stat-label">待处理/进行中</div>
        </div>
        <div className="stat-card">
          <div className="stat-value">{stats.success_rate}%</div>
          <div className="stat-label">成功率</div>
        </div>
        <div className="stat-card">
          <div className="stat-value">{formatDuration(stats.avg_duration_seconds)}</div>
          <div className="stat-label">平均耗时</div>
        </div>
      </div>

      <div className="dashboard-charts">
        <div className="chart-section suspect-section">
          <h3>可疑字幕 ({suspectItems.length})</h3>
          {suspectItems.length === 0 ? (
            <p className="empty-hint">最近任务都通过了质量自检 ✓</p>
          ) : (
            <ul className="suspect-list">
              {suspectItems.map((item) => {
                const score = item.quality_report?.score
                const errorCount = (item.quality_report?.issues || []).filter((i) => i.severity === 'error').length
                const warningCount = (item.quality_report?.issues || []).filter((i) => i.severity === 'warning').length
                return (
                  <li key={item.id} className={`suspect-item severity-${errorCount > 0 ? 'error' : 'warning'}`}>
                    <Link to={`/tasks/${item.id}`} className="suspect-link">
                      <div className="suspect-header">
                        <span className="suspect-score" title="质量评分">{score ?? '-'} 分</span>
                        <span className="suspect-name" title={item.file_path}>{basename(item.file_path)}</span>
                        <span className="suspect-badges">
                          {errorCount > 0 ? <span className="badge badge-error">{errorCount} 错误</span> : null}
                          {warningCount > 0 ? <span className="badge badge-warning">{warningCount} 警告</span> : null}
                        </span>
                      </div>
                      <div className="suspect-summary">{item.quality_report?.summary || ''}</div>
                    </Link>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div className="chart-section">
          <h3>每日完成趋势（近14天）</h3>
          {stats.daily_trend.length === 0 ? (
            <p className="empty-hint">暂无数据</p>
          ) : (
            <div className="bar-chart">
              {stats.daily_trend.map((item) => (
                <div key={item.date} className="bar-item">
                  <div
                    className="bar-fill"
                    style={{ height: `${(item.count / maxDailyCount) * 100}%` }}
                    title={`${item.date}: ${item.count}`}
                  />
                  <span className="bar-label">{item.date.slice(5)}</span>
                  <span className="bar-count">{item.count}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="chart-section">
          <h3>各阶段平均耗时</h3>
          {stats.stage_stats.length === 0 ? (
            <p className="empty-hint">暂无数据</p>
          ) : (
            <table className="stage-table">
              <thead>
                <tr>
                  <th>阶段</th>
                  <th>任务数</th>
                  <th>平均耗时</th>
                </tr>
              </thead>
              <tbody>
                {stats.stage_stats.map((s) => (
                  <tr key={s.stage}>
                    <td>{stageLabels[s.stage] || s.stage}</td>
                    <td>{s.count}</td>
                    <td>{formatDuration(s.avg_seconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
