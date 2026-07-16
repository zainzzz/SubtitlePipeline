import { useEffect, useMemo, useState } from 'react'

import {
  AppConfig,
  cloneConfig,
  defaultAppConfig,
  getConfig,
  getModels,
  llmTypeOptions,
  ModelItem,
  ModelListResponse,
  sourceLanguageOptions,
  testTranslation,
  updateConfig,
} from '../api'
import {
  countChangedSections,
  getAlignStatus,
} from '../components/SettingsWidgets'
import { AlignStep } from '../components/settings-steps/AlignStep'
import { AsrStep } from '../components/settings-steps/AsrStep'
import { FileScanStep } from '../components/settings-steps/FileScanStep'
import { MuxStep } from '../components/settings-steps/MuxStep'
import { SubtitleStep } from '../components/settings-steps/SubtitleStep'
import { SystemStep } from '../components/settings-steps/SystemStep'
import { TranslationStep } from '../components/settings-steps/TranslationStep'
import {
  SetFieldFn,
} from '../components/settings-steps/types'

const initialExpandedState: Record<string, boolean> = {
  file: false,
  asr: false,
  align: false,
  translation: false,
  subtitle: false,
  mux: false,
  system: false,
}

export function SettingsPage() {
  const [config, setConfig] = useState<AppConfig>(cloneConfig(defaultAppConfig))
  const [loadedConfig, setLoadedConfig] = useState<AppConfig>(cloneConfig(defaultAppConfig))
  const [models, setModels] = useState<ModelListResponse>({ items: [], current_model: '' })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>(initialExpandedState)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const [nextConfig, nextModels] = await Promise.all([getConfig(), getModels()])
      setConfig(nextConfig)
      setLoadedConfig(nextConfig)
      setModels(nextModels)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '配置加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const setField: SetFieldFn = (group, key, value) => {
    setConfig((current) => ({
      ...current,
      [group]: {
        ...current[group],
        [key]: value,
      },
    }))
  }

  const setLLMType = (value: AppConfig['translation']['llm_type']) => {
    const option = llmTypeOptions.find((item) => item.value === value)
    setConfig((current) => ({
      ...current,
      translation: {
        ...current.translation,
        llm_type: value,
        api_base_url: option?.defaultBaseUrl || current.translation.api_base_url,
      },
    }))
  }

  // ───── Derived ASR / align state ─────
  const installedAsrModels = useMemo(
    () => models.items.filter((item) => item.model_type === 'asr' && item.status === 'installed'),
    [models.items],
  )
  const selectedAsrModel = useMemo<ModelItem | null>(
    () => models.items.find((item) => item.name === config.whisper.model_name) || null,
    [config.whisper.model_name, models.items],
  )
  const qwenAligner = useMemo<ModelItem | null>(
    () => models.items.find((item) => item.name === 'qwen3-forced-aligner') || null,
    [models.items],
  )

  const changedSectionCount = useMemo(
    () => countChangedSections(config, loadedConfig),
    [config, loadedConfig],
  )
  const sourceLanguageLabel = useMemo(
    () =>
      sourceLanguageOptions.find((option) => option.value === config.subtitle.source_language)?.label ||
      config.subtitle.source_language,
    [config.subtitle.source_language],
  )

  /**
   * P1-3 fix: alignHint now reads `selectedAsrModel` (object) and `qwenAligner` (object)
   * directly so the memo always recomputes when the underlying model objects change,
   * even if previously-derived primitives stayed equal. Computing `currentProvider`
   * and `qwenAlignerInstalled` inside the memo guarantees no stale closure.
   */
  const alignHint = useMemo(() => {
    const currentProvider = selectedAsrModel?.provider ?? config.whisper.provider
    const qwenAlignerInstalled = qwenAligner?.status === 'installed'
    if (config.whisper.align_provider === 'auto' && currentProvider === 'whisperx') {
      return { level: 'success' as const, text: '当前将使用 WhisperX 内置对齐' }
    }
    if (config.whisper.align_provider === 'auto' && currentProvider !== 'whisperx' && !qwenAlignerInstalled) {
      return { level: 'warning' as const, text: '建议下载 Qwen3 强制对齐模型以提升精度' }
    }
    if (config.whisper.align_provider === 'qwen-forced' && qwenAlignerInstalled) {
      return { level: 'success' as const, text: 'Qwen3-ForcedAligner 已就绪' }
    }
    if (config.whisper.align_provider === 'qwen-forced' && !qwenAlignerInstalled) {
      return { level: 'warning' as const, text: '模型未下载，任务将报错停止' }
    }
    if (config.whisper.align_provider === 'none') {
      return { level: 'muted' as const, text: '将直接使用 ASR 内置时间戳' }
    }
    return {
      level: 'muted' as const,
      text: '自动模式会根据当前 ASR Provider 和本地模型状态选择对齐器',
    }
  }, [config.whisper.align_provider, config.whisper.provider, selectedAsrModel, qwenAligner])

  const currentProvider = selectedAsrModel?.provider ?? config.whisper.provider
  const qwenAlignerInstalled = qwenAligner?.status === 'installed'

  const alignStatus = useMemo(
    () => getAlignStatus(config.whisper.align_provider, alignHint.level),
    [alignHint.level, config.whisper.align_provider],
  )

  /**
   * P1-4 fix: overviewStats now depends on the full `selectedAsrModel` object
   * (not just `selectedAsrModel?.display_name`) so updates to model status / provider
   * propagate to the overview even when the display name is unchanged.
   */
  const overviewStats = useMemo(
    () => [
      { label: '流水线步骤', value: '7', hint: '扫描到系统参数' },
      {
        label: '未保存变更',
        value: String(changedSectionCount),
        hint: changedSectionCount > 0 ? '建议保存后生效' : '当前与已保存一致',
      },
      {
        label: '已选模型',
        value: selectedAsrModel?.display_name || config.whisper.model_name,
        hint: currentProvider,
      },
      { label: '对齐状态', value: alignStatus.label, hint: alignHint.text },
    ],
    [
      alignHint.text,
      alignStatus.label,
      changedSectionCount,
      config.whisper.model_name,
      currentProvider,
      selectedAsrModel, // P1-4: full object, not just display_name
    ],
  )

  const toggleExpanded = (key: string) => {
    setExpanded((current) => ({ ...current, [key]: !current[key] }))
  }

  const handleSelectAsrModel = (modelName: string) => {
    const nextModel = models.items.find((item) => item.name === modelName && item.model_type === 'asr')
    if (!nextModel) {
      return
    }
    setConfig((current) => ({
      ...current,
      whisper: {
        ...current.whisper,
        model_name: nextModel.name,
        provider: nextModel.provider === 'qwen-forced' ? current.whisper.provider : nextModel.provider,
      },
    }))
  }

  const submit = async () => {
    setSaving(true)
    try {
      const updated = await updateConfig({
        file: config.file,
        processing: config.processing,
        whisper: config.whisper,
        translation: config.translation,
        subtitle: config.subtitle,
        mux: config.mux,
        logging: config.logging,
      })
      setConfig(updated)
      setLoadedConfig(updated)
      setMessage('设置已保存')
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '设置保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    const next = cloneConfig(defaultAppConfig)
    setConfig(next)
    setMessage('已恢复默认值，可继续保存生效')
    setError('')
  }

  const handleRestoreLoaded = () => {
    setConfig(cloneConfig(loadedConfig))
    setMessage('已恢复当前已保存配置')
    setError('')
  }

  const handleTestTranslation = async () => {
    setTesting(true)
    try {
      const result = await testTranslation({
        enabled: config.translation.enabled,
        llm_type: config.translation.llm_type,
        api_base_url: config.translation.api_base_url,
        api_key: config.translation.api_key,
        model: config.translation.model,
        timeout_seconds: config.translation.timeout_seconds,
        target_language: config.translation.target_languages[0] || 'zh',
        content_type: config.translation.content_type,
        custom_prompt: config.translation.custom_prompt,
      })
      if (result.success) {
        setMessage(result.message)
        setError('')
      } else {
        setError(result.message)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '翻译连接测试失败')
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return <div className="card muted settings-loading">配置加载中…</div>
  }

  const sharedStepProps = { config, setField }

  return (
    <section className="settings-page">
      <header className="page-header settings-header">
        <div className="settings-header-main">
          <h1>设置</h1>
          <p>管理扫描目录、识别模型、时间轴对齐、翻译和输出设置。</p>
        </div>
        <div className="inline-actions settings-top-actions">
          <button className="ghost-button" onClick={handleRestoreLoaded}>恢复已保存</button>
          <button className="ghost-button" onClick={() => void load()}>重新加载</button>
        </div>
      </header>
      {message ? <div className="alert success">{message}</div> : null}
      {error ? <div className="alert error">{error}</div> : null}
      {config.meta?.restart_required ? (
        <div className="alert warning">检测到系统级配置更新，需要重启 Scanner / Worker。</div>
      ) : null}

      <section className="settings-overview card">
        <div className="settings-overview-main">
          <div className="settings-overview-copy">
            <div className="settings-overview-badges">
              <span className="summary-pill emphasis">配置概览</span>
              <span className={`step-badge ${changedSectionCount > 0 ? 'tone-warning' : 'tone-success'}`}>
                {changedSectionCount > 0 ? `未保存 ${changedSectionCount} 项` : '已同步'}
              </span>
            </div>
            <h2>当前配置概览</h2>
            <p>汇总当前模型、对齐状态和未保存改动，便于保存前快速检查。</p>
          </div>
          <div className="settings-overview-stats">
            {overviewStats.map((stat) => (
              <div key={stat.label} className="overview-stat">
                <span className="overview-stat-label">{stat.label}</span>
                <strong className="overview-stat-value">{stat.value}</strong>
                <span className="overview-stat-hint">{stat.hint}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <div className="pipeline-steps">
        <FileScanStep
          {...sharedStepProps}
          expanded={expanded.file}
          onToggle={() => toggleExpanded('file')}
        />
        <AsrStep
          {...sharedStepProps}
          expanded={expanded.asr}
          onToggle={() => toggleExpanded('asr')}
          models={models}
          installedAsrModels={installedAsrModels}
          selectedAsrModel={selectedAsrModel}
          currentProvider={currentProvider}
          sourceLanguageLabel={sourceLanguageLabel}
          onSelectAsrModel={handleSelectAsrModel}
        />
        <AlignStep
          {...sharedStepProps}
          expanded={expanded.align}
          onToggle={() => toggleExpanded('align')}
          currentProvider={currentProvider}
          qwenAlignerInstalled={qwenAlignerInstalled}
          alignHint={alignHint}
          alignStatus={alignStatus}
        />
        <TranslationStep
          {...sharedStepProps}
          expanded={expanded.translation}
          onToggle={() => toggleExpanded('translation')}
          testing={testing}
          setLLMType={setLLMType}
          onTestTranslation={() => void handleTestTranslation()}
        />
        <SubtitleStep
          {...sharedStepProps}
          expanded={expanded.subtitle}
          onToggle={() => toggleExpanded('subtitle')}
        />
        <MuxStep
          {...sharedStepProps}
          expanded={expanded.mux}
          onToggle={() => toggleExpanded('mux')}
        />
        <SystemStep
          {...sharedStepProps}
          expanded={expanded.system}
          onToggle={() => toggleExpanded('system')}
        />
      </div>

      <div className="page-actions settings-action-bar">
        <div className="settings-action-meta">
          <span className="settings-action-title">配置变更</span>
          <span className="muted">
            {changedSectionCount > 0
              ? `当前有 ${changedSectionCount} 个分组未保存`
              : '当前没有未保存改动'}
          </span>
        </div>
        <div className="inline-actions">
          <button className="ghost-button" onClick={handleReset}>重置默认</button>
          <button disabled={saving} onClick={() => void submit()}>
            {saving ? '保存中…' : '保存设置'}
          </button>
        </div>
      </div>
    </section>
  )
}
