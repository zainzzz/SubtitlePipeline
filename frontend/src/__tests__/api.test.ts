import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { cancelTask, deleteTask, getTasks, retryTask } from '../api'
import { mockFetch204, mockFetchError, mockFetchResponse } from './mocks'

const setFetch = (response: Response) => {
  globalThis.fetch = vi.fn().mockResolvedValue(response)
}

const tasksBody = {
  items: [
    {
      id: 1,
      file_path: '/data/video.mp4',
      status: 'pending',
      stage: 'queued',
      progress: 0,
      retry_count: 0,
      max_retries: 3,
      cancel_requested: 0,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    },
  ],
  total: 1,
  page: 1,
  page_size: 20,
  status_counts: { pending: 1 },
}

describe('api.ts P1-5: type-safe 204 handling', () => {
  beforeEach(() => {
    vi.useRealTimers()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns parsed JSON body for 200 responses from request<T>', async () => {
    setFetch(mockFetchResponse(tasksBody, 200))
    const result = await getTasks()
    expect(result).toEqual(tasksBody)
    expect(result.total).toBe(1)
  })

  it('returns undefined for 204 responses from requestMaybeEmpty<void>', async () => {
    setFetch(mockFetch204())
    const result = await deleteTask(42)
    expect(result).toBeUndefined()
  })

  it('cancelTask resolves without throwing on 204', async () => {
    setFetch(mockFetch204())
    await expect(cancelTask(7)).resolves.toBeUndefined()
  })

  it('retryTask resolves without throwing on 204', async () => {
    setFetch(mockFetch204())
    await expect(retryTask(99, 'restart')).resolves.toBeUndefined()
  })

  it('throws on non-ok responses with detail message', async () => {
    setFetch(mockFetchError(500, 'internal error'))
    await expect(getTasks()).rejects.toThrow('internal error')
  })

  it('handles 204 with empty text body for requestMaybeEmpty', async () => {
    const response = {
      ok: true,
      status: 204,
      statusText: 'No Content',
      json: vi.fn(),
      text: vi.fn().mockResolvedValue(''),
      headers: new Headers(),
    } as unknown as Response
    setFetch(response)
    const result = await deleteTask(1)
    expect(result).toBeUndefined()
  })

  it('requestMaybeEmpty parses JSON body when 200 with content', async () => {
    setFetch(mockFetchResponse({ ok: true }, 200))
    const result = await retryTask(5, 'resume')
    expect(result).toEqual({ ok: true })
  })
})
