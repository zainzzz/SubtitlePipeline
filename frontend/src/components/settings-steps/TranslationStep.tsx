import {
  AppConfig,
  llmTypeOptions,
  translationContentTypeOptions,
} from '../../api'
import { addToList, getTranslationStatus, removeFromList, StepCard, TagEditor } from '../SettingsWidgets'
import { TranslationStepProps } from './types'

export function TranslationStep({
  config,
  setField,
  expanded,
  onToggle,
  testing,
  setLLMType,
  onTestTranslation,
}: TranslationStepProps) {
  const translationStatus = getTranslationStatus(config)
  const usingCustomPrompt = config.translation.custom_prompt.trim().length > 0

  const addTargetLanguage = (value: string) =>
    setField('translation', 'target_languages', addToList(config.translation.target_languages, value))
  const removeTargetLanguage = (value: string) =>
    setField('translation', 'target_languages', removeFromList(config.translation.target_languages, value))

  return (
    <StepCard
      index="04"
      title="翻译"
      description="配置翻译服务、目标语言与内容风格。关闭后会保留参数但跳过翻译阶段。"
      statusLabel={translationStatus.label}
      tone={translationStatus.tone}
      pills={[
        config.translation.enabled ? '已启用' : '已关闭',
        config.translation.llm_type,
        config.translation.target_languages.join(', ') || '未设置目标语言',
        config.translation.model || '未设置模型',
      ]}
      expanded={expanded}
      onToggle={onToggle}
      headerActions={
        <label className="switch-row">
          <span>启用</span>
          <input
            type="checkbox"
            checked={config.translation.enabled}
            onChange={(event) => setField('translation', 'enabled', event.target.checked)}
          />
        </label>
      }
      basicContent={
        <div className={config.translation.enabled ? '' : 'disabled-section'}>
          <div className="field-grid">
            <label>
              <span>LLM 类型</span>
              <select
                disabled={!config.translation.enabled}
                value={config.translation.llm_type}
                onChange={(event) =>
                  setLLMType(event.target.value as AppConfig['translation']['llm_type'])
                }
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
            <TagEditor
              label="目标语言代码"
              values={config.translation.target_languages}
              placeholder="例如 zh、en、ja"
              hint="建议优先填写媒体库识别更友好的语言代码，例如 zh、en、ja；如确有需要也可手动填写 zh-CN 这类地区代码。"
              disabled={!config.translation.enabled}
              onAdd={addTargetLanguage}
              onRemove={removeTargetLanguage}
            />
          </div>
          <button
            disabled={testing || !config.translation.enabled}
            onClick={onTestTranslation}
          >
            {testing ? '测试中…' : '测试连接'}
          </button>
        </div>
      }
      advancedContent={
        <div className={config.translation.enabled ? '' : 'disabled-section'}>
          <section className="advanced-section">
            <div className="field-grid">
              <label>
                <span>超时（秒）</span>
                <input
                  disabled={!config.translation.enabled}
                  type="number"
                  value={config.translation.timeout_seconds}
                  onChange={(event) =>
                    setField('translation', 'timeout_seconds', Number(event.target.value))
                  }
                />
              </label>
              <label>
                <span>最大重试</span>
                <input
                  disabled={!config.translation.enabled}
                  type="number"
                  value={config.translation.max_retries}
                  onChange={(event) =>
                    setField('translation', 'max_retries', Number(event.target.value))
                  }
                />
              </label>
              <label>
                <span>内容类型</span>
                <select
                  disabled={!config.translation.enabled || usingCustomPrompt}
                  value={config.translation.content_type}
                  onChange={(event) =>
                    setField(
                      'translation',
                      'content_type',
                      event.target.value as AppConfig['translation']['content_type'],
                    )
                  }
                >
                  {translationContentTypeOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <div className="field-block pipeline-wide">
                <span className="field-label">自定义 Prompt</span>
                <textarea
                  disabled={!config.translation.enabled}
                  rows={5}
                  value={config.translation.custom_prompt}
                  placeholder="留空使用预设，填写后将替换预设 prompt"
                  onChange={(event) =>
                    setField('translation', 'custom_prompt', event.target.value)
                  }
                />
                <span className="muted">
                  {usingCustomPrompt
                    ? '当前已启用自定义 prompt，内容类型预设已禁用。'
                    : '留空时使用上方内容类型预设。'}
                </span>
              </div>
            </div>
          </section>
        </div>
      }
    />
  )
}
