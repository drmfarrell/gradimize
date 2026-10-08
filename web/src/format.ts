// Shared formatting/sorting helpers.
//
// One home for the 12-hour time formatter (two copies had already diverged:
// "9a" vs "9am"), the newest-first created_at sort (was copy-pasted six
// times), and the timestamp display format.

export function toMin(t: string): number {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

/** Minutes-since-midnight → compact 12-hour label ("9a", "2:30p"). */
export function fmtTimeMin(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  const ap = h < 12 ? "a" : "p";
  const hh = ((h + 11) % 12) + 1;
  return m ? `${hh}:${String(m).padStart(2, "0")}${ap}` : `${hh}${ap}`;
}

/** "HH:MM" → compact 12-hour label ("9a", "2:30p"). */
export function fmtTime(t: string): string {
  return fmtTimeMin(toMin(t));
}

/** Newest-first copy of a list with ISO `created_at` timestamps. */
export function sortNewestFirst<T extends { created_at: string }>(items: T[]): T[] {
  return items.slice().sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** ISO timestamp → "YYYY-MM-DD HH:MM" for display. */
export function fmtTimestamp(iso: string): string {
  return iso.slice(0, 16).replace("T", " ");
}
