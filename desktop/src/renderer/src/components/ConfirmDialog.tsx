/**
 * Shared modal confirmation dialog.
 *
 * role="dialog" + aria-modal, labelled by its title. Esc and a backdrop click
 * cancel; Tab is trapped inside; the safe (cancel) button takes initial focus
 * and focus returns to the opener when the dialog unmounts.
 *
 * Key events are stopped at the dialog root so window-level shortcuts
 * (Sidebar's Cmd/Ctrl+N, W5Review's single-key actions) do not fire while it
 * is open.
 */

import { useEffect, useId, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "./Button";

export interface ConfirmDialogProps {
  open: boolean;
  title: ReactNode;
  body?: ReactNode;
  /** Small mono label above the title. */
  eyebrow?: ReactNode;
  /** Omit for an informational dialog with only the cancel/close button. */
  confirmLabel?: ReactNode;
  /** Defaults to common.action.cancel. */
  cancelLabel?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
  variant?: "default" | "danger";
  /** Disables the confirm button (an action already in flight). */
  busy?: boolean;
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function ConfirmDialog(props: ConfirmDialogProps) {
  if (!props.open) return null;
  return <DialogPanel {...props} />;
}

function DialogPanel({
  title,
  body,
  eyebrow,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
  variant = "default",
  busy = false,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const bodyId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const opener = document.activeElement;
    cancelRef.current?.focus();
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
      return;
    }
    if (e.key !== "Tab" || rootRef.current === null) return;
    const nodes = Array.from(rootRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const current = document.activeElement;
    if (e.shiftKey && (current === first || !rootRef.current.contains(current))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (current === last || !rootRef.current.contains(current))) {
      e.preventDefault();
      first.focus();
    }
  };

  const danger = variant === "danger";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onCancel}
      onKeyDown={onKeyDown}
    >
      <div
        ref={rootRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={body !== undefined ? bodyId : undefined}
        className="w-[400px] max-w-[calc(100vw-32px)] border border-edge bg-raised p-5"
        onClick={(e) => e.stopPropagation()}
      >
        {eyebrow !== undefined && (
          <div className="mb-2 font-mono text-micro font-semibold tracking-[0.08em] text-text3 uppercase">
            {eyebrow}
          </div>
        )}
        <div id={titleId} className="mb-2 text-title font-bold text-text">
          {title}
        </div>
        {body !== undefined && (
          <div id={bodyId} className="mb-5 text-body leading-relaxed text-text2">
            {body}
          </div>
        )}
        <div className={`flex justify-end gap-2 ${body === undefined ? "mt-5" : ""}`}>
          <Button ref={cancelRef} variant="secondary" size="sm" onClick={onCancel}>
            {cancelLabel ?? t("common.action.cancel")}
          </Button>
          {confirmLabel !== undefined && (
            <Button
              variant={danger ? "danger" : "primary"}
              size="sm"
              disabled={busy}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
