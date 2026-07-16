import { useMemo } from 'react'

import { DirectoryPicker } from '../DirectoryPicker'
import { addToList, removeFromList, StepCard, TagEditor } from '../SettingsWidgets'
import { SettingsStepProps, StatusBadge } from './types'

export function FileScanStep({ config, setField, expanded, onToggle }: SettingsStepProps) {
  const fileStatus = useMemo<StatusBadge>(
    () =>
      config.file.input_dir.trim() && config.file.allowed_extensions.length > 0
        ? { tone: 'success', label: '已配置' }
        : { tone: 'warning', label: '待补充' },
    [config.file.allowed_extensions.length, config.file.input_dir],
  )

  const fileDescription = useMemo(
    () =>
      config.file.output_to_source_dir
        ? '扫描媒体目录并直接输出回源文件位置。'
        : '扫描媒体目录并统一输出到 /output。',
    [config.file.output_to_source_dir],
  )

  const addFileExtension = (value: string) =>
    setField('file', 'allowed_extensions', addToList(config.file.allowed_extensions, value))
  const removeFileExtension = (value: string) =>
    setField('file', 'allowed_extensions', removeFromList(config.file.allowed_extensions, value))
  const addInputDir = (value: string) =>
    setField('file', 'input_dirs', addToList(config.file.input_dirs, value))
  const removeInputDir = (value: string) =>
    setField('file', 'input_dirs', removeFromList(config.file.input_dirs, value))
  const addExcludeDir = (value: string) =>
    setField('file', 'exclude_dirs', addToList(config.file.exclude_dirs, value))
  const removeExcludeDir = (value: string) =>
    setField('file', 'exclude_dirs', removeFromList(config.file.exclude_dirs, value))

  return (
    <StepCard
      index="01"
      title="文件扫描"
      description={fileDescription}
      statusLabel={fileStatus.label}
      tone={fileStatus.tone}
      pills={[
        config.file.input_dir,
        ...config.file.allowed_extensions.slice(0, 3),
        config.file.output_to_source_dir ? '输出回源' : '输出 /output',
      ]}
      expanded={expanded}
      onToggle={onToggle}
      basicContent={
        <div className="field-grid">
          <DirectoryPicker
            label="输入目录"
            value={config.file.input_dir}
            onChange={(value) => setField('file', 'input_dir', value)}
            placeholder="例如 /data"
          />
          <TagEditor
            label="允许文件类型"
            values={config.file.allowed_extensions}
            placeholder="例如 .mp4"
            onAdd={addFileExtension}
            onRemove={removeFileExtension}
          />
          <TagEditor
            label="额外扫描目录（可选，留空则只用输入目录）"
            values={config.file.input_dirs}
            placeholder="例如 /media/movies"
            onAdd={addInputDir}
            onRemove={removeInputDir}
          />
          <TagEditor
            label="排除目录名（支持通配符 *，如 预告*、Sample、@eaDir）"
            values={config.file.exclude_dirs}
            placeholder="例如 Sample"
            onAdd={addExcludeDir}
            onRemove={removeExcludeDir}
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
                ? '字幕和压片文件会写回源视频所在目录。'
                : '字幕和压片文件会统一输出到 /output 目录。'}
            </span>
          </div>
        </div>
      }
      advancedContent={
        <section className="advanced-section">
          <div className="field-grid">
            <label>
              <span>扫描间隔（秒）</span>
              <input
                type="number"
                value={config.file.scan_interval_seconds}
                onChange={(event) => setField('file', 'scan_interval_seconds', Number(event.target.value))}
              />
            </label>
            <label>
              <span>最小文件（MB）</span>
              <input
                type="number"
                value={config.file.min_size_mb}
                onChange={(event) => setField('file', 'min_size_mb', Number(event.target.value))}
              />
            </label>
            <label>
              <span>最大文件（MB）</span>
              <input
                type="number"
                value={config.file.max_size_mb}
                onChange={(event) => setField('file', 'max_size_mb', Number(event.target.value))}
              />
            </label>
            <label>
              <span>最大排队任务数</span>
              <input
                type="number"
                value={config.file.max_pending_tasks}
                onChange={(event) => setField('file', 'max_pending_tasks', Number(event.target.value))}
              />
            </label>
            <div className="field-block">
              <label className="switch-row">
                <span>启用扫描</span>
                <input
                  type="checkbox"
                  checked={config.file.scan_enabled}
                  onChange={(event) => setField('file', 'scan_enabled', event.target.checked)}
                />
              </label>
              <span className="muted">关闭后扫描器暂停，不再发现新文件入队。</span>
            </div>
          </div>
        </section>
      }
    />
  )
}
