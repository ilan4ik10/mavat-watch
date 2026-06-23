export function humanize(iso: string): string {
  if (!iso) return '';
  const then = new Date(iso);
  const seconds = (Date.now() - then.getTime()) / 1000;
  if (seconds < 60) return 'ממש עכשיו';
  if (seconds < 3600) return `לפני ${Math.floor(seconds / 60)} דק'`;
  if (seconds < 86400) return `לפני ${Math.floor(seconds / 3600)} שעות`;
  return `לפני ${Math.floor(seconds / 86400)} ימים`;
}

export function niceTs(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const ACTION_LABEL: Record<string, string> = {
  NEW: 'חדש',
  UPDATED: 'עודכן',
  REMOVED: 'הוסר',
  SECTION_NEW: 'מקטע חדש',
  SECTION_REMOVED: 'מקטע הוסר',
};
export const actionHe = (a: string) => ACTION_LABEL[a] ?? a;
