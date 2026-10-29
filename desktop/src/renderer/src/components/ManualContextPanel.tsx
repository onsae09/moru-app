/**
 * Translation aids for the focused entry.
 *
 * Two layers, deliberately:
 *
 * - **Per keystroke, locally** — placeholder and §-code balance, from the
 *   shared token grammar. Zero latency, so it reacts as the user types.
 * - **Debounced, from the engine** — the glossary rules that actually apply to
 *   this entry's lang key, translation-memory matches, sibling lines, and the
 *   authoritative validator verdict. All pure server-side work: no provider, no
 *   API key, no network beyond the loopback engine, so every panel here is
 *   available with nothing configured.
 *
 * Glossary scope matching is NOT reimplemented here. `key_scope` is resolved
 * server-side per lang key; a second copy of that logic in the renderer would
 * drift and quietly show the wrong terms.
 */

import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Entry, EntryContext, ValidationIssue } from "../../../shared/engine";
import { STATUS_COLOR, StatusIcon, tokensOf } from "@/components/EntryText";
import { api } from "@/lib/api";
import { contentKind, fileLabel } from "@/stores/manual";

/** Idle time before asking the engine to validate the draft. */
const VALIDATE_IDLE_MS = 250;

/** One token's expected vs. actual occurrence count. */
interface TokenBalance {
  token: string;
  source: number;
  draft: number;
}

function tokenBalances(source: string, draft: string): TokenBalance[] {
  const counts: Record<string, TokenBalance> = {};
  for (const token of tokensOf(source)) {
    counts[token] ??= { token, source: 0, draft: 0 };
    counts[token].source += 1;
  }
  for (const token of tokensOf(draft)) {
    counts[token] ??= { token, source: 0, draft: 0 };
    counts[token].draft += 1;
  }
  return Object.values(counts);
}

function Section({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <div className="border border-edge bg-card">
      <div className="border-b border-edge px-[10px] py-[5px] font-mono text-[10px] font-bold tracking-[0.06em] text-text3 uppercase">
        {title}
      </div>
      <div className="px-[10px] py-2">{children}</div>
    </div>
  );
}

function InsertButton({
  label,
  value,
  color,
  hint,
  title,
  onInsert,
}: {
  label: string;
  value: string;
  color: string;
  hint?: string;
  title: string;
  onInsert: (text: string) => void;
}): ReactNode {
  return (
    <button
      onClick={() => onInsert(value)}
      title={title}
      className="flex flex-col items-start text-left hover:opacity-80"
    >
      <span className="font-mono text-[10px] text-text3">{label}</span>
      <span className="text-[12px]" style={{ color }}>
        → {value}
      </span>
      {hint !== undefined && (
        <span className="font-mono text-[10px] text-text3">{hint}</span>
      )}
    </button>
  );
}

export function ManualContextPanel({
  jobId,
  entry,
  draft,
  onInsert,
}: {
  jobId: string;
  entry: Entry;
  draft: string;
  onInsert: (text: string) => void;
}): ReactNode {
  const { t } = useTranslation();
  const label = fileLabel(entry.file);
  const balances = tokenBalances(entry.source_text, draft);
  const sectionCount = (entry.source_text.match(/§/g) ?? []).length;
  const draftSections = (draft.match(/§/g) ?? []).length;

  const [context, setContext] = useState<EntryContext | null>(null);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);

  // Context is per entry, so it refetches on selection change only. A failure
  // is survivable: the local panels above still work.
  useEffect(() => {
    let live = true;
    setContext(null);
    api
      .entryContext(jobId, entry.key, entry.file)
      .then((res) => {
        if (live) setContext(res);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [jobId, entry.key, entry.file]);

  // Validation follows the draft, debounced: the local token counts already
  // give instant feedback, so this only has to be the authoritative verdict.
  useEffect(() => {
    if (draft === "") {
      setIssues([]);
      return;
    }
    let live = true;
    const timer = setTimeout(() => {
      api
        .validateDraft(jobId, entry.key, draft, entry.file)
        .then((res) => {
          if (live) setIssues(res.issues);
        })
        .catch(() => undefined);
    }, VALIDATE_IDLE_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [jobId, entry.key, entry.file, draft]);

  const siblings = context?.siblings ?? [];
  const terms = context?.glossary.terms ?? [];
  const properNouns = context?.glossary.proper_nouns ?? [];
  const formatting = context?.glossary.formatting ?? [];
  const tmExact = context?.tm.exact ?? null;
  const tmElsewhere = context?.tm.same_source_elsewhere ?? [];
  const ordered = [
    ...issues.filter((i) => i.severity === "error"),
    ...issues.filter((i) => i.severity !== "error"),
  ];

  return (
    <div className="flex flex-col gap-2 overflow-y-auto p-3">
      {/* Where this string lives */}
      <Section title={t("w5m.context.origin")}>
        <div className="flex flex-col gap-[3px] font-mono text-[11px]">
          <div>
            <span className="text-text3">{t("w5m.context.mod")} </span>
            <span className="text-accent">{context?.mod_id ?? label.mod}</span>
          </div>
          <div>
            <span className="text-text3">{t("w5m.context.kind")} </span>
            <span className="text-text2">
              {context?.content_type ?? contentKind(entry.key)}
            </span>
          </div>
          <div className="break-all text-text3">{entry.file}</div>
        </div>
      </Section>

      {/* Placeholder balance — the most common hand-translation error */}
      {(balances.length > 0 || sectionCount > 0) && (
        <Section title={t("w5m.context.tokens")}>
          <div className="flex flex-col gap-[4px]">
            {balances.map((b) => {
              const ok = b.source === b.draft;
              return (
                <button
                  key={b.token}
                  onClick={() => onInsert(b.token)}
                  title={t("w5m.context.insertToken")}
                  className="flex items-center gap-2 text-left hover:opacity-80"
                >
                  <span
                    className="border px-[3px] font-mono text-[11px]"
                    style={
                      ok
                        ? {
                            background: "rgba(61,220,132,0.12)",
                            borderColor: "rgba(61,220,132,0.3)",
                            color: "#3DDC84",
                          }
                        : {
                            background: "rgba(242,107,107,0.12)",
                            borderColor: "rgba(242,107,107,0.4)",
                            color: "#F26B6B",
                          }
                    }
                  >
                    {b.token}
                  </span>
                  <span
                    className="font-mono text-[10px]"
                    style={{ color: ok ? "#6A7C74" : "#F26B6B" }}
                  >
                    {b.draft} / {b.source}
                  </span>
                </button>
              );
            })}
            {sectionCount > 0 && (
              <div
                className="mt-1 font-mono text-[10px]"
                style={{ color: draftSections === sectionCount ? "#6A7C74" : "#F5B454" }}
              >
                {t("w5m.context.sectionCodes", {
                  draft: draftSections,
                  source: sectionCount,
                })}
              </div>
            )}
          </div>
        </Section>
      )}

      {/* Glossary rules that apply to THIS entry's source AND lang key */}
      {(terms.length > 0 || properNouns.length > 0 || formatting.length > 0) && (
        <Section title={t("w5m.context.glossary")}>
          <div className="flex flex-col gap-[6px]">
            {terms.map((term) => (
              <InsertButton
                key={`${term.aliases.join(",")}:${term.term_ko}`}
                label={term.aliases.join(" · ")}
                value={term.term_ko}
                color="#3DDC84"
                hint={
                  term.key_scope.length > 0
                    ? t("w5m.context.keyScope", { scope: term.key_scope.join(", ") })
                    : undefined
                }
                title={t("w5m.context.insertTerm")}
                onInsert={onInsert}
              />
            ))}
            {properNouns.map((noun) => (
              <InsertButton
                key={noun.source_like}
                label={noun.source_like}
                value={noun.preferred_ko}
                color="#A78BFA"
                title={t("w5m.context.insertTerm")}
                onInsert={onInsert}
              />
            ))}
            {formatting.map((rule) => (
              <div key={rule.rule_name} className="flex flex-col">
                <span className="font-mono text-[10px] text-text3">{rule.rule_name}</span>
                {/* Prose-only guidance: never machine-checked, so it is shown
                    as advice and never as a pass/fail. */}
                <span className="text-[11px] leading-[1.5] text-text2">
                  {rule.description}
                </span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Memory + cross-file consistency */}
      {(tmExact !== null || tmElsewhere.length > 0) && (
        <Section title={t("w5m.context.memory")}>
          <div className="flex flex-col gap-2">
            {tmExact !== null && (
              <InsertButton
                label={t("w5m.context.tmExact", { origin: tmExact.origin })}
                value={tmExact.translated_text}
                color="#3DDC84"
                title={t("w5m.context.insertTerm")}
                onInsert={onInsert}
              />
            )}
            {tmElsewhere.map((other) => (
              <div
                key={`${other.file}:${other.key}`}
                className="flex flex-col gap-[2px] border-l pl-2"
                style={{ borderColor: other.agrees ? "#24322B" : "#F5B454" }}
              >
                <span
                  className="font-mono text-[10px]"
                  style={{ color: other.agrees ? "#6A7C74" : "#F5B454" }}
                >
                  {other.agrees
                    ? t("w5m.context.sameSourceAgrees")
                    : t("w5m.context.sameSourceDiffers")}
                </span>
                <span className="truncate font-mono text-[10px] text-text3">{other.key}</span>
                <button
                  onClick={() => onInsert(other.translated_text)}
                  title={t("w5m.context.insertTerm")}
                  className="text-left text-[11px] text-text2 hover:text-text"
                >
                  {other.translated_text}
                </button>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Sibling lines — one sentence often runs across a numbered run */}
      {siblings.length > 0 && (
        <Section title={t("w5m.context.siblings")}>
          <div className="flex flex-col gap-2">
            <p className="m-0 font-mono text-[10px] leading-relaxed text-text3">
              {t("w5m.context.siblingsHint")}
            </p>
            {siblings.map((s) => (
              <div key={s.key} className="flex flex-col gap-[2px] border-l border-edge2 pl-2">
                <div className="flex items-center gap-[5px]">
                  <StatusIcon status={s.status} size={9} />
                  <span className="truncate font-mono text-[10px] text-text3">{s.key}</span>
                </div>
                <div className="text-[11px] leading-[1.5] text-text2">{s.source_text}</div>
                {s.translated_text !== null && s.translated_text !== "" && (
                  <div
                    className="text-[11px] leading-[1.5]"
                    style={{ color: STATUS_COLOR[s.status] }}
                  >
                    {s.translated_text}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* The engine's verdict on the current draft */}
      {ordered.length > 0 && (
        <Section title={t("w5m.context.checks")}>
          <div className="flex flex-col gap-[5px]">
            {ordered.map((issue, i) => (
              <div key={`${issue.issue_type}:${i}`} className="flex flex-col">
                <span
                  className="font-mono text-[10px] font-bold"
                  style={{ color: issue.severity === "error" ? "#F26B6B" : "#F5B454" }}
                >
                  {/* Localized off issue_type; `message` is English-only. */}
                  {t(`w5m.issue.${issue.issue_type}`, { defaultValue: issue.message })}
                </span>
                {issue.suggestion !== null && issue.suggestion !== undefined && (
                  <span className="text-[11px] text-text2">{issue.suggestion}</span>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
