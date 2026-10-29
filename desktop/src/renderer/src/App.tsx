import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "@/lib/api";
import { WEB_URL } from "@/lib/web";
import { useSettings } from "@/stores/settings";
import { EngineGate } from "@/components/EngineGate";
import { UpdateGate } from "@/components/UpdateGate";
import { Sidebar } from "@/components/Sidebar";
import { Titlebar } from "@/components/Titlebar";
import { ToastHost } from "@/components/Toast";
import { TranslationQueueRuntime } from "@/components/TranslationQueueRuntime";
import { WizardLayout } from "@/components/WizardLayout";
import { GlossaryScreen } from "@/screens/Glossary";
import { HistoryScreen } from "@/screens/History";
import { HomeScreen } from "@/screens/Home";
import { OnboardingScreen } from "@/screens/Onboarding";
import { SettingsScreen } from "@/screens/Settings";
import { TranslationQueueScreen } from "@/screens/TranslationQueue";
import { W1Select } from "@/screens/W1Select";
import { W2Scan } from "@/screens/W2Scan";
import { W3Settings } from "@/screens/W3Settings";
import { W4Progress } from "@/screens/W4Progress";
import { W5Review } from "@/screens/W5Review";
import { W5Manual } from "@/screens/W5Manual";
import { W6Export } from "@/screens/W6Export";
import { WIZARD_SCREENS, useRouter } from "@/stores/router";

const WIZARD_BODIES = {
  w1: W1Select,
  w2: W2Scan,
  w3: W3Settings,
  w4: W4Progress,
  w5: W5Review,
  w5m: W5Manual,
  w6: W6Export,
} as const;

// Module-level (not a ref): StrictMode remounts would reset a ref and
// double-fire the sync; the engine restarting later must not re-run it either.
let startupSyncDone = false;

/**
 * One-shot community pull on app launch. Mounted inside EngineGate so the
 * sidecar is up. Best-effort: offline/web-down is silent - the pre-run
 * sync in startTranslate and the glossary screen's manual sync still cover
 * later updates. Unchanged versions are manifest-only no-ops (cheap).
 */
function StartupCommunitySync() {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (startupSyncDone) return;
    startupSyncDone = true;
    const { targetLocale } = useSettings.getState();
    void api
      .syncCommunity(WEB_URL, targetLocale)
      .then((sync) => {
        if (sync.glossary?.updated === true || sync.tm?.updated === true) {
          void queryClient.invalidateQueries({ queryKey: ["glossary"] });
          void queryClient.invalidateQueries({ queryKey: ["tm-stats"] });
        }
      })
      .catch(() => {
        // best-effort: nothing to surface at boot
      });
  }, [queryClient]);
  return null;
}

function MainContent() {
  const screen = useRouter((s) => s.screen);
  if (WIZARD_SCREENS.includes(screen)) {
    const Body = WIZARD_BODIES[screen as keyof typeof WIZARD_BODIES];
    return (
      <WizardLayout>
        <Body />
      </WizardLayout>
    );
  }
  switch (screen) {
    case "history":
      return <HistoryScreen />;
    case "queue":
      return <TranslationQueueScreen />;
    case "glossary":
      return <GlossaryScreen />;
    case "settings":
      return <SettingsScreen />;
    default:
      return <HomeScreen />;
  }
}

export default function App() {
  const onboarded = useRouter((s) => s.onboarded);
  const screen = useRouter((s) => s.screen);
  // First run (or Settings replay): the wizard takes over the whole area
  // below the titlebar - no sidebar. Still inside EngineGate because step 2
  // talks to the engine (/providers).
  const showOnboarding = !onboarded || screen === "onboarding";
  return (
    <div className="flex h-dvh min-h-0 min-w-0 flex-col overflow-hidden bg-bg">
      <Titlebar />
      <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
        {/* UpdateGate outside EngineGate: a mandatory update blocks the
            app even when the sidecar failed to boot. */}
        <UpdateGate>
          <EngineGate>
            <StartupCommunitySync />
            <TranslationQueueRuntime />
            {showOnboarding ? (
              <OnboardingScreen />
            ) : (
              <>
                <Sidebar />
                <main className="relative min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto bg-bg">
                  <MainContent />
                </main>
              </>
            )}
          </EngineGate>
        </UpdateGate>
      </div>
      <ToastHost />
    </div>
  );
}
