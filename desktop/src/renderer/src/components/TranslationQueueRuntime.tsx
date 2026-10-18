import { useEffect } from "react";

import { initializeTranslationQueue } from "@/stores/translationQueue";
import { reconcileResumableSessions } from "@/stores/wizard";

/**
 * Starts persisted queue reconciliation only after EngineGate is ready.
 * Resumable checkpoints are folded into history first, so a translation the
 * app was closed in reads as interrupted when the queue looks at its row.
 */
export function TranslationQueueRuntime() {
  useEffect(() => {
    void reconcileResumableSessions().finally(() => initializeTranslationQueue());
  }, []);
  return null;
}
