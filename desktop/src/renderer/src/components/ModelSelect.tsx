/**
 * Searchable model combobox for the W3 advanced panel. A native <select>
 * became unusable once live provider catalogs landed (OpenRouter alone
 * serves 300+ models), so this filters by display name or raw model id.
 * ARIA combobox pattern: a text input controls a listbox; backdrop click,
 * Escape, Tab, or picking an option closes it.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { modelDisplayName } from "@/lib/models";

/**
 * Dropdown rows for a query: the persisted value stays pickable even when
 * a catalog refresh drops it, and the query matches the raw model id or
 * its display name ("4.5" finds "claude-haiku-4-5"), case-insensitively.
 */
export function modelSearchResults(
  options: readonly string[],
  value: string,
  query: string,
): readonly string[] {
  const all = options.includes(value) ? options : [value, ...options];
  const q = query.trim().toLowerCase();
  if (q.length === 0) return all;
  return all.filter(
    (m) => m.toLowerCase().includes(q) || modelDisplayName(m).toLowerCase().includes(q),
  );
}

export function ModelSelect({
  value,
  options,
  onSelect,
  labelId,
}: {
  value: string;
  options: readonly string[];
  onSelect: (model: string) => void;
  /** id of the element naming this combobox (the field caption). */
  labelId?: string;
}) {
  const { t } = useTranslation();
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const allOptions = useMemo(() => modelSearchResults(options, value, ""), [options, value]);
  const filtered = useMemo(
    () => modelSearchResults(options, value, query),
    [options, value, query],
  );

  const openList = () => {
    setQuery("");
    const index = allOptions.indexOf(value);
    setActive(index >= 0 ? index : 0);
    setOpen(true);
  };
  /* closing unmounts the focused search input; hand focus back to the
     trigger (skipped for Tab, where the browser then advances from it) */
  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };
  const pick = (model: string) => {
    onSelect(model);
    close();
  };

  /* refetch or typing can shrink the list while open; keep the cursor in range */
  useEffect(() => {
    setActive((i) => Math.min(i, Math.max(filtered.length - 1, 0)));
  }, [filtered.length]);

  /* keep the keyboard cursor visible while it moves */
  useEffect(() => {
    if (!open) return;
    listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (filtered.length > 0) setActive((i) => (i + 1) % filtered.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (filtered.length > 0) setActive((i) => (i - 1 + filtered.length) % filtered.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const model = filtered[active];
      if (model !== undefined) pick(model);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={labelId}
        onClick={() => (open ? close() : openList())}
        className="flex w-full cursor-pointer items-center gap-2 border border-edge bg-ink px-[10px] py-[7px] text-left font-mono text-[12px] text-text hover:border-edge2"
      >
        <span className="min-w-0 flex-1 truncate">{modelDisplayName(value)}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          className="text-text4 shrink-0"
        >
          <path d="M2 3 L5 6 L8 3" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={close} />
          <div className="absolute top-full right-0 left-0 z-20 mt-1 border border-edge bg-raised">
            <input
              autoFocus
              role="combobox"
              aria-expanded={open}
              aria-labelledby={labelId}
              aria-controls={listboxId}
              aria-activedescendant={
                filtered.length > 0 ? `${listboxId}-${active}` : undefined
              }
              aria-autocomplete="list"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActive(0);
              }}
              onKeyDown={onKeyDown}
              placeholder={t("w3.advanced.modelSearchPlaceholder")}
              className="w-full border-b border-line2 bg-ink px-[10px] py-[7px] font-mono text-[12px] text-text outline-none placeholder:text-text3 focus:border-accent"
            />
            {filtered.length === 0 ? (
              <div className="px-[10px] py-3 font-mono text-[11px] text-text3">
                {t("w3.advanced.modelNoMatch", { q: query.trim() })}
              </div>
            ) : (
              <ul
                ref={listRef}
                id={listboxId}
                role="listbox"
                className="m-0 max-h-[240px] list-none overflow-y-auto p-0"
              >
                {filtered.map((m, i) => {
                  const selected = m === value;
                  return (
                    <li
                      key={m}
                      id={`${listboxId}-${i}`}
                      role="option"
                      aria-selected={selected}
                      onMouseEnter={() => setActive(i)}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => pick(m)}
                      className={`relative cursor-pointer px-[10px] py-[6px] ${i === active ? "bg-hover" : ""}`}
                    >
                      {selected && (
                        <div className="absolute inset-y-[6px] left-0 w-[3px] bg-accent" />
                      )}
                      <div
                        className={`truncate text-[12px] ${selected ? "font-bold text-accent" : "text-text2"}`}
                      >
                        {modelDisplayName(m)}
                      </div>
                      <div className="truncate font-mono text-[10px] text-text3">{m}</div>
                    </li>
                  );
                })}
              </ul>
            )}
            {query.trim().length > 0 && filtered.length > 0 && (
              <div className="border-t border-line2 px-[10px] py-[5px] font-mono text-[10px] text-text3">
                {t("w3.advanced.modelMatchCount", {
                  n: filtered.length,
                  total: allOptions.length,
                })}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
