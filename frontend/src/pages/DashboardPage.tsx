import { useEffect, useState } from 'react'
import { getDashboardStats, DashboardStats } from '../api'

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      setStats(await getDashboardStats())
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
