/**
 * App-wide toasts: transient, toned (ok / warn / err) status messages with an
 * optional action (e.g. "undo"). `toast()` can be called from any screen;
 * `<ToastHost />` is mounted once in App.
 *
 * The store lives here rather than in stores/ because it is pure view state
 * with no persistence and no engine coupling.
 */

import { useEffect, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { create } from "zustand";

export type ToastTone = "ok" | "warn" | "err";

export interface ToastItem {
  id: number;
  text: ReactNode;
  tone: ToastTone;
  /** ms before auto-dismiss; 0 keeps it until dismissed. */
  duration: number;
  action?: { label: string; onClick: () => void };
  /** Runs when the toast leaves without its action being used. */
  onExpire?: () => void;
}

interface ToastStore {
  toasts: ToastItem[];
  push: (toast: Omit<ToastItem, "id">) => number;
  dismiss: (id: number, viaAction?: boolean) => void;
}

const DEFAULT_DURATION: Record<ToastTone, number> = { ok: 3000, warn: 7000, err: 8000 };
const MAX_TOASTS = 4;
let nextId = 1;

export const useToasts = create<ToastStore>((set, get) => ({
  toasts: [],
  push: (toast) => {
    const id = nextId++;
    const all = [...get().toasts, { ...toast, id }];
    // Overflowing toasts expire as if their timer ran out.
    const dropped = all.slice(0, Math.max(0, all.length - MAX_TOASTS));
    set({ toasts: all.slice(-MAX_TOASTS) });
    for (const t of dropped) t.onExpire?.();
    return id;
  },
  dismiss: (id, viaAction = false) => {
    const target = get().toasts.find((t) => t.id === id);
    if (target === undefined) return;
    set({ toasts: get().toasts.filter((t) => t.id !== id) });
    if (!viaAction) target.onExpire?.();
  },
}));

export function toast(
  text: ReactNode,
  options: {
    tone?: ToastTone;
    duration?: number;
    action?: ToastItem["action"];
    onExpire?: () => void;
  } = {},
): number {
  const tone = options.tone ?? "ok";
  return useToasts.getState().push({
    text,
    tone,
    duration: options.duration ?? DEFAULT_DURATION[tone],
    action: options.action,
    onExpire: options.onExpire,
  });
}

const TONE_CLASS: Record<ToastTone, string> = {
  ok: "border-accent",
  warn: "border-amber",
  err: "border-red",
};
const DOT_CLASS: Record<ToastTone, string> = {
  ok: "bg-accent",
  warn: "bg-amber",
  err: "bg-red",
};

function ToastView({ item }: { item: ToastItem }) {
  const { t } = useTranslation();
  const dismiss = useToasts((s) => s.dismiss);

  useEffect(() => {
    if (item.duration <= 0) return;
    const timer = window.setTimeout(() => dismiss(item.id), item.duration);
    return () => window.clearTimeout(timer);
  }, [dismiss, item.duration, item.id]);

  return (
    <div
      className={`animate-fade-in-up pointer-events-auto flex max-w-[420px] items-start gap-2.5 border bg-raised px-3.5 py-2.5 shadow-[0_6px_16px_rgba(0,0,0,0.4)] ${TONE_CLASS[item.tone]}`}
    >
      <span className={`mt-[5px] h-1.5 w-1.5 shrink-0 ${DOT_CLASS[item.tone]}`} />
      <div className="min-w-0 flex-1 font-mono text-[12px] leading-relaxed break-words text-text">
        {item.text}
      </div>
      {item.action !== undefined && (
        <button
          type="button"
          onClick={() => {
            item.action?.onClick();
            dismiss(item.id, true);
          }}
          className="shrink-0 font-mono text-[12px] font-semibold text-accent hover:text-accent-bright"
        >
          {item.action.label}
        </button>
      )}
      <button
        type="button"
        aria-label={t("common.action.close")}
        onClick={() => dismiss(item.id)}
        className="shrink-0 font-mono text-[12px] text-text3 hover:text-text"
      >
        ✕
      </button>
    </div>
  );
}

export function ToastHost() {
  const toasts = useToasts((s) => s.toasts);
  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed right-6 bottom-6 z-[60] flex flex-col items-end gap-2"
    >
      {toasts.map((item) => (
        <ToastView key={item.id} item={item} />
      ))}
    </div>
  );
}
