import type { SearchTrack } from '../types';
import { humanize } from '../utils';
import SearchHistoryDetails from './SearchHistoryDetails';

interface Props {
  track: SearchTrack;
  onCheck: () => void;
  onRemove: () => void;
}

export default function SearchTrackCard({ track: t, onCheck, onRemove }: Props) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-2">
      <h2 className="text-lg font-semibold mb-1">{t.label}</h2>
      <div className="text-gray-500 text-sm mb-1">
        גוש {t.gush}
        {t.parcel && ` · חלקה ${t.parcel}`}
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 my-3 text-sm text-gray-500">
        <span>
          נוסף <strong className="text-gray-900 font-medium">{humanize(t.added_at)}</strong>
        </span>
        <span>
          בדיקה אחרונה{' '}
          <strong className="text-gray-900 font-medium">{humanize(t.last_check)}</strong>
        </span>
        <span>
          <strong className="text-gray-900 font-medium">{t.plan_count}</strong> תכניות
        </span>
      </div>
      <div className="flex gap-2 flex-wrap">
        <button
          type="button"
          onClick={onCheck}
          className="px-4 py-3 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700"
        >
          בדוק עכשיו
        </button>
        <button
          type="button"
          onClick={onRemove}
          className="px-4 py-3 bg-transparent text-red-600 text-sm font-medium border border-gray-200 rounded-md hover:bg-red-50 hover:border-red-600"
        >
          הסר
        </button>
      </div>
      {t.history.length > 0 && <SearchHistoryDetails history={t.history} />}
    </div>
  );
}
