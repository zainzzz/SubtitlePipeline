import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  getTask,
  getTaskLogs,
  getTaskSubtitle,
  triggerSubtitleWebhook,
  updateTaskSubtitle,
  LogResponse,
  Task,
  WebhookStatus,
} from '../api'
import { usePolling } from '../hooks'

function WebhookBadge({
  status,
  busy,
  onRetry,
}: {
  status: WebhookStatus
  busy: boolean
  onRetry: () => void
}) {
  let label = ''
  let tone: 'success' | 'error' | 'neutral' = 'neutral'
  if (status.state === 'success') {
    label = `已通知 ${status.webhook_type || '媒体库'} ✓`
    tone = 'success'
  } else if (status.state === 'failed') {
    label = `通知失败: ${status.error || '未知错误'}`
    tone = 'error'
  } else {
    label = `未通知 (${status.detail || status.state})`
    tone = 'neutral'
  }
  return (
    <div className={`webhook-badge webhook-${tone}`} style={{ marginTop: 8, fontSize: 12 }}>
      <span>{label}</span>
      {status.state === 'failed' ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={busy}
          style={{ marginLeft: 8, padding: '2px 8px', fontSize: 12 }}
        >
          {busy ? '重试中…' : '重试'}
        </button>
      ) : null}
    </div>
  )
}

export function TaskDetailPage() {
  const { taskId } = useParams()
  const [task, setTask] = useState<Task | null>(null)
  const [logs, setLogs] = useState<LogResponse>({ items: [], total: 0, page: 1, page_size: 20 })
  const [page, setPage] = useState(1)
  const [error, setError] = useState('')
  const [subtitleContent, setSubtitleContent] = useState<string | null>(null)
  const [subtitlePath, setSubtitlePath] = useState('')
  const [editing, setEditing] = useState(false)
  const [editContent, setEditContent] = useState('')
  const [webhookStatus, setWebhookStatus] = useState<WebhookStatus | null>(null)
  const [webhookBusy, setWebhookBusy] = useState(false)

  const loadTask = useCallback(async () => {
    if (!taskId) {
      return
    }
    try {
      setTask(await getTask(taskId))
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '任务详情读取失败')
    }
  }, [taskId])

  const loadLogs = useCallback(
    async (targetPage: number) => {
      if (!taskId) {
        return
      }
      try {
        setLogs(await getTaskLogs(taskId, targetPage))
        setPage(targetPage)
      } catch (err) {
        setError(err instanceof Error ? err.message : '日志读取失败')
      }
    },
    [taskId],
  )

  const poll = useCallback(async () => {
    await loadTask()
    await loadLogs(page)
  }, [loadLogs, loadTask, page])

  usePolling(poll, 3000, [page])

  if (!taskId) {
    return <div className="alert error">缺少任务 ID</div>
  }

  const loadSubtitle = async () => {
    if (!taskId || !task) return
    if (task.status !== 'done' || !task.result_payload?.subtitle_paths?.length) return
    try {
      const result = await getTaskSubtitle(Number(taskId))
      setSubtitleContent(result.content)
      setSubtitlePath(result.path)
    } catch {
      setSubtitleContent(null)
    }
  }

  const startEdit = () => {
    if (subtitleContent === null) return
    setEditContent(subtitleContent)
    setEditing(true)
  }

  const saveEdit = async () => {
    if (!taskId) return
    try {
      const result = await updateTaskSubtitle(Number(taskId), editContent)
      setSubtitleContent(editContent)
      setEditing(false)
      setWebhookStatus(result.webhook)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    }
  }

  const resendWebhook = async () => {
    if (!taskId || webhookBusy) return
    setWebhookBusy(true)
    try {
      const result = await triggerSubtitleWebhook(Number(taskId))
      setWebhookStatus(result.webhook)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重新通知失败')
    } finally {
      setWebhookBusy(false)
    }
  }

  const hasSubtitle = task?.status === 'done' && task?.result_payload?.subtitle_paths?.length

  return (
    <section>
      <header className="page-header">
        <div>
          <h1>任务详情 #{taskId}</h1>
          <p>查看阶段、输出路径与结构化日志。</p>
        </div>
        <Link to="/">返回列表</Link>
      </header>
      {error ? <div className="alert error">{error}</div> : null}
      {task ? (
        <>
          <div className="detail-grid">
            <div className="card">
              <h2>概览</h2>
              <dl className="detail-list">
                <div>
                  <dt>文件</dt>
                  <dd>{task.file_path}</dd>
                </div>
                <div>
                  <dt>状态</dt>
                  <dd>{task.status}</dd>
                </div>
                <div>
                  <dt>阶段</dt>
                  <dd>{task.stage}</dd>
                </div>
                <div>
                  <dt>进度</dt>
                  <dd>{task.progress}%</dd>
                </div>
                <div>
                  <dt>错误</dt>
                  <dd>{task.error_message || '-'}</dd>
                </div>
              </dl>
            </div>
            <div className="card">
              <h2>输出</h2>
              <ul className="simple-list">
                {task.result_payload?.subtitle_paths?.length ? (
                  task.result_payload.subtitle_paths.map((path) => <li key={path}>{path}</li>)
                ) : (
                  <li>暂无输出</li>
                )}
                {task.result_payload?.mux_path ? <li>{task.result_payload.mux_path}</li> : null}
              </ul>
            </div>
          </div>
          {task.result_payload?.quality_report ? (
            <div className={`card quality-card quality-${task.result_payload.quality_report.is_suspect ? 'suspect' : 'ok'}`}>
              <div className="card-header">
                <h2>字幕质量自检</h2>
                <div className="quality-score-block">
                  <span className="quality-score">
                    {task.result_payload.quality_report.score ?? '-'}
                  </span>
                  <span className="quality-score-suffix">/ 100</span>
                </div>
              </div>
              <p className="quality-summary">{task.result_payload.quality_report.summary || '无问题'}</p>
              {task.result_payload.quality_report.issues.length > 0 ? (
                <ul className="quality-issues">
                  {task.result_payload.quality_report.issues.map((issue, idx) => (
                    <li key={`${issue.code}-${idx}`} className={`quality-issue severity-${issue.severity}`}>
                      <span className={`quality-tag quality-tag-${issue.severity}`}>
                        {issue.severity === 'error' ? '错误' : '警告'}
                      </span>
                      <span className="quality-message">{issue.message}</span>
                      {issue.segment_index !== null && issue.segment_index !== undefined ? (
                        <span className="quality-seg">第 {issue.segment_index + 1} 段</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
          {hasSubtitle ? (
            <div className="card subtitle-preview">
              <div className="card-header">
                <h2>字幕预览</h2>
                <div>
                  {subtitleContent === null ? (
                    <button onClick={() => void loadSubtitle()} type="button">加载字幕</button>
                  ) : editing ? (
                    <>
                      <button onClick={() => void saveEdit()} type="button">保存</button>
                      <button onClick={() => setEditing(false)} type="button">取消</button>
                    </>
                  ) : (
                    <button onClick={startEdit} type="button">编辑</button>
                  )}
                </div>
              </div>
              {subtitleContent !== null ? (
                editing ? (
                  <textarea
                    className="subtitle-edit-area"
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                  />
                ) : (
                  <pre>{subtitleContent}</pre>
                )
              ) : (
                <p className="muted">点击"加载字幕"查看内容</p>
              )}
              {subtitlePath ? <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>{subtitlePath}</p> : null}
              {webhookStatus ? <WebhookBadge status={webhookStatus} busy={webhookBusy} onRetry={() => void resendWebhook()} /> : null}
            </div>
          ) : null}
          <div className="card">
            <div className="card-header">
              <h2>日志</h2>
              <div className="pagination">
                <button disabled={page <= 1} onClick={() => void loadLogs(page - 1)}>
                  上一页
                </button>
                <span>
                  第 {page} 页 / 共 {Math.max(Math.ceil(logs.total / logs.page_size), 1)} 页
                </span>
                <button
                  disabled={page >= Math.ceil(logs.total / logs.page_size)}
                  onClick={() => void loadLogs(page + 1)}
                >
                  下一页
                </button>
              </div>
            </div>
            <ul className="log-list">
              {logs.items.map((log) => (
                <li key={log.id}>
                  <span>{new Date(log.timestamp).toLocaleString()}</span>
                  <strong>{log.level}</strong>
                  <span>{log.stage}</span>
                  <span>{log.message}</span>
                </li>
              ))}
            </ul>
          </div>
        </>
      ) : (
        <div className="card muted">加载中…</div>
      )}
    </section>
  )
}
