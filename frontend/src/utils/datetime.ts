/**
 * Datetime formatting utilities.
 *
 * P2-17 — extracted to avoid inline `new Date(...)` allocation inside JSX
 * render bodies. TasksPage renders a row per task and polls every 3 seconds;
 * the previous inline `new Date(task.updated_at).toLocaleString()` allocated
 * a fresh Date and ran locale formatting on every render for every row. This
 * helper keeps the same `toLocaleString()` output but centralises the logic so
 * the allocation is explicit, testable, and easy to swap for an ICU cache.
 */

/**
 * Format an ISO timestamp string or Date instance using the host locale.
 *
 * Accepts both `string` (ISO 8601 from the backend) and `Date` instances so
 * callers do not need to branch on input type. The following inputs degrade
 * gracefully so a malformed backend value never crashes the UI:
 *
 * - `null` / `undefined`  → `''`
 * - invalid date string   → `''` (the original `Invalid Date` would otherwise
 *   leak into the DOM and confuse users)
 * - invalid `Date` object (`.getTime()` is NaN) → `''`
 *
 * The function is pure: it does not mutate the input `Date` and produces
 * deterministic output for a given input within the same locale/timezone.
 *
 * @param input ISO string, Date instance, or nullish value.
 * @returns locale-formatted string, or `''` when the input is not parseable.
 */
export function formatDate(input: string | Date | null | undefined): string {
  if (input === null || input === undefined) return ''
  const date = input instanceof Date ? input : new Date(input)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString()
}
