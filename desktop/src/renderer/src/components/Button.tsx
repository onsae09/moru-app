/**
 * Shared button: primary / secondary / ghost / danger at sm / md.
 * Square corners, accent fill for primary, on-accent ink (text-sel-ink).
 * Extra classes go through `className`; there is no class merging, so they
 * must not collide with the variant/size utilities (pass size={null} to set
 * padding or font size yourself).
 */

import { forwardRef, type ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "neutral" | "ghost" | "danger";
/** `null` emits no padding / font size, for call sites that set their own. */
export type ButtonSize = "sm" | "md" | null;

const VARIANT: Record<ButtonVariant, string> = {
  primary: "bg-accent font-bold text-sel-ink enabled:hover:bg-accent-hi",
  secondary:
    "border border-edge font-semibold text-text2 enabled:hover:border-edge2 enabled:hover:text-text",
  neutral: "bg-line2 font-bold text-text enabled:hover:bg-edge",
  ghost: "font-semibold text-text2 enabled:hover:text-text",
  danger: "bg-red font-bold text-sel-ink enabled:hover:bg-red-hi",
};

const SIZE: Record<"sm" | "md", string> = {
  sm: "px-3.5 py-2 text-body",
  md: "px-5 py-[10px] text-ui",
};

export function buttonClass(
  variant: ButtonVariant = "secondary",
  size: ButtonSize = "md",
  extra = "",
): string {
  return `inline-flex shrink-0 cursor-pointer items-center justify-center gap-[6px] whitespace-nowrap disabled:cursor-not-allowed disabled:opacity-40 ${VARIANT[variant]} ${size === null ? "" : SIZE[size]} ${extra}`.trim();
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", className = "", type = "button", ...rest },
  ref,
) {
  return <button ref={ref} type={type} className={buttonClass(variant, size, className)} {...rest} />;
});
