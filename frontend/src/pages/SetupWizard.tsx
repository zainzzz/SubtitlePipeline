import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  activateModel,
  AppConfig,
  bilingualModeOptions,
  cloneConfig,
  defaultAppConfig,
  downloadModel,
  getConfig,
  getModels,
  getSystemStatus,
  llmTypeOptions,
  ModelListResponse,
  setSetupComplete,
  sourceLanguageOptions,
  SystemStatus,
  testTranslation,
  translationContentTypeOptions,
  updateConfig,
} from '../api'
import { usePolling } from '../hooks'
import { StepComplete } from '../wizard-steps/StepComplete'
import { StepModel } from '../wizard-steps/StepModel'
import { StepPath } from '../wizard-steps/StepPath'
import { StepProvider } from '../wizard-steps/StepProvider'
import { StepTranslation } from '../wizard-steps/StepTranslation'
import { GroupName } from '../wizard-steps/types'

export function SetupWizard({
  onCompleted,
}: {
  onCompleted: () => Promise<void> | void
}) {
  const [step, setStep] = useState(1)
  const [config, setConfig] = useState<AppConfig>(cloneConfig(defaultAppConfig))
  const [models, setModels] = useState<ModelListResponse>({ items: [], current_model: '' })
  const [systemStatus, setSystemStatus] = useState<SystemStatus>({
    setup_complete: false,
    asr_ready: false,
    translation_ready: false,
    current_model: '',
    proxy: {
      http_proxy: null,
      https_proxy: null,
      hf_endpoint: null,
    },
  })
  const [selectedModel, setSelectedModel] = useState('')
  const [loading, setLoading] = useState(true)
  const [testing, setTesting] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const navigate = useNavigate()

  const load = async () => {
    setLoading(true)
    try {
      const [nextConfig, nextModels, nextStatus] = await Promise.all([getConfig(), getModels(), getSystemStatus()])
      setConfig(nextConfig)
      setModels(nextModels)
      setSystemStatus(nextStatus)
      setSelectedModel(
        (current) =>
          current || nextModels.current_model || nextModels.items.find((item) => item.status === 'installed')?.name || 'whisperx-small',
      )
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '初始化信息读取失败')
    } finally {
      setLoading(false)
    }
  }

  const isDownloading = models.items.some((item) => item.status === 'downloading')

  const pollModels = useCallback(async () => {
    try {
      const nextModels = await getModels()
      setModels(nextModels)
    } catch {
      // ignore polling errors
    }
  }, [])

  usePolling(pollModels, 2000, [step, isDownloading], step === 2 && isDownloading)

  useEffect(() => {
    void load()
  }, [])

  const setField = useCallback(
    <T extends GroupName, K extends keyof AppConfig[T]>(group: T, key: K, value: AppConfig[T][K]) => {
      setConfig((current) => ({
        ...current,
        [group]: {
          ...current[group],
          [key]: value,
        },
      }))
    },
    [],
  )

  const setLLMType = useCallback((value: AppConfig['translation']['llm_type']) => {
    const option = llmTypeOptions.find((item) => item.value === value)
    setConfig((current) => ({
      ...current,
      translation: {
        ...current.translation,
        llm_type: value,
        api_base_url: option?.defaultBaseUrl || current.translation.api_base_url,
      },
    }))
  }, [])

  const selectedModelItem = useMemo(
    () => models.items.find((item) => item.name === selectedModel),
    [models.items, selectedModel],
  )
  const canMoveFromModelStep = Boolean(selectedModelItem && selectedModelItem.status === 'installed')
  const outputModeLabel = config.file.output_to_source_dir ? '源文件目录' : '/output 目录'
  const sourceLanguageLabel =
    sourceLanguageOptions.find((option) => option.value === config.subtitle.source_language)?.label || config.subtitle.source_language
  const bilingualModeLabel =
    bilingualModeOptions.find((option) => option.value === config.subtitle.bilingual_mode)?.label || config.subtitle.bilingual_mode
  const translationContentTypeLabel =
    translationContentTypeOptions.find((option) => option.value === config.translation.content_type)?.label || config.translation.content_type
  const proxyItems = useMemo(
    () => [
      { label: 'HTTP 代理', value: systemStatus.proxy.http_proxy },
      { label: 'HTTPS 代理', value: systemStatus.proxy.https_proxy },
      { label: 'HuggingFace 镜像', value: systemStatus.proxy.hf_endpoint },
    ],
    [systemStatus.proxy.http_proxy, systemStatus.proxy.https_proxy, systemStatus.proxy.hf_endpoint],
  )
  const proxyConfigured = proxyItems.some((item) => Boolean(item.value))

  const handleDownload = async (name: string) => {
    try {
      const result = await downloadModel(name)
      setMessage(result.message)
      setError('')
      setSelectedModel(name)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '模型下载启动失败')
    }
  }

  const handleTest = async () => {
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

  const completeSetup = async () => {
    if (!selectedModel) {
      setError('请先选择已安装模型')
      return
    }
    setFinishing(true)
    try {
      if (!selectedModelItem || selectedModelItem.status !== 'installed') {
        throw new Error('所选模型尚未安装完成')
      }
      await activateModel(selectedModel)
      await updateConfig({
        file: config.file,
        subtitle: config.subtitle,
        translation: config.translation,
      })
      await setSetupComplete(true)
      await onCompleted()
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : '初始化完成失败')
    } finally {
      setFinishing(false)
    }
  }

  if (loading) {
    return <div className="card muted">引导信息加载中…</div>
  }

  return (
    <section className="wizard-shell">
      <header className="wizard-header">
        <div>
          <h1>首次引导</h1>
          <p>完成路径、模型、字幕偏好与翻译配置后，即可进入任务主界面。</p>
        </div>
        <div className="wizard-steps">
          {[1, 2, 3, 4, 5].map((value) => (
            <span key={value} className={`wizard-step ${value === step ? 'active' : value < step ? 'done' : ''}`}>
              {value}
            </span>
          ))}
        </div>
      </header>

      {message ? <div className="alert success">{message}</div> : null}
      {error ? <div className="alert error">{error}</div> : null}

      {step === 1 ? (
        <StepPath config={config} setField={setField} onNext={() => setStep(2)} />
      ) : null}

      {step === 2 ? (
        <StepModel
          config={config}
          setField={setField}
          models={models.items}
          selectedModel={selectedModel}
          setSelectedModel={setSelectedModel}
          canMoveFromModelStep={canMoveFromModelStep}
          proxyItems={proxyItems}
          proxyConfigured={proxyConfigured}
          onReload={() => void load()}
          onDownload={(name) => void handleDownload(name)}
          onPrev={() => setStep(1)}
          onNext={() => setStep(3)}
        />
      ) : null}

      {step === 3 ? (
        <StepProvider config={config} setField={setField} onPrev={() => setStep(2)} onNext={() => setStep(4)} />
      ) : null}

      {step === 4 ? (
        <StepTranslation
          config={config}
          setField={setField}
          setLLMType={setLLMType}
          testing={testing}
          onTest={() => void handleTest()}
          onPrev={() => setStep(3)}
          onNext={() => setStep(5)}
        />
      ) : null}

      {step === 5 ? (
        <StepComplete
          outputModeLabel={outputModeLabel}
          selectedModel={selectedModel}
          sourceLanguageLabel={sourceLanguageLabel}
          bilingualModeLabel={bilingualModeLabel}
          translationContentTypeLabel={translationContentTypeLabel}
          finishing={finishing}
          onComplete={() => void completeSetup()}
          onPrev={() => setStep(4)}
          bilingualEnabled={config.subtitle.bilingual}
          translationEnabled={config.translation.enabled}
          translationModel={config.translation.model}
          targetLanguages={config.translation.target_languages.join(', ')}
        />
      ) : null}
    </section>
  )
}
