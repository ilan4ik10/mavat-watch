import type { Track } from '../types';
import { humanize } from '../utils';
import HistoryDetails from './HistoryDetails';

interface Props {
  track: Track;
  onCheck: () => void;
  onRemove: () => void;
}

export default function TrackCard({ track: t, onCheck, onRemove }: Props) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-2">
      <h2 className="text-lg font-semibold mb-1">{t.label}</h2>
      {t.title && <div className="text-gray-700 text-sm mb-1 leading-relaxed">{t.title}</div>}
      <a
        href={t.url}
        target="_blank"
        rel="noreferrer"
        dir="ltr"
        className="text-gray-500 text-sm break-all hover:text-blue-600"
      >
        {t.url}
      </a>
      <div className="flex flex-wrap gap-x-5 gap-y-1 my-3 text-sm text-gray-500">
        <span>
          נוספה <strong className="text-gray-900 font-medium">{humanize(t.added_at)}</strong>
        </span>
        <span>
          בדיקה אחרונה{' '}
          <strong className="text-gray-900 font-medium">{humanize(t.last_check)}</strong>
        </span>
        <span>
          <strong className="text-gray-900 font-medium">{t.total_rows}</strong> מסמכים
        </span>
        <span>
          <strong className="text-gray-900 font-medium">{t.history_count}</strong> שינויים תועדו
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
      {t.history.length > 0 && <HistoryDetails history={t.history} />}
    </div>
  );
}
