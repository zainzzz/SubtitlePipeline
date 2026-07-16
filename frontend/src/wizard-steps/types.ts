import { AppConfig, ModelItem, SystemStatus } from '../api'

export type GroupName = 'file' | 'translation' | 'subtitle'

export interface WizardStepProps {
  config: AppConfig
  setField: <T extends GroupName, K extends keyof AppConfig[T]>(group: T, key: K, value: AppConfig[T][K]) => void
}

export interface StepModelProps extends WizardStepProps {
  models: ModelItem[]
  selectedModel: string
  setSelectedModel: (name: string) => void
  canMoveFromModelStep: boolean
  proxyItems: Array<{ label: string; value: string | null }>
  proxyConfigured: boolean
  onReload: () => void
  onDownload: (name: string) => void
  onPrev: () => void
  onNext: () => void
}

export interface StepTranslationProps extends WizardStepProps {
  setLLMType: (value: AppConfig['translation']['llm_type']) => void
  testing: boolean
  onTest: () => void
  onPrev: () => void
  onNext: () => void
}

export interface StepCompleteProps {
  config: AppConfig
  selectedModel: string
  outputModeLabel: string
  sourceLanguageLabel: string
  bilingualModeLabel: string
  translationContentTypeLabel: string
  finishing: boolean
  onComplete: () => void
  onPrev: () => void
}

export interface StepProviderProps extends WizardStepProps {}

export type { SystemStatus }
