import type { SearchHistoryEntry } from '../types';
import { niceTs } from '../utils';

export default function SearchHistoryDetails({ history }: { history: SearchHistoryEntry[] }) {
  return (
    <details className="mt-3 text-sm">
      <summary className="text-gray-500 cursor-pointer hover:text-blue-600">
        תוכניות חדשות שהתגלו ({history.length})
      </summary>
      <ul className="mt-2 list-none p-0">
        {history.map((h, i) => (
          <li
            key={i}
            className="flex justify-between py-2 border-b border-gray-200 last:border-0 text-gray-500"
          >
            <span>
              <b className="text-gray-900">{h.plan_number}</b> · {h.plan_name}
            </span>
            <span>{niceTs(h.ts)}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
