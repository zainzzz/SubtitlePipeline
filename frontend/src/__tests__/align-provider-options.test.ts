import { describe, expect, it } from 'vitest'

import { alignProviderOptions } from '../components/SettingsWidgets'
import { AlignProvider } from '../api'

describe('P2-16: alignProviderOptions centralized in SettingsWidgets', () => {
  it('exports alignProviderOptions from SettingsWidgets', () => {
    expect(alignProviderOptions).toBeDefined()
    expect(Array.isArray(alignProviderOptions)).toBe(true)
  })

  it('has exactly 4 options', () => {
    expect(alignProviderOptions).toHaveLength(4)
  })

  it('contains all expected values: auto, whisperx, qwen-forced, none', () => {
    const values = alignProviderOptions.map((o) => o.value)
    expect(values).toContain('auto')
    expect(values).toContain('whisperx')
    expect(values).toContain('qwen-forced')
    expect(values).toContain('none')
  })

  it('every option has a non-empty label', () => {
    for (const option of alignProviderOptions) {
      expect(option.label).toBeTruthy()
      expect(typeof option.label).toBe('string')
    }
  })

  it('every value is a valid AlignProvider', () => {
    const validProviders: AlignProvider[] = ['auto', 'whisperx', 'qwen-forced', 'none']
    for (const option of alignProviderOptions) {
      expect(validProviders).toContain(option.value)
    }
  })

  it('all values are unique', () => {
    const values = alignProviderOptions.map((o) => o.value)
    const unique = new Set(values)
    expect(unique.size).toBe(values.length)
  })
})
