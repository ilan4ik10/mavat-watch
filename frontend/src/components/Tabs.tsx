type TabId = 'tracked' | 'add';

interface Props {
  active: TabId;
  onChange: (tab: TabId) => void;
  trackedCount: number;
}

export default function Tabs({ active, onChange, trackedCount }: Props) {
  const tab = (id: TabId, label: string) => (
    <button
      type="button"
      onClick={() => onChange(id)}
      className={`px-5 py-3 font-medium text-sm border-b-2 -mb-px transition-colors ${
        active === id
          ? 'text-blue-600 border-blue-600'
          : 'text-gray-500 border-transparent hover:text-gray-900'
      }`}
    >
      {label}
    </button>
  );
  return (
    <div className="flex gap-1 mb-6 border-b border-gray-200">
      {tab('tracked', `תכניות במעקב (${trackedCount})`)}
      {tab('add', 'הוספת תכנית')}
    </div>
  );
}
