import type { SearchTrack, Track } from './types';

async function jsonRequest<T>(input: string, init?: RequestInit): Promise<T> {
  const res = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string };
      if (body?.error) message = body.error;
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  list: () => jsonRequest<Track[]>('/api/tracks'),
  add: (url: string) =>
    jsonRequest<Track[]>('/api/tracks', { method: 'POST', body: JSON.stringify({ url }) }),
  check: (id: string) =>
    jsonRequest<Track[]>(`/api/tracks/${id}/check`, { method: 'POST' }),
  simulate: (id: string) =>
    jsonRequest<Track[]>(`/api/tracks/${id}/simulate`, { method: 'POST' }),
  simulatePdf: (id: string) =>
    jsonRequest<Track[]>(`/api/tracks/${id}/simulate-pdf`, { method: 'POST' }),
  remove: (id: string) =>
    jsonRequest<Track[]>(`/api/tracks/${id}`, { method: 'DELETE' }),
};

export const searchApi = {
  list: () => jsonRequest<SearchTrack[]>('/api/searches'),
  add: (gush: string, parcel: string, label: string) =>
    jsonRequest<SearchTrack[]>('/api/searches', {
      method: 'POST',
      body: JSON.stringify({ gush, parcel, label }),
    }),
  check: (id: number) =>
    jsonRequest<SearchTrack[]>(`/api/searches/${id}/check`, { method: 'POST' }),
  remove: (id: number) =>
    jsonRequest<SearchTrack[]>(`/api/searches/${id}`, { method: 'DELETE' }),
};
