export type Action =
  | 'NEW'
  | 'UPDATED'
  | 'REMOVED'
  | 'SECTION_NEW'
  | 'SECTION_REMOVED'
  | 'COUNT_CHANGED';

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

export interface SearchHistoryEntry {
  ts: string;
  plan_id: string;
  plan_number: string;
  plan_name: string;
  auth_name: string;
  status: string;
}

export interface SearchTrack {
  id: number;
  gush: string;
  parcel: string;
  label: string;
  added_at: string;
  last_check: string;
  plan_count: number;
  history: SearchHistoryEntry[];
}
