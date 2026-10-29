/** History-row actions shared by Home's recent cards and the History list. */

import type { TFunction } from "i18next";

import { toast } from "@/components/Toast";
import { deleteHistorySession } from "@/stores/wizard";

/**
 * Deletes a history row and reports an engine refusal. The store resolves to
 * "deleted" | "running" | "failed" (older builds resolved to undefined, which
 * reads as deleted).
 */
export async function deleteSessionWithToast(sessionId: string, t: TFunction): Promise<void> {
  let outcome: string;
  try {
    outcome = String(await deleteHistorySession(sessionId));
  } catch {
    outcome = "failed";
  }
  if (outcome === "running") toast(t("history.delete.running"), { tone: "warn" });
  else if (outcome === "failed") toast(t("history.delete.failed"), { tone: "err" });
}
