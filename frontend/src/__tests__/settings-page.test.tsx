import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  AppConfig,
  cloneConfig,
  defaultAppConfig,
  ModelItem,
} from '../api'
import { AlignStep } from '../components/settings-steps/AlignStep'
import { AsrStep } from '../components/settings-steps/AsrStep'
import { FileScanStep } from '../components/settings-steps/FileScanStep'
import { MuxStep } from '../components/settings-steps/MuxStep'
import { SubtitleStep } from '../components/settings-steps/SubtitleStep'
import { SystemStep } from '../components/settings-steps/SystemStep'
import { TranslationStep } from '../components/settings-steps/TranslationStep'
import { SettingsPage } from '../pages/SettingsPage'
import { mockFetch204, mockFetchError, mockFetchResponse } from './mocks'

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------

const noop = () => {}
const noopField = (() => {}) as never

function makeModel(overrides: Partial<ModelItem> = {}): ModelItem {
  return {
    name: 'whisperx-small',
    repo_id: 'repo/whisperx-small',
    size_label: '500 MB',
    estimated_size_bytes: 500_000_000,
    status: 'installed',
    progress: 1,
    current: false,
    path: '/models/whisperx-small',
    provider: 'whisperx',
    display_name: 'WhisperX Small',
    description: 'Small model',
    tags: [],
    model_type: 'asr',
    ...overrides,
  }
}

function renderPage(initialModels: ModelItem[] = []) {
  const configResponse = cloneConfig(defaultAppConfig)
  const modelsResponse = { items: initialModels, current_model: '' }

  const fetchMock = vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation((input: any, init?: any) => {
      const url = typeof input === 'string' ? input : input.url
      const method = init?.method
      if (url.includes('/api/config') && (!method || method === 'GET')) {
        return Promise.resolve(mockFetchResponse(configResponse))
      }
      if (url.includes('/api/models')) {
        return Promise.resolve(mockFetchResponse(modelsResponse))
      }
      if (url.includes('/api/config') && method === 'PUT') {
        return Promise.resolve(mockFetchResponse(configResponse))
      }
      return Promise.resolve(mockFetchResponse({}))
    })

  const result = render(
    <MemoryRouter>
      <SettingsPage />
    </MemoryRouter>,
  )
  return { fetchMock, configResponse, modelsResponse, ...result }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ----------------------------------------------------------------------------
// Tests
// ----------------------------------------------------------------------------

describe('SettingsPage refactor: step extraction', () => {
  it('renders all 7 step titles after load', async () => {
    renderPage([makeModel()])
    await waitFor(() => expect(screen.queryByText('配置加载中…')).toBeNull())

    expect(screen.getByText('文件扫描')).toBeInTheDocument()
    expect(screen.getByText('语音识别')).toBeInTheDocument()
    expect(screen.getByText('时间轴对齐')).toBeInTheDocument()
    expect(screen.getByText('翻译')).toBeInTheDocument()
    expect(screen.getByText('字幕输出')).toBeInTheDocument()
    expect(screen.getByText('字幕压片')).toBeInTheDocument()
    expect(screen.getByText('系统参数')).toBeInTheDocument()
  })

  it('FileScanStep is independently renderable with minimal props', () => {
    render(
      <FileScanStep
        config={defaultAppConfig}
        setField={noopField}
        expanded={false}
        onToggle={noop}
      />,
    )
    expect(screen.getByText('文件扫描')).toBeInTheDocument()
    expect(screen.getByText('输入目录')).toBeInTheDocument()
  })

  it('AsrStep is independently renderable with minimal props', () => {
    render(
      <AsrStep
        config={defaultAppConfig}
        setField={noopField}
        expanded={false}
        onToggle={noop}
        models={{ items: [], current_model: '' }}
        installedAsrModels={[]}
        selectedAsrModel={null}
        currentProvider="whisperx"
        sourceLanguageLabel="自动检测"
        onSelectAsrModel={noop}
      />,
    )
    expect(screen.getByText('语音识别')).toBeInTheDocument()
    expect(screen.getByText('识别模型')).toBeInTheDocument()
  })

  it('AlignStep is independently renderable with minimal props', () => {
    render(
      <AlignStep
        config={defaultAppConfig}
        setField={noopField}
        expanded={false}
        onToggle={noop}
        currentProvider="whisperx"
        qwenAlignerInstalled={false}
        alignHint={{ level: 'muted', text: '提示文本' }}
        alignStatus={{ tone: 'neutral', label: '自动模式' }}
      />,
    )
    expect(screen.getByText('时间轴对齐')).toBeInTheDocument()
    expect(screen.getByText('提示文本')).toBeInTheDocument()
  })

  it('TranslationStep is independently renderable with minimal props', () => {
    render(
      <TranslationStep
        config={defaultAppConfig}
        setField={noopField}
        expanded={false}
        onToggle={noop}
        testing={false}
        setLLMType={noop}
        onTestTranslation={noop}
      />,
    )
    expect(screen.getByText('翻译')).toBeInTheDocument()
    expect(screen.getByText('LLM 类型')).toBeInTheDocument()
    expect(screen.getByText('测试连接')).toBeInTheDocument()
  })

  it('SubtitleStep, MuxStep, SystemStep are each independently renderable', () => {
    const { unmount: u1 } = render(
      <SubtitleStep config={defaultAppConfig} setField={noopField} expanded={false} onToggle={noop} />,
    )
    expect(screen.getByText('字幕输出')).toBeInTheDocument()
    u1()

    const { unmount: u2 } = render(
      <MuxStep config={defaultAppConfig} setField={noopField} expanded={false} onToggle={noop} />,
    )
    expect(screen.getByText('字幕压片')).toBeInTheDocument()
    u2()

    const { unmount: u3 } = render(
      <SystemStep config={defaultAppConfig} setField={noopField} expanded={false} onToggle={noop} />,
    )
    expect(screen.getByText('系统参数')).toBeInTheDocument()
    u3()
  })

  it('regression P1-4: overviewStats recomputes when selectedAsrModel object changes (same display_name)', async () => {
    // Two distinct model objects with identical display_name but different status.
    // The pre-fix code used selectedAsrModel?.display_name as the only model-related dep,
    // so a status flip would NOT re-render overview. We assert the overview tracks status.
    const installed = makeModel({
      name: 'whisperx-small',
      display_name: 'WhisperX Small',
      status: 'installed',
      provider: 'whisperx',
    })

    const { fetchMock } = renderPage([installed])
    await waitFor(() => expect(screen.queryByText('配置加载中…')).toBeNull())

    // The "已选模型" stat hint is the currentProvider ("whisperx").
    const hintCellsBefore = screen.getAllByText('whisperx')
    expect(hintCellsBefore.length).toBeGreaterThan(0)

    // Now swap the model response so selectedAsrModel is a new object with same
    // display_name but different provider. overviewStats must update hint.
    fetchMock.mockRestore()
    const swapped = makeModel({
      name: 'whisperx-small',
      display_name: 'WhisperX Small', // unchanged
      provider: 'faster-whisper', // changed provider
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: any, init?: any) => {
      const url = typeof input === 'string' ? input : input.url
      const method = init?.method
      if (url.includes('/api/models')) {
        return Promise.resolve(mockFetchResponse({ items: [swapped], current_model: '' }))
      }
      if (url.includes('/api/config') && (!method || method === 'GET')) {
        return Promise.resolve(mockFetchResponse(cloneConfig(defaultAppConfig)))
      }
      return Promise.resolve(mockFetchResponse({}))
    })

    // Trigger a reload by clicking 重新加载.
    fireEvent.click(screen.getByText('重新加载'))
    await waitFor(
      () => expect(screen.getAllByText('faster-whisper').length).toBeGreaterThan(0),
      { timeout: 3000 },
    )
  })

  it('regression P1-3: alignHint reacts to provider change coming from selectedAsrModel', async () => {
    // Start with a whisperx-provider model: align hint should say WhisperX 内置对齐.
    const wxModel = makeModel({
      name: 'whisperx-small',
      provider: 'whisperx',
    })
    const { fetchMock } = renderPage([wxModel])
    await waitFor(() => expect(screen.queryByText('配置加载中…')).toBeNull())

    // Default align_provider is 'auto'; currentProvider becomes 'whisperx'.
    expect(screen.getAllByText('当前将使用 WhisperX 内置对齐').length).toBeGreaterThan(0)

    // Swap to a faster-whisper model with no qwen aligner installed.
    // alignHint should now warn about downloading Qwen aligner.
    fetchMock.mockRestore()
    const fwModel = makeModel({
      name: 'faster-whisper-large-v3',
      provider: 'faster-whisper',
      display_name: 'Faster-Whisper Large v3',
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: any, init?: any) => {
      const url = typeof input === 'string' ? input : input.url
      const method = init?.method
      if (url.includes('/api/models')) {
        return Promise.resolve(mockFetchResponse({ items: [fwModel], current_model: '' }))
      }
      // preserve the config model_name swap so selectedAsrModel finds the new model
      if (url.includes('/api/config') && (!method || method === 'GET')) {
        const cfg = cloneConfig(defaultAppConfig)
        cfg.whisper.model_name = 'faster-whisper-large-v3'
        return Promise.resolve(mockFetchResponse(cfg))
      }
      return Promise.resolve(mockFetchResponse({}))
    })

    fireEvent.click(screen.getByText('重新加载'))
    await waitFor(
      () =>
        expect(screen.getAllByText('建议下载 Qwen3 强制对齐模型以提升精度').length).toBeGreaterThan(0),
      { timeout: 3000 },
    )
  })

  it('setField propagates input changes into config state', async () => {
    renderPage([makeModel()])
    await waitFor(() => expect(screen.queryByText('配置加载中…')).toBeNull())

    // The 工作目录 input belongs to SystemStep.
    const workDirInput = screen.getByDisplayValue(defaultAppConfig.processing.work_dir)
    fireEvent.change(workDirInput, { target: { value: '/tmp/custom-work' } })
    expect(workDirInput).toHaveValue('/tmp/custom-work')
  })

  it('save button is enabled after editing config and triggers PUT /api/config', async () => {
    const { fetchMock } = renderPage([makeModel()])
    await waitFor(() => expect(screen.queryByText('配置加载中…')).toBeNull())

    // The 保存设置 button should be enabled once a field changes.
    const workDirInput = screen.getByDisplayValue(defaultAppConfig.processing.work_dir)
    fireEvent.change(workDirInput, { target: { value: '/tmp/saved-here' } })

    const saveButton = screen.getByRole('button', { name: '保存设置' })
    expect(saveButton).not.toBeDisabled()

    await act(async () => {
      fireEvent.click(saveButton)
    })

    const putCall = fetchMock.mock.calls.find((call) => {
      const url = String(call[0])
      const init = (call[1] as RequestInit | undefined) ?? {}
      return init.method === 'PUT' && url.includes('/api/config')
    })
    expect(putCall).toBeTruthy()
    const putUrl = String(putCall![0])
    const putInit = putCall![1] as RequestInit
    expect(putUrl).toContain('/api/config')
    expect(putInit.method).toBe('PUT')
    expect(String(putInit.body)).toContain('"work_dir":"/tmp/saved-here"')
  })

  it('shows loading indicator before config is loaded', () => {
    // Don't resolve fetch immediately — render in pending state.
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () => new Promise(() => {}), // never resolves
    )
    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    )
    expect(screen.getByText('配置加载中…')).toBeInTheDocument()
  })

  it('renders error UI when save fails', async () => {
    const { fetchMock } = renderPage([makeModel()])
    await waitFor(() => expect(screen.queryByText('配置加载中…')).toBeNull())

    // Make the next PUT fail.
    fetchMock.mockImplementation((input: any, init?: any) => {
      const url = typeof input === 'string' ? input : input.url
      const method = init?.method
      if (url.includes('/api/config') && method === 'PUT') {
        return Promise.resolve(mockFetchError(500, '后端写入失败'))
      }
      if (url.includes('/api/config')) {
        return Promise.resolve(mockFetchResponse(cloneConfig(defaultAppConfig)))
      }
      if (url.includes('/api/models')) {
        return Promise.resolve(mockFetchResponse({ items: [makeModel()], current_model: '' }))
      }
      return Promise.resolve(mockFetchResponse({}))
    })

    // Edit + save.
    fireEvent.change(screen.getByDisplayValue(defaultAppConfig.processing.work_dir), {
      target: { value: '/tmp/will-fail' },
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    })

    await waitFor(() =>
      expect(screen.getByText(/后端写入失败/)).toBeInTheDocument(),
    )
  })

  it('step components import cleanly (smoke check)', async () => {
    // Dynamic import of every step module — verifies there are no circular or missing imports.
    const modules: Array<Record<string, unknown>> = [
      await import('../components/settings-steps/FileScanStep'),
      await import('../components/settings-steps/AsrStep'),
      await import('../components/settings-steps/AlignStep'),
      await import('../components/settings-steps/TranslationStep'),
      await import('../components/settings-steps/SubtitleStep'),
      await import('../components/settings-steps/MuxStep'),
      await import('../components/settings-steps/SystemStep'),
    ]
    expect(modules.every((mod) => typeof mod === 'object')).toBe(true)
    expect(typeof modules[0].FileScanStep).toBe('function')
    expect(typeof modules[1].AsrStep).toBe('function')
    expect(typeof modules[2].AlignStep).toBe('function')
    expect(typeof modules[3].TranslationStep).toBe('function')
    expect(typeof modules[4].SubtitleStep).toBe('function')
    expect(typeof modules[5].MuxStep).toBe('function')
    expect(typeof modules[6].SystemStep).toBe('function')
  })
})
