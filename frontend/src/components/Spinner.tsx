import { useEffect, useState } from 'react';

const STATES = ['', '.', '..', '...'];

export default function Spinner({ text }: { text: string }) {
  const [dots, setDots] = useState('');
  useEffect(() => {
    let i = 0;
    const id = setInterval(() => {
      i = (i + 1) % STATES.length;
      setDots(STATES[i]);
    }, 350);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="py-4 text-center text-gray-500 text-base mb-2">
      <span>{text}</span>
      <span className="inline-block min-w-[1.5em] text-start">{dots}</span>
    </div>
  );
}
