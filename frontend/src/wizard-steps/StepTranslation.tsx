import { AppConfig, llmTypeOptions, translationContentTypeOptions } from '../api'
import { GroupName } from './types'

interface StepTranslationProps {
  config: AppConfig
  setField: <T extends GroupName, K extends keyof AppConfig[T]>(group: T, key: K, value: AppConfig[T][K]) => void
  setLLMType: (value: AppConfig['translation']['llm_type']) => void
  testing: boolean
  onTest: () => void
  onPrev: () => void
  onNext: () => void
}

export function StepTranslation({
  config,
  setField,
  setLLMType,
  testing,
  onTest,
  onPrev,
  onNext,
}: StepTranslationProps) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>步骤 4：翻译配置</h2>
          <p>翻译配置为可选项，关闭后系统只生成源语言字幕。</p>
        </div>
        <label className="switch-row">
          <span>启用翻译</span>
          <input
            type="checkbox"
            checked={config.translation.enabled}
            onChange={(event) => setField('translation', 'enabled', event.target.checked)}
          />
        </label>
      </div>
      <div className={`field-grid ${config.translation.enabled ? '' : 'disabled-section'}`}>
        <label>
          <span>LLM 类型</span>
          <select
            disabled={!config.translation.enabled}
            value={config.translation.llm_type}
            onChange={(event) => setLLMType(event.target.value as AppConfig['translation']['llm_type'])}
          >
            {llmTypeOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>API Base URL</span>
          <input
            disabled={!config.translation.enabled}
            value={config.translation.api_base_url}
            onChange={(event) => setField('translation', 'api_base_url', event.target.value)}
          />
        </label>
        <label>
          <span>API Key</span>
          <input
            disabled={!config.translation.enabled}
            type="password"
            value={config.translation.api_key}
            onChange={(event) => setField('translation', 'api_key', event.target.value)}
          />
        </label>
        <label>
          <span>模型</span>
          <input
            disabled={!config.translation.enabled}
            value={config.translation.model}
            onChange={(event) => setField('translation', 'model', event.target.value)}
          />
        </label>
        <label>
          <span>目标语言</span>
          <input
            disabled={!config.translation.enabled}
            value={config.translation.target_languages.join(', ')}
            onChange={(event) =>
              setField(
                'translation',
                'target_languages',
                event.target.value
                  .split(',')
                  .map((item) => item.trim())
                  .filter(Boolean),
              )
            }
          />
        </label>
        <label>
          <span>内容类型</span>
          <select
            disabled={!config.translation.enabled}
            value={config.translation.content_type}
            onChange={(event) =>
              setField('translation', 'content_type', event.target.value as AppConfig['translation']['content_type'])
            }
          >
            {translationContentTypeOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="page-actions">
        <button onClick={onPrev}>上一步</button>
        <div className="inline-actions">
          <button disabled={testing} onClick={onTest}>
            {testing ? '测试中…' : '测试连接'}
          </button>
          <button onClick={onNext}>下一步</button>
        </div>
      </div>
    </div>
  )
}
