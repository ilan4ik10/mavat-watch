import { useEffect, useState } from 'react';
import { api } from './api';
import type { Track } from './types';
import Header from './components/Header';
import Tabs from './components/Tabs';
import Spinner from './components/Spinner';
import AddTrackForm from './components/AddTrackForm';
import TrackList from './components/TrackList';

type TabId = 'tracked' | 'add';

export default function App() {
  const [tracks, setTracks] = useState<Track[]>([]);
  const [tab, setTab] = useState<TabId>('tracked');
  const [spinner, setSpinner] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .list()
      .then(setTracks)
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

  return (
    <div className="bg-page min-h-screen text-gray-900 py-10 px-4" dir="rtl">
      <main className="max-w-2xl mx-auto">
        <Header />
        {spinner && <Spinner text={spinner} />}
        <Tabs active={tab} onChange={setTab} trackedCount={tracks.length} />
        {tab === 'tracked' &&
          (loading ? (
            <Spinner text="טוען תכניות" />
          ) : (
            <TrackList
              tracks={tracks}
              onCheck={(id) => run('בודק את התכנית ברגעים אלה', () => api.check(id))}
              onRemove={(id) => run('מסיר את התכנית מהמעקב', () => api.remove(id))}
            />
          ))}
        {tab === 'add' && (
          <AddTrackForm
            onAdd={(url) =>
              run('מוסיף את התוכנית שלך למעקב ברגעים אלה', () => api.add(url)).then(() =>
                setTab('tracked'),
              )
            }
          />
        )}
      </main>
    </div>
  );
}
