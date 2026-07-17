import { describe, expect, it } from 'vitest'

import * as apiExports from '../api'

describe('P2-15: asrProviderOptions should be removed from api.ts', () => {
  it('asrProviderOptions is NOT exported from api.ts', () => {
    expect(apiExports).not.toHaveProperty('asrProviderOptions')
  })

  it('other options arrays are still exported', () => {
    expect(apiExports).toHaveProperty('translationContentTypeOptions')
    expect(apiExports).toHaveProperty('llmTypeOptions')
    expect(apiExports).toHaveProperty('bilingualModeOptions')
    expect(apiExports).toHaveProperty('retryModeOptions')
    expect(apiExports).toHaveProperty('sourceLanguageOptions')
  })

  it('defaultAppConfig is still exported', () => {
    expect(apiExports).toHaveProperty('defaultAppConfig')
  })

  it('ASRProvider type still compiles (import works)', () => {
    // If ASRProvider type were accidentally removed, this wouldn't compile
    type T = import('../api').ASRProvider
    const _provider: T | undefined = undefined
    expect(_provider).toBeUndefined()
  })
})
