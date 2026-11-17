/**
 * The owner picker of the staff upload panel (issue #76, ADR 0031).
 *
 * An original's owner is chosen, never implied by the uploading account: the picker starts
 * unchosen, and "unassigned" is a choice of its own (no customer sees the original).
 */

export const OWNER_UNCHOSEN = "";
export const OWNER_UNASSIGNED = "unassigned";

/** The request's `organisation_id`: an id, `null` for unassigned, `undefined` while unchosen. */
export function ownerChoice(value: string): number | null | undefined {
  if (value === OWNER_UNCHOSEN) return undefined;
  if (value === OWNER_UNASSIGNED) return null;
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : undefined;
}
