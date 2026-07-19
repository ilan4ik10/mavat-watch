import type { SearchTrack } from '../types';
import SearchTrackCard from './SearchTrackCard';

interface Props {
  tracks: SearchTrack[];
  onCheck: (id: number) => void;
  onRemove: (id: number) => void;
}

export default function SearchTrackList({ tracks, onCheck, onRemove }: Props) {
  if (tracks.length === 0) {
    return (
      <div className="text-center text-gray-500 py-12 px-4 bg-white border border-dashed border-gray-200 rounded-xl">
        אין עדיין חיפושים במעקב. עברו לטאב <b>הוספת חיפוש</b> כדי להוסיף.
      </div>
    );
  }
  return (
    <>
      {tracks.map((t) => (
        <SearchTrackCard
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
