"use client";

import { useEffect, useMemo, useState } from "react";
import { faDigits } from "@/lib/format";
import { MONTHS, WEEKDAYS, addJalaliDays, monthLength, padJalali, parseJalali, todayJalali, weekdayIndex } from "@/lib/jalali";

export function JalaliDateField({
  value,
  onChange,
  minExclusive,
  maxExclusive,
}: {
  value: string;
  onChange: (value: string) => void;
  minExclusive?: string;
  maxExclusive?: string;
}) {
  const today = todayJalali();
  const selected = parseJalali(value);
  const opening = minExclusive ? parseJalali(addJalaliDays(minExclusive, 1)) : null;
  const initial = selected || opening || parseJalali(today)!;
  const [view, setView] = useState({ jy: initial.jy, jm: initial.jm });
  useEffect(() => {
    const parsed = parseJalali(value) || (minExclusive ? parseJalali(addJalaliDays(minExclusive, 1)) : null);
    if (parsed) setView({ jy: parsed.jy, jm: parsed.jm });
  }, [value, minExclusive]);
  const cells = useMemo(() => {
    const length = monthLength(view.jy, view.jm);
    const lead = weekdayIndex({ jy: view.jy, jm: view.jm, jd: 1 });
    const items: Array<{ day: number } | null> = Array.from({ length: lead }, () => null);
    for (let day = 1; day <= length; day += 1) items.push({ day });
    return items;
  }, [view]);

  function shift(delta: number) {
    let jm = view.jm + delta;
    let jy = view.jy;
    if (jm < 1) {
      jm = 12;
      jy -= 1;
    }
    if (jm > 12) {
      jm = 1;
      jy += 1;
    }
    setView({ jy, jm });
  }

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3">
      <div className="mb-2 flex items-center justify-between">
        <button type="button" className="min-h-11 min-w-11 rounded-xl hover:bg-slate-100" onClick={() => shift(-1)} aria-label="ماه قبل">
          ›
        </button>
        <div className="font-bold">
          {MONTHS[view.jm - 1]} {faDigits(view.jy)}
        </div>
        <button type="button" className="min-h-11 min-w-11 rounded-xl hover:bg-slate-100" onClick={() => shift(1)} aria-label="ماه بعد">
          ‹
        </button>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center text-xs text-slate-500">
        {WEEKDAYS.map((day) => (
          <div key={day} className="py-1">{day}</div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {cells.map((cell, index) => {
          if (!cell) return <div key={`e-${index}`} />;
          const iso = padJalali({ jy: view.jy, jm: view.jm, jd: cell.day });
          const disabled = Boolean((minExclusive && iso <= minExclusive) || (maxExclusive && iso >= maxExclusive));
          const active = iso === value;
          return (
            <button
              key={iso}
              type="button"
              disabled={disabled}
              onClick={() => onChange(iso)}
              className={`min-h-11 rounded-xl text-sm ${active ? "bg-brand text-white" : "hover:bg-teal-50"} disabled:text-slate-300 disabled:hover:bg-transparent`}
            >
              {faDigits(cell.day)}
            </button>
          );
        })}
      </div>
    </div>
  );
}
