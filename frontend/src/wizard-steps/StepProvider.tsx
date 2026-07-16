import { AppConfig, bilingualModeOptions, sourceLanguageOptions } from '../api'
import { GroupName } from './types'

interface StepProviderProps {
  config: AppConfig
  setField: <T extends GroupName, K extends keyof AppConfig[T]>(group: T, key: K, value: AppConfig[T][K]) => void
  onPrev: () => void
  onNext: () => void
}

export function StepProvider({ config, setField, onPrev, onNext }: StepProviderProps) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>步骤 3：语言与字幕偏好</h2>
          <p>这里的源语言会同时用于 Whisper 识别提示和字幕命名。</p>
        </div>
      </div>
      <div className="field-grid">
        <label>
          <span>视频源语言</span>
          <select
            value={config.subtitle.source_language}
            onChange={(event) =>
              setField('subtitle', 'source_language', event.target.value as AppConfig['subtitle']['source_language'])
            }
          >
            {sourceLanguageOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <span className="muted">无法确定时建议保留自动检测；选错语言可能影响识别效果。</span>
        </label>
        <label className="switch-row">
          <span>双语字幕</span>
          <input
            type="checkbox"
            checked={config.subtitle.bilingual}
            onChange={(event) => setField('subtitle', 'bilingual', event.target.checked)}
          />
        </label>
        {config.subtitle.bilingual ? (
          <label>
            <span>双语模式</span>
            <select
              value={config.subtitle.bilingual_mode}
              onChange={(event) =>
                setField('subtitle', 'bilingual_mode', event.target.value as AppConfig['subtitle']['bilingual_mode'])
              }
            >
              {bilingualModeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>
      <div className="page-actions">
        <button onClick={onPrev}>上一步</button>
        <button onClick={onNext}>下一步</button>
      </div>
    </div>
  )
}
