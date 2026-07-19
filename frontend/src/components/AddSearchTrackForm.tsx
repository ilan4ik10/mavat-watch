import { useState } from 'react';

interface Props {
  onAdd: (gush: string, parcel: string, label: string) => void;
}

export default function AddSearchTrackForm({ onAdd }: Props) {
  const [gush, setGush] = useState('');
  const [parcel, setParcel] = useState('');
  const [label, setLabel] = useState('');

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-8">
      <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-3">
        הוספת חיפוש גוש/חלקה למעקב
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const trimmedGush = gush.trim();
          if (trimmedGush) {
            onAdd(trimmedGush, parcel.trim(), label.trim());
            setGush('');
            setParcel('');
            setLabel('');
          }
        }}
        className="flex flex-col gap-2"
      >
        <div className="flex gap-2">
          <input
            type="number"
            required
            value={gush}
            onChange={(e) => setGush(e.target.value)}
            placeholder="גוש"
            className="flex-1 px-3 py-3 border border-gray-200 rounded-md text-sm focus:border-blue-600 focus:outline-none"
          />
          <input
            type="number"
            value={parcel}
            onChange={(e) => setParcel(e.target.value)}
            placeholder="חלקה (לא חובה)"
            className="flex-1 px-3 py-3 border border-gray-200 rounded-md text-sm focus:border-blue-600 focus:outline-none"
          />
        </div>
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="שם לזיהוי (לא חובה)"
          className="px-3 py-3 border border-gray-200 rounded-md text-sm focus:border-blue-600 focus:outline-none"
        />
        <button
          type="submit"
          className="px-5 py-3 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700"
        >
          + הוסף
        </button>
      </form>
      <p className="text-sm text-gray-500 mt-3">
        מספר התכניות הנוכחי בחיפוש נשמר כבסיס. כל תכנית חדשה שתופיע מהרגע הזה
        ואילך תישלח אליכם במייל.
      </p>
    </div>
  );
}
