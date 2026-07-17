import { describe, expect, it, vi } from 'vitest'

import { formatDate } from '../utils/datetime'

describe('P2-17: formatDate utility', () => {
  it('test_format_date_string: ISO string returns locale-formatted value', () => {
    const iso = '2024-03-15T10:30:00Z'
    const result = formatDate(iso)
    expect(result).toBe(new Date(iso).toLocaleString())
    expect(result.length).toBeGreaterThan(0)
  })

  it('test_format_date_object: Date instance is accepted and formatted', () => {
    const date = new Date('2024-03-15T10:30:00Z')
    const result = formatDate(date)
    expect(result).toBe(date.toLocaleString())
  })

  it('test_format_date_invalid: malformed string yields empty string (no "Invalid Date" leak)', () => {
    expect(formatDate('not-a-date')).toBe('')
    expect(formatDate('2024-13-99T99:99:99Z')).toBe('')
  })

  it('test_format_date_invalid_date_object: Date with NaN time yields empty string', () => {
    const invalid = new Date('definitely-not-a-date')
    expect(Number.isNaN(invalid.getTime())).toBe(true)
    expect(formatDate(invalid)).toBe('')
  })

  it('test_format_date_null: null and undefined return empty string', () => {
    expect(formatDate(null)).toBe('')
    expect(formatDate(undefined)).toBe('')
  })

  it('test_format_date_consistent: two calls with the same input return identical output', () => {
    const iso = '2024-03-15T10:30:00Z'
    const spy = vi.spyOn(Date.prototype, 'toLocaleString')
    const a = formatDate(iso)
    const b = formatDate(iso)
    expect(a).toBe(b)
    expect(typeof a).toBe('string')
    spy.mockRestore()
  })

  it('test_format_date_does_not_mutate_input: passing a Date instance does not alter it', () => {
    const date = new Date('2024-03-15T10:30:00Z')
    const originalTime = date.getTime()
    const originalRef = date
    const result = formatDate(date)
    expect(result).toBe(date.toLocaleString())
    expect(date.getTime()).toBe(originalTime)
    expect(date).toBe(originalRef)
  })
})
