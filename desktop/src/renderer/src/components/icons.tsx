/**
 * Small pixel-style icons repeated across screens. All draw in currentColor,
 * so color comes from a text-* class on the icon or its parent.
 */

/** 10px disclosure chevron; points down when `open`, right otherwise. */
export function ChevronIcon({ open = true, className = "" }: { open?: boolean; className?: string }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
      className={`shrink-0 transition-transform ${open ? "" : "-rotate-90"} ${className}`}
    >
      <path d="M2 3 L5 6 L8 3" />
    </svg>
  );
}

/** Check mark for filled checkboxes (10px). */
export function CheckMarkIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
      className={className}
    >
      <path d="M1 5 L4 8 L9 2" />
    </svg>
  );
}

/** Square spinner. Size and color via className, e.g. "h-3 w-3 border-text". */
export function Spinner({ className = "h-3 w-3 border-text" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block animate-pxspin border-2 border-t-transparent ${className}`}
    />
  );
}
