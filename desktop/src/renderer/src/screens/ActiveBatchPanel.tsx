import { useTranslation } from "react-i18next";

import type { ActiveBatch } from "../stores/wizard";

export function ActiveBatchPanel({
  batches,
  limit,
  now,
  glossaryActive,
}: {
  batches: ActiveBatch[];
  limit: number;
  now: number;
  glossaryActive: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="border border-line2 bg-raised px-3 py-2.5">
      <div className="mb-2 flex items-center justify-between font-mono text-[10px] font-semibold tracking-[0.06em] uppercase">
        <div className="flex items-center gap-1.5 text-accent">
          <div className="h-1.5 w-1.5 animate-pxpulse bg-accent" />
          {t("w4.concurrent.title")}
        </div>
        <span className="text-text3">
          {t("w4.concurrent.slots", { active: batches.length, limit })}
        </span>
      </div>
      {batches.length === 0 ? (
        <div className="flex h-[42px] items-center gap-2 border border-line bg-card px-3 font-mono text-[10px] text-text3">
          <div className="h-1.5 w-1.5 animate-pxpulse bg-edge2" />
          {t(glossaryActive ? "w4.concurrent.glossary" : "w4.concurrent.waiting")}
        </div>
      ) : (
        <div className="grid max-h-[174px] grid-cols-5 gap-1.5 overflow-y-auto">
          {batches.map((batch) => {
            const fileName = batch.file.split(/[\\/]/).at(-1) ?? batch.file;
            const elapsed = Math.max(0, Math.floor((now - batch.startedAt) / 1000));
            return (
              <div
                key={batch.requestId}
                title={`${batch.file}\n${batch.key}`}
                className="min-w-0 border border-accent-lo bg-tint px-2 py-1.5"
              >
                <div className="flex items-center justify-between font-mono text-[10px] text-accent">
                  <span>REQ {batch.requestId}</span>
                  <span>{elapsed}s</span>
                </div>
                <div className="mt-0.5 truncate font-mono text-[10px] text-text2">
                  {fileName}
                </div>
                <div className="truncate font-mono text-[10px] text-text3">
                  {batch.key}
                </div>
                <div className="mt-0.5 font-mono text-[10px] text-text3">
                  {t("w4.concurrent.entries", { count: batch.entries })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
