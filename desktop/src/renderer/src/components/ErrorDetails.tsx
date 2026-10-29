/**
 * Raw error text, folded away under a "details" disclosure. Screens show a
 * localized, actionable message first and keep the engine/provider text here
 * for bug reports.
 */

import { useTranslation } from "react-i18next";

export function ErrorDetails({ raw, className = "" }: { raw: string | null | undefined; className?: string }) {
  const { t } = useTranslation();
  const text = raw?.trim() ?? "";
  if (text === "") return null;
  return (
    <details className={`group ${className}`}>
      <summary className="cursor-pointer font-mono text-[10px] text-text3 select-none hover:text-text2">
        {t("common.error.details")}
      </summary>
      <pre className="mt-1 max-h-32 overflow-auto font-mono text-[10px] leading-[1.5] break-words whitespace-pre-wrap text-text3 select-text">
        {text}
      </pre>
    </details>
  );
}
