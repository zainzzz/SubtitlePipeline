import { vi } from 'vitest'

export function mockFetchResponse(body: unknown, status = 200, statusText = 'OK'): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: vi.fn().mockResolvedValue(body),
    text: vi.fn().mockResolvedValue(typeof body === 'string' ? body : JSON.stringify(body)),
    headers: new Headers(),
  } as unknown as Response
}

export function mockFetch204(): Response {
  return {
    ok: true,
    status: 204,
    statusText: 'No Content',
    json: vi.fn().mockResolvedValue(undefined),
    text: vi.fn().mockResolvedValue(''),
    headers: new Headers(),
  } as unknown as Response
}

export function mockFetchError(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: vi.fn().mockResolvedValue({ detail }),
    text: vi.fn().mockResolvedValue(detail),
    headers: new Headers(),
  } as unknown as Response
}
