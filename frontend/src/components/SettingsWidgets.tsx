import { ReactNode, useState } from 'react'

import { AlignProvider, AppConfig } from '../api'

export type StepTone = 'success' | 'warning' | 'neutral' | 'muted'

export function TagEditor({
  label,
  values,
  placeholder,
  hint,
  onAdd,
  onRemove,
  disabled = false,
}: {
  label: string
  values: string[]
  placeholder: string
  hint?: ReactNode
  onAdd: (value: string) => void
  onRemove: (value: string) => void
  disabled?: boolean
}) {
  const [draft, setDraft] = useState('')

  return (
    <div className="field-block">
      <span className="field-label">{label}</span>
      <div className={`tag-editor ${disabled ? 'disabled' : ''}`}>
        <div className="tag-list">
          {values.map((value) => (
            <button
              key={value}
              type="button"
              className="tag-chip"
              onClick={() => onRemove(value)}
              disabled={disabled}
            >
              {value}
            </button>
          ))}
        </div>
        <div className="tag-input-row">
          <input
            value={draft}
            placeholder={placeholder}
            disabled={disabled}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                const next = draft.trim()
                if (next) {
                  onAdd(next)
                  setDraft('')
                }
              }
            }}
          />
          <button
            type="button"
            disabled={disabled || !draft.trim()}
            onClick={() => {
              const next = draft.trim()
              if (next) {
                onAdd(next)
                setDraft('')
              }
            }}
          >
            添加
          </button>
        </div>
      </div>
      {hint ? <span className="muted">{hint}</span> : null}
    </div>
  )
}

export function StepCard({
  index,
  title,
  description,
  statusLabel,
  tone,
  pills,
  expanded,
  onToggle,
  basicContent,
  advancedContent,
  headerActions,
}: {
  index: string
  title: string
  description: string
  statusLabel: string
  tone: StepTone
  pills: string[]
  expanded: boolean
  onToggle: () => void
  basicContent: ReactNode
  advancedContent?: ReactNode
  headerActions?: ReactNode
}) {
  const expandable = Boolean(advancedContent)
  const isExpanded = expandable && expanded
  return (
    <article className={`card pipeline-step-card tone-${tone} ${isExpanded ? 'is-expanded' : ''}`}>
      <div className="pipeline-step-header">
        <div className="pipeline-step-toggle">
          <span className="pipeline-step-rail" aria-hidden="true">
            <span className="pipeline-step-index">{index}</span>
            <span className="pipeline-step-line" />
          </span>
          <span className="pipeline-step-content">
            <span className="pipeline-step-topline">
              <strong>{title}</strong>
              <span className={`step-badge tone-${tone}`}>{statusLabel}</span>
            </span>
            <span className="pipeline-step-description">{description}</span>
            <span className="pipeline-step-pills" aria-hidden="true">
              {pills.map((pill) => (
                <span key={pill} className="summary-pill">
                  {pill}
                </span>
              ))}
            </span>
          </span>
        </div>
        {headerActions ? <div className="pipeline-step-actions">{headerActions}</div> : null}
      </div>
      <div className="pipeline-step-body">
        <div className="pipeline-step-basic">{basicContent}</div>
        {expandable ? (
          <button type="button" className={`pipeline-step-disclosure ${isExpanded ? 'is-open' : ''}`} onClick={onToggle}>
            <span className="pipeline-step-disclosure-label">{isExpanded ? '收起高级设置' : '高级设置'}</span>
          </button>
        ) : null}
        {isExpanded && advancedContent ? (
          <div className="pipeline-step-advanced">
            <div className="pipeline-step-advanced-header">
              <span className="pipeline-step-advanced-title">高级配置</span>
              <span className="muted">用于细化调优与运行策略，通常不需要频繁修改。</span>
            </div>
            {advancedContent}
          </div>
        ) : null}
      </div>
    </article>
  )
}

export function countChangedSections(current: AppConfig, saved: AppConfig): number {
  const groups: Array<keyof AppConfig> = ['file', 'whisper', 'translation', 'subtitle', 'mux', 'processing', 'logging']
  return groups.reduce((count, group) => {
    return JSON.stringify(current[group]) === JSON.stringify(saved[group]) ? count : count + 1
  }, 0)
}

export function getTranslationStatus(config: AppConfig): { tone: StepTone; label: string } {
  if (!config.translation.enabled) {
    return { tone: 'muted', label: '已关闭' }
  }
  const required = [
    config.translation.api_base_url.trim(),
    config.translation.api_key.trim(),
    config.translation.model.trim(),
    config.translation.target_languages.length > 0 ? 'ok' : '',
  ]
  return required.every(Boolean)
    ? { tone: 'success', label: '已就绪' }
    : { tone: 'warning', label: '待补充' }
}

export function getAlignStatus(
  alignProvider: AlignProvider,
  hintLevel: 'success' | 'warning' | 'muted',
): { tone: StepTone; label: string } {
  if (alignProvider === 'none') {
    return { tone: 'muted', label: '已禁用' }
  }
  if (hintLevel === 'warning') {
    return { tone: 'warning', label: '需注意' }
  }
  if (hintLevel === 'success') {
    return { tone: 'success', label: '已就绪' }
  }
  return { tone: 'neutral', label: '自动模式' }
}
