import { describe, expect, it } from 'vitest'

import { basename, formatDuration } from './path'

describe('basename', () => {
  it('returns the trailing component of a POSIX path', () => {
    expect(basename('/data/movies/a.mkv')).toBe('a.mkv')
    expect(basename('/data/movies/sub/a.mkv')).toBe('a.mkv')
  })

  it('handles paths without separators', () => {
    expect(basename('a.mkv')).toBe('a.mkv')
  })

  it('returns empty string for empty input', () => {
    expect(basename('')).toBe('')
  })

  it('handles trailing slash', () => {
    expect(basename('/data/movies/')).toBe('')
  })

  it('handles Windows-style separators', () => {
    expect(basename('C:\\Users\\foo\\bar.mkv')).toBe('bar.mkv')
    expect(basename('C:/Users/foo/bar.mkv')).toBe('bar.mkv')
  })
})

describe('formatDuration', () => {
  it('formats sub-minute durations as seconds', () => {
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(5)).toBe('5s')
    expect(formatDuration(59)).toBe('59s')
  })

  it('formats sub-hour durations as minutes + seconds', () => {
    expect(formatDuration(60)).toBe('1m 0s')
    expect(formatDuration(125)).toBe('2m 5s')
    expect(formatDuration(3599)).toBe('59m 59s')
  })

  it('formats hour-plus durations as hours + minutes', () => {
    expect(formatDuration(3600)).toBe('1h 0m')
    expect(formatDuration(3661)).toBe('1h 1m')
    expect(formatDuration(7325)).toBe('2h 2m')
  })

  it('returns 0s for invalid input', () => {
    expect(formatDuration(NaN)).toBe('0s')
    expect(formatDuration(-1)).toBe('0s')
    expect(formatDuration(Infinity)).toBe('0s')
  })
})
