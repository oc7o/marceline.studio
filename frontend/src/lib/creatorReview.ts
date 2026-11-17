/**
 * Pure logic for `CreatorsScreen` (issue #29 follow-up, operator 2026-09-17): resolving which
 * target creator a merge/alias confirmation should send. Framework-free so it can be unit tested
 * without rendering the dialog.
 */

/** The chosen target id: a picked suggestion wins, otherwise a valid typed id, otherwise null. */
export function resolveTargetId(targetId: number | null, manualId: string): number | null {
  if (targetId != null) return targetId;
  const trimmed = manualId.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}
