/**
 * Guard for every "start over" entry point (Sidebar "new translation" and
 * Cmd/Ctrl+N, Home's empty-state CTA, W6 "home" / "new modpack").
 *
 * `wizard.reset()` detaches the live job streams and drops the current
 * session from the wizard, so it must not run silently while a scan,
 * translation or export is in flight, nor right after a finished run whose
 * output was never exported.
 */

import { useCallback, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { useRouter, type Screen } from "@/stores/router";
import { useWizard } from "@/stores/wizard";

import { ConfirmDialog } from "./ConfirmDialog";

type WizardSnapshot = Pick<
  ReturnType<typeof useWizard.getState>,
  "scanState" | "runState" | "exportState" | "sourceExportState"
>;

export type ResetBlocker =
  | { kind: "running"; screen: Screen; job: "scan" | "translate" | "export" }
  | { kind: "unexported" };

/** Why resetting the wizard now would lose work, or null when it is safe. */
export function resetBlocker(state: WizardSnapshot): ResetBlocker | null {
  if (state.scanState === "running") return { kind: "running", screen: "w2", job: "scan" };
  if (state.runState === "running") return { kind: "running", screen: "w4", job: "translate" };
  if (state.exportState === "running") return { kind: "running", screen: "w6", job: "export" };
  if (state.sourceExportState === "running") {
    return { kind: "running", screen: "w3", job: "export" };
  }
  if (state.runState === "done" && state.exportState !== "done") return { kind: "unexported" };
  return null;
}

interface Pending {
  blocker: ResetBlocker;
  target: Screen;
}

/**
 * `requestReset(target)` resets and navigates when safe, otherwise opens a
 * confirmation. Render `dialog` once in the calling component.
 */
export function useWizardResetGuard(): {
  requestReset: (target: Screen) => void;
  dialog: ReactNode;
} {
  const { t } = useTranslation();
  const [pending, setPending] = useState<Pending | null>(null);

  const doReset = useCallback((target: Screen): void => {
    useWizard.getState().reset();
    useRouter.getState().go(target);
  }, []);

  const requestReset = useCallback(
    (target: Screen): void => {
      if (pending !== null) return;
      const blocker = resetBlocker(useWizard.getState());
      if (blocker === null) doReset(target);
      else setPending({ blocker, target });
    },
    [doReset, pending],
  );

  const close = (): void => setPending(null);
  let dialog: ReactNode = null;

  if (pending !== null && pending.blocker.kind === "running") {
    const { screen, job } = pending.blocker;
    const here = useRouter.getState().screen === screen;
    dialog = (
      <ConfirmDialog
        open
        title={t(`common.resetGuard.running.title.${job}`)}
        body={t("common.resetGuard.running.body")}
        cancelLabel={here ? t("common.action.close") : t("common.action.cancel")}
        confirmLabel={here ? undefined : t("common.resetGuard.running.goToProgress")}
        onCancel={close}
        onConfirm={() => {
          close();
          useRouter.getState().go(screen);
        }}
      />
    );
  } else if (pending !== null) {
    const { target } = pending;
    dialog = (
      <ConfirmDialog
        open
        variant="danger"
        title={t("common.resetGuard.unexported.title")}
        body={t("common.resetGuard.unexported.body")}
        confirmLabel={t("common.resetGuard.unexported.confirm")}
        onCancel={close}
        onConfirm={() => {
          close();
          doReset(target);
        }}
      />
    );
  }

  return { requestReset, dialog };
}
