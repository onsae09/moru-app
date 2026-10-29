import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { initI18n } from "./i18n";
import { installTheme } from "./lib/theme";
import { bootEngineStore } from "./stores/engine";
import "./styles/global.css";

initI18n();
installTheme();
bootEngineStore();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

const rootEl = document.getElementById("root");
if (rootEl === null) throw new Error("missing #root container");

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
