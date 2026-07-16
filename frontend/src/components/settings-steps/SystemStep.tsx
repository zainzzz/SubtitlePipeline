import { AppConfig, retryModeOptions } from '../../api'
import { StepCard } from '../SettingsWidgets'
import { SettingsStepProps } from './types'

export function SystemStep({ config, setField, expanded, onToggle }: SettingsStepProps) {
  return (
    <StepCard
      index="07"
      title="系统参数"
      description="放置工作目录、自动重试与轮询间隔等运行时高级设置。"
      statusLabel="高级参数"
      tone="neutral"
      pills={[
        config.processing.work_dir,
        config.processing.retry_mode,
        `轮询 ${config.processing.poll_interval_seconds}s`,
        config.logging.level,
      ]}
      expanded={expanded}
      onToggle={onToggle}
      basicContent={
        <div className="field-grid">
          <label>
            <span>工作目录</span>
            <input
              value={config.processing.work_dir}
              onChange={(event) => setField('processing', 'work_dir', event.target.value)}
            />
          </label>
          <label>
            <span>自动重试模式</span>
            <select
              value={config.processing.retry_mode}
              onChange={(event) =>
                setField(
                  'processing',
                  'retry_mode',
                  event.target.value as AppConfig['processing']['retry_mode'],
                )
              }
            >
              {retryModeOptions.map((option) => (
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
            <label>
              <span>任务最大重试</span>
              <input
                type="number"
                value={config.processing.max_retries}
                onChange={(event) =>
                  setField('processing', 'max_retries', Number(event.target.value))
                }
              />
            </label>
            <label>
              <span>轮询间隔（秒）</span>
              <input
                type="number"
                value={config.processing.poll_interval_seconds}
                onChange={(event) =>
                  setField('processing', 'poll_interval_seconds', Number(event.target.value))
                }
              />
            </label>
            <label className="switch-row">
              <span>保留中间产物</span>
              <input
                type="checkbox"
                checked={config.processing.keep_intermediates}
                onChange={(event) =>
                  setField('processing', 'keep_intermediates', event.target.checked)
                }
              />
            </label>
            <label>
              <span>日志级别</span>
              <select
                value={config.logging.level}
                onChange={(event) => setField('logging', 'level', event.target.value)}
              >
                <option value="INFO">INFO</option>
                <option value="WARNING">WARNING</option>
                <option value="ERROR">ERROR</option>
              </select>
            </label>
          </div>
        </section>
      }
    />
  )
}
