import { StepCard } from '../SettingsWidgets'
import { SettingsStepProps } from './types'

export function MuxStep({ config, setField, expanded, onToggle }: SettingsStepProps) {
  return (
    <StepCard
      index="06"
      title="字幕压片"
      description="是否将字幕封装回视频容器。通常作为可选收尾步骤。"
      statusLabel={config.mux.enabled ? '已启用' : '可选步骤'}
      tone={config.mux.enabled ? 'neutral' : 'muted'}
      pills={[
        config.mux.enabled ? '已启用' : '已关闭',
        config.file.output_to_source_dir ? '跟随源目录' : '统一输出',
        config.mux.filename_template,
      ]}
      expanded={expanded}
      onToggle={onToggle}
      headerActions={
        <label className="switch-row">
          <span>启用</span>
          <input
            type="checkbox"
            checked={config.mux.enabled}
            onChange={(event) => setField('mux', 'enabled', event.target.checked)}
          />
        </label>
      }
      basicContent={
        <div className={config.mux.enabled ? '' : 'disabled-section'}>
          <div className="field-grid">
            <div className="field-block">
              <span className="field-label">输出位置</span>
              <span className="muted">
                {config.file.output_to_source_dir
                  ? '当前跟随源文件目录输出。'
                  : '当前统一输出到 /output 目录。'}
              </span>
            </div>
          </div>
        </div>
      }
      advancedContent={
        <div className={config.mux.enabled ? '' : 'disabled-section'}>
          <section className="advanced-section">
            <div className="field-grid">
              <div className="field-block pipeline-wide">
                <span className="field-label">压片文件名模板</span>
                <input
                  disabled={!config.mux.enabled}
                  value={config.mux.filename_template}
                  onChange={(event) => setField('mux', 'filename_template', event.target.value)}
                />
                <span className="muted">{'可用占位符：{stem} = 源文件名（不含扩展名）'}</span>
              </div>
            </div>
          </section>
        </div>
      }
    />
  )
}
