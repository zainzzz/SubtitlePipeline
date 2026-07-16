import { Link } from 'react-router-dom'

import { AlignProvider } from '../../api'
import { StepCard } from '../SettingsWidgets'
import { AlignStepProps } from './types'

const alignProviderOptions: Array<{ value: AlignProvider; label: string }> = [
  { value: 'auto', label: '自动（推荐）' },
  { value: 'whisperx', label: 'WhisperX 强制对齐' },
  { value: 'qwen-forced', label: 'Qwen 强制对齐' },
  { value: 'none', label: '禁用' },
]

export function AlignStep({
  config,
  setField,
  expanded,
  onToggle,
  currentProvider,
  qwenAlignerInstalled,
  alignHint,
  alignStatus,
}: AlignStepProps) {
  return (
    <StepCard
      index="03"
      title="时间轴对齐"
      description="根据当前 ASR Provider 和对齐模型状态决定是否精修时间戳。"
      statusLabel={alignStatus.label}
      tone={alignStatus.tone}
      pills={[
        config.whisper.align_provider,
        `当前 ASR ${currentProvider}`,
        qwenAlignerInstalled ? 'Qwen 对齐已下载' : 'Qwen 对齐未下载',
      ]}
      expanded={expanded}
      onToggle={onToggle}
      basicContent={
        <div className="field-grid">
          <label>
            <span>对齐方式</span>
            <select
              value={config.whisper.align_provider}
              onChange={(event) =>
                setField('whisper', 'align_provider', event.target.value as AlignProvider)
              }
            >
              {alignProviderOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <div
            className={`alert ${
              alignHint.level === 'warning'
                ? 'warning'
                : alignHint.level === 'success'
                  ? 'success'
                  : 'card-muted'
            }`}
          >
            {alignHint.text}
            {config.whisper.align_provider === 'qwen-forced' || alignHint.level === 'warning' ? (
              <span className="inline-link">
                {' '}
                <Link to="/models">前往下载</Link>
              </span>
            ) : null}
          </div>
          <div className="field-block">
            <span className="field-label">对齐说明</span>
            <span className="muted">
              手动选择 WhisperX 时，也可以对非 WhisperX 的识别结果做二次时间轴对齐；`auto`
              模式不会自动这样做。
            </span>
          </div>
        </div>
      }
    />
  )
}
