/**
 * Keyboard support for clickable non-button elements.
 *
 * Used where a real <button> would be invalid (the element already holds a
 * nested button, e.g. a row with a checkbox) or would disturb a grid/flex
 * layout. Enter and Space activate; key events that started on a nested
 * control are ignored so a nested button does not also trigger the row.
 */

import type { KeyboardEvent } from "react";

export function activateOnKey(onActivate: () => void) {
  return (e: KeyboardEvent<HTMLElement>): void => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onActivate();
    }
  };
}

/** Spread onto a <div> to make it behave like a button. */
export function pressable(onActivate: (() => void) | undefined) {
  if (onActivate === undefined) return {};
  return {
    role: "button" as const,
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: activateOnKey(onActivate),
  };
}
