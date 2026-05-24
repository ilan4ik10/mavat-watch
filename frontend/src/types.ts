export type Action = 'NEW' | 'UPDATED' | 'REMOVED';

export interface HistoryEntry {
  ts: string;
  section: string;
  action: Action;
  name: string;
  category: string;
  scope: string;
  edit_date: string;
  prev_scope: string;
  prev_edit_date: string;
}

export interface Track {
  id: string;
  url: string;
  label: string;
  title: string;
  added_at: string;
  last_check: string;
  total_rows: number;
  history: HistoryEntry[];
  history_count: number;
}
