import { ModelItem } from '../api'
import { StepModelProps } from './types'

export function StepModel({
  models,
  selectedModel,
  setSelectedModel,
  canMoveFromModelStep,
  proxyItems,
  proxyConfigured,
  onReload,
  onDownload,
  onPrev,
  onNext,
}: StepModelProps) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>步骤 2：模型准备</h2>
          <p>请选择一个已安装模型，或先触发下载。也可手动挂载本地模型到 /models 目录。</p>
        </div>
        <button onClick={onReload}>检测本地模型</button>
      </div>
      <div className="model-grid">
        {models.map((item: ModelItem) => (
          <button
            key={item.name}
            type="button"
            className={`model-card ${selectedModel === item.name ? 'selected' : ''}`}
            onClick={() => setSelectedModel(item.name)}
          >
            <div className="table-main">
              <strong>{item.name}</strong>
              <span className={`status-chip ${item.status}`}>{item.status}</span>
            </div>
            <span className="muted">{item.size_label}</span>
            {item.status === 'downloading' ? (
              <div className="progress-block">
                <div className="progress-bar">
                  <span style={{ width: `${item.progress}%` }} />
                </div>
                <span>{item.progress}%</span>
              </div>
            ) : null}
            {item.stalled ? <span className="status-chip stalled">下载超时</span> : null}
            {item.error ? <span className="muted">{item.error}</span> : null}
            {item.stalled && item.manual_download_url ? (
              <a href={item.manual_download_url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
                前往 HuggingFace 手动下载
              </a>
            ) : null}
            <span className="muted">{item.path}</span>
            <div className="inline-actions">
              <button
                type="button"
                disabled={item.status !== 'not_installed'}
                onClick={(event) => {
                  event.stopPropagation()
                  onDownload(item.name)
                }}
              >
                下载
              </button>
              <span>{item.current ? '当前默认模型' : '可选模型'}</span>
            </div>
          </button>
        ))}
      </div>
      <div className="card">
        <div className="card-header">
          <div>
            <h3>代理与镜像配置</h3>
            <p>以下信息为容器当前生效的只读环境变量，修改后需重启容器。</p>
          </div>
        </div>
        <div className="summary-grid">
          {proxyItems.map((item) => (
            <div key={item.label} className="summary-item">
              <span>{item.label}</span>
              <strong>{item.value || '未配置'}</strong>
            </div>
          ))}
        </div>
        {!proxyConfigured ? (
          <div className="muted">如需加速模型下载，请在 Docker Compose 的 environment 中设置 HTTP_PROXY、HTTPS_PROXY 或 HF_ENDPOINT。</div>
        ) : null}
      </div>
      <div className="page-actions">
        <button onClick={onPrev}>上一步</button>
        <button disabled={!canMoveFromModelStep} onClick={onNext}>
          下一步
        </button>
      </div>
    </div>
  )
}
