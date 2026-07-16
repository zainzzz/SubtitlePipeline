import { AppConfig, ModelItem, ModelListResponse } from '../../api'
import { StepTone } from '../SettingsWidgets'

export type GroupName = 'file' | 'processing' | 'whisper' | 'translation' | 'subtitle' | 'mux' | 'logging'

export type SetFieldFn = <T extends GroupName, K extends keyof AppConfig[T]>(
  group: T,
  key: K,
  value: AppConfig[T][K],
) => void

/**
 * Base props every settings step consumes. Each step picks the slice it needs.
 */
export interface SettingsStepProps {
  config: AppConfig
  setField: SetFieldFn
  expanded: boolean
  onToggle: () => void
}

export interface AlignHint {
  level: 'success' | 'warning' | 'muted'
  text: string
}

export interface StatusBadge {
  tone: StepTone
  label: string
}

export interface AsrStepProps extends SettingsStepProps {
  models: ModelListResponse
  installedAsrModels: ModelItem[]
  selectedAsrModel: ModelItem | null
  currentProvider: string
  sourceLanguageLabel: string
  onSelectAsrModel: (modelName: string) => void
}

export interface AlignStepProps extends SettingsStepProps {
  currentProvider: string
  qwenAlignerInstalled: boolean
  alignHint: AlignHint
  alignStatus: StatusBadge
}

export interface TranslationStepProps extends SettingsStepProps {
  testing: boolean
  setLLMType: (value: AppConfig['translation']['llm_type']) => void
  onTestTranslation: () => void
}

export type { ModelItem, ModelListResponse }
