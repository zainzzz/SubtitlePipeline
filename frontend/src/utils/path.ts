// Small path helpers shared by multiple pages. Kept here (rather than inside a
// page file) so they can be unit-tested in isolation and reused by future
// components that need the same logic (e.g. a future "Jobs" page that lists
// suspect task file paths the same way Dashboard does).

/** Return the trailing path component. For POSIX, this is everything after
 * the last "/". For Windows-style paths, everything after the last "\\" or "/".
 * Returns the input unchanged if no separator is present.
 *
 * Note: only the lexical suffix is inspected; this is not a full Path
 * implementation. Sufficient for display purposes.
 */
export function basename(path: string): string {
  if (!path) return ''
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] || ''
}

/** Format a duration in seconds as a short human-readable string.
 * - 0..59: "Ns"
 * - 60..3599: "Mm Ss"
 * - 3600+:  "Hh Mm"
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0s'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}
