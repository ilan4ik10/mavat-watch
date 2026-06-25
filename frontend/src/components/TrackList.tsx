import type { Track } from '../types';
import TrackCard from './TrackCard';

interface Props {
  tracks: Track[];
  onCheck: (id: string) => void;
  onRemove: (id: string) => void;
}

export default function TrackList({ tracks, onCheck, onRemove }: Props) {
  if (tracks.length === 0) {
    return (
      <div className="text-center text-gray-500 py-12 px-4 bg-white border border-dashed border-gray-200 rounded-xl">
        אין עדיין תכניות במעקב. עברו לטאב <b>הוספת תכנית</b> כדי להוסיף.
      </div>
    );
  }
  return (
    <>
      {tracks.map((t) => (
        <TrackCard
          key={t.id}
          track={t}
          onCheck={() => onCheck(t.id)}
          onRemove={() => {
            if (confirm(`להפסיק את המעקב אחר ${t.label}?`)) onRemove(t.id);
          }}
        />
      ))}
    </>
  );
}
