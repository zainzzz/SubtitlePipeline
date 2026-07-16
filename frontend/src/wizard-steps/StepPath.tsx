import { DirectoryPicker } from '../components/DirectoryPicker'
import { AppConfig } from '../api'
import { GroupName } from './types'

interface StepPathProps {
  config: AppConfig
  setField: <T extends GroupName, K extends keyof AppConfig[T]>(group: T, key: K, value: AppConfig[T][K]) => void
  onNext: () => void
}

export function StepPath({ config, setField, onNext }: StepPathProps) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>步骤 1：路径配置</h2>
          <p>先确认扫描目录与输出位置，后续字幕和压片都会沿用这里的设置。</p>
        </div>
      </div>
      <div className="field-grid">
        <DirectoryPicker
          label="输入目录"
          value={config.file.input_dir}
          onChange={(value) => setField('file', 'input_dir', value)}
          placeholder="例如 /data"
        />
        <div className="field-block">
          <label className="switch-row">
            <span>输出到源文件目录</span>
            <input
              type="checkbox"
              checked={config.file.output_to_source_dir}
              onChange={(event) => setField('file', 'output_to_source_dir', event.target.checked)}
            />
          </label>
          <span className="muted">
            {config.file.output_to_source_dir
              ? '字幕和压片结果会直接写回源视频所在目录。'
              : '字幕和压片结果会统一输出到 /output 目录。'}
          </span>
        </div>
      </div>
      <div className="page-actions">
        <button disabled>上一步</button>
        <button onClick={onNext}>下一步</button>
      </div>
    </div>
  )
}
