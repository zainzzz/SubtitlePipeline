import { AppConfig, bilingualModeOptions } from '../../api'
import { StepCard } from '../SettingsWidgets'
import { SettingsStepProps } from './types'

export function SubtitleStep({ config, setField, expanded, onToggle }: SettingsStepProps) {
  return (
    <StepCard
      index="05"
      title="字幕输出"
      description="定义双语策略与输出文件命名模板。"
      statusLabel={config.subtitle.bilingual ? '双语输出' : '单语输出'}
      tone="success"
      pills={[
        config.subtitle.bilingual ? '双语' : '单语',
        config.subtitle.bilingual_mode,
        config.subtitle.filename_template,
      ]}
      expanded={expanded}
      onToggle={onToggle}
      basicContent={
        <div className="field-grid">
          <label className="switch-row">
            <span>双语字幕</span>
            <input
              type="checkbox"
              checked={config.subtitle.bilingual}
              onChange={(event) => setField('subtitle', 'bilingual', event.target.checked)}
            />
          </label>
          <label>
            <span>双语模式</span>
            <select
              value={config.subtitle.bilingual_mode}
              disabled={!config.subtitle.bilingual}
              onChange={(event) =>
                setField(
                  'subtitle',
                  'bilingual_mode',
                  event.target.value as AppConfig['subtitle']['bilingual_mode'],
                )
              }
            >
              {bilingualModeOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      }
      advancedContent={
        <section className="advanced-section">
          <div className="field-grid">
            <div className="field-block pipeline-wide">
              <span className="field-label">文件名模板</span>
              <input
                value={config.subtitle.filename_template}
                onChange={(event) =>
                  setField('subtitle', 'filename_template', event.target.value)
                }
              />
              <span className="muted">
                {'可用占位符：{stem} = 源文件名（不含扩展名），{lang} = 语言代码或 bilingual / source'}
              </span>
            </div>
          </div>
        </section>
      }
    />
  )
}
