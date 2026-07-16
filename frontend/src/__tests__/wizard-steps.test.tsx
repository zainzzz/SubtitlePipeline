import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { defaultAppConfig } from '../api'
import { StepComplete } from '../wizard-steps/StepComplete'
import { StepPath } from '../wizard-steps/StepPath'
import { StepProvider } from '../wizard-steps/StepProvider'
import { StepTranslation } from '../wizard-steps/StepTranslation'

const noop = () => {}
const noopField = (() => {}) as never

describe('P3-5: SetupWizard step components', () => {
  it('StepPath renders input directory picker and output toggle', () => {
    render(
      <StepPath config={defaultAppConfig} setField={noopField} onNext={noop} />,
    )
    expect(screen.getByText('步骤 1：路径配置')).toBeInTheDocument()
    expect(screen.getByText('输入目录')).toBeInTheDocument()
    expect(screen.getByText('输出到源文件目录')).toBeInTheDocument()
    expect(screen.getByText('下一步')).toBeInTheDocument()
  })

  it('StepProvider renders source language select and bilingual toggle', () => {
    render(
      <StepProvider config={defaultAppConfig} setField={noopField} onPrev={noop} onNext={noop} />,
    )
    expect(screen.getByText('步骤 3：语言与字幕偏好')).toBeInTheDocument()
    expect(screen.getByText('视频源语言')).toBeInTheDocument()
    expect(screen.getByText('双语字幕')).toBeInTheDocument()
  })

  it('StepTranslation renders translation fields', () => {
    render(
      <StepTranslation
        config={defaultAppConfig}
        setField={noopField}
        setLLMType={noop}
        testing={false}
        onTest={noop}
        onPrev={noop}
        onNext={noop}
      />,
    )
    expect(screen.getByText('步骤 4：翻译配置')).toBeInTheDocument()
    expect(screen.getByText('API Base URL')).toBeInTheDocument()
    expect(screen.getByText('API Key')).toBeInTheDocument()
    expect(screen.getByText('测试连接')).toBeInTheDocument()
  })

  it('StepTranslation shows testing text when testing=true', () => {
    render(
      <StepTranslation
        config={defaultAppConfig}
        setField={noopField}
        setLLMType={noop}
        testing={true}
        onTest={noop}
        onPrev={noop}
        onNext={noop}
      />,
    )
    expect(screen.getByText('测试中…')).toBeInTheDocument()
  })

  it('StepComplete renders summary grid with expected fields', () => {
    render(
      <StepComplete
        outputModeLabel="源文件目录"
        selectedModel="whisperx-small"
        sourceLanguageLabel="自动检测"
        bilingualModeLabel="合并"
        translationContentTypeLabel="电影"
        finishing={false}
        onComplete={noop}
        onPrev={noop}
        bilingualEnabled={true}
        translationEnabled={true}
        translationModel="gpt-4o"
        targetLanguages="zh"
      />,
    )
    expect(screen.getByText('步骤 5：完成确认')).toBeInTheDocument()
    expect(screen.getByText('源文件目录')).toBeInTheDocument()
    expect(screen.getByText('whisperx-small')).toBeInTheDocument()
    expect(screen.getByText('完成初始化')).toBeInTheDocument()
  })

  it('StepComplete shows finishing text when finishing=true', () => {
    render(
      <StepComplete
        outputModeLabel="源文件目录"
        selectedModel="whisperx-small"
        sourceLanguageLabel="自动"
        bilingualModeLabel="合并"
        translationContentTypeLabel="电影"
        finishing={true}
        onComplete={noop}
        onPrev={noop}
        bilingualEnabled={false}
        translationEnabled={false}
        translationModel=""
        targetLanguages=""
      />,
    )
    expect(screen.getByText('完成中…')).toBeInTheDocument()
  })
})
