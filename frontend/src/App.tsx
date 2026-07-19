import { useEffect, useState } from 'react';
import { api, searchApi } from './api';
import type { SearchTrack, Track } from './types';
import Header from './components/Header';
import Tabs, { type TabId } from './components/Tabs';
import Spinner from './components/Spinner';
import AddTrackForm from './components/AddTrackForm';
import TrackList from './components/TrackList';
import AddSearchTrackForm from './components/AddSearchTrackForm';
import SearchTrackList from './components/SearchTrackList';

export default function App() {
  const [tracks, setTracks] = useState<Track[]>([]);
  const [searches, setSearches] = useState<SearchTrack[]>([]);
  const [tab, setTab] = useState<TabId>('tracked');
  const [spinner, setSpinner] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.list(), searchApi.list()])
      .then(([t, s]) => {
        setTracks(t);
        setSearches(s);
      })
      .catch((e) => alert('שגיאה: ' + (e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  async function run(message: string, action: () => Promise<Track[]>) {
    setSpinner(message);
    try {
      setTracks(await action());
    } catch (e) {
      alert('שגיאה: ' + (e as Error).message);
    } finally {
      setSpinner(null);
    }
  }

  async function runSearch(message: string, action: () => Promise<SearchTrack[]>) {
    setSpinner(message);
    try {
      setSearches(await action());
    } catch (e) {
      alert('שגיאה: ' + (e as Error).message);
    } finally {
      setSpinner(null);
    }
  }

  if (loading) {
    return (
      <div
        className="bg-page min-h-screen flex items-center justify-center px-4"
        dir="rtl"
      >
        <Spinner text="טוען תכניות" />
      </div>
    );
  }

  return (
    <div className="bg-page min-h-screen text-gray-900 py-10 px-4" dir="rtl">
      <main className="max-w-2xl mx-auto">
        <Header />
        {spinner && <Spinner text={spinner} />}
        <Tabs
          active={tab}
          onChange={setTab}
          trackedCount={tracks.length}
          searchesCount={searches.length}
        />
        {tab === 'tracked' && (
          <TrackList
            tracks={tracks}
            onCheck={(id) => run('בודק את התכנית ברגעים אלה', () => api.check(id))}
            onRemove={(id) => run('מסיר את התכנית מהמעקב', () => api.remove(id))}
          />
        )}
        {tab === 'add' && (
          <AddTrackForm
            onAdd={(url) =>
              run('מוסיף את התוכנית שלך למעקב ברגעים אלה', () => api.add(url)).then(() =>
                setTab('tracked'),
              )
            }
          />
        )}
        {tab === 'searches' && (
          <SearchTrackList
            tracks={searches}
            onCheck={(id) => runSearch('בודק את החיפוש ברגעים אלה', () => searchApi.check(id))}
            onRemove={(id) => runSearch('מסיר את החיפוש מהמעקב', () => searchApi.remove(id))}
          />
        )}
        {tab === 'add-search' && (
          <AddSearchTrackForm
            onAdd={(gush, parcel, label) =>
              runSearch('מוסיף את החיפוש שלך למעקב ברגעים אלה', () =>
                searchApi.add(gush, parcel, label),
              ).then(() => setTab('searches'))
            }
          />
        )}
      </main>
    </div>
  );
}
