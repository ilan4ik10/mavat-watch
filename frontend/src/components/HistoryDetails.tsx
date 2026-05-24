import type { HistoryEntry } from '../types';
import { actionHe, niceTs } from '../utils';

export default function HistoryDetails({ history }: { history: HistoryEntry[] }) {
  return (
    <details className="mt-3 text-sm">
      <summary className="text-gray-500 cursor-pointer hover:text-blue-600">
        יומן שינויים אחרונים ({history.length})
      </summary>
      <ul className="mt-2 list-none p-0">
        {history.map((h, i) => (
          <li
            key={i}
            className="flex justify-between py-2 border-b border-gray-200 last:border-0 text-gray-500"
          >
            <span>
              <b className="text-gray-900">{actionHe(h.action)}</b> · {h.name}
            </span>
            <span>{niceTs(h.ts)}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
