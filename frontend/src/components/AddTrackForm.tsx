import { useState } from 'react';

export default function AddTrackForm({ onAdd }: { onAdd: (url: string) => void }) {
  const [url, setUrl] = useState('');
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 mb-8">
      <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-3">
        הוספת תכנית למעקב
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const trimmed = url.trim();
          if (trimmed) {
            onAdd(trimmed);
            setUrl('');
          }
        }}
        className="flex gap-2"
      >
        <button
          type="submit"
          className="px-5 py-3 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700"
        >
          + הוסף
        </button>
        <input
          type="url"
          required
          dir="ltr"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://mavat.iplan.gov.il/SV4/1/3005115162/310"
          pattern="https://mavat\.iplan\.gov\.il/.*"
          className="flex-1 px-3 py-3 border border-gray-200 rounded-md text-sm focus:border-blue-600 focus:outline-none"
        />
      </form>
      <p className="text-sm text-gray-500 mt-3">
        המצב הנוכחי של התכנית נשמר כבסיס. כל שינוי מהרגע הזה ואילך יישלח אליכם במייל.
      </p>
    </div>
  );
}
