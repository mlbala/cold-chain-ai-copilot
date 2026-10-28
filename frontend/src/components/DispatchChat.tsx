import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleX,
  CloudSun,
  Cpu,
  Database,
  LoaderCircle,
  RotateCcw,
  Send,
  ShieldAlert,
  Sparkles,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import Markdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError, api } from "../api/client";
import type { ChatResponse, TraceStep } from "../api/client";

const PRESETS = [
  "Audit active temperature excursions",
  "Which trucks are delayed by weather, and what is the wind at their location right now?",
  "What does the SOP require for a biologics shipment with an open door?",
  "List trucks with battery below 25% and their ETA",
];

interface Turn {
  id: string;
  question: string;
  result?: ChatResponse;
  error?: string;
}

const SOP_TAG = /\[SOP:\s*([^\]]+)\]/g;
const SOP_MARK = "SOP::";

function withSopBadges(markdown: string): string {
  return markdown.replace(SOP_TAG, (_match, ref: string) => `\`${SOP_MARK}${ref.trim()}\``);
}

function SopBadge({ reference }: { reference: string }) {
  return (
    <span
      className="mx-0.5 inline-flex items-center gap-1 rounded-md border border-ok/40 bg-ok/10 px-1.5 py-0.5 align-middle font-sans text-[11px] font-medium text-ok"
      title={`SOP citation: ${reference}`}
    >
      <BookOpen className="h-3 w-3" aria-hidden />
      {reference}
    </span>
  );
}

const markdownComponents: Components = {
  code({ children, className }) {
    const text = String(children ?? "");
    if (text.startsWith(SOP_MARK)) {
      return <SopBadge reference={text.slice(SOP_MARK.length)} />;
    }
    return <code className={className}>{children}</code>;
  },
  table({ children }) {
    return (
      <div className="table-wrap">
        <table>{children}</table>
      </div>
    );
  },
};

function modelBadge(step: TraceStep): { label: string; style: string } | null {
  if (!step.tier) return null;
  if (step.tier === "flash") {
    return { label: "DeepSeek V4 Flash", style: "border-signal/40 bg-signal/10 text-signal" };
  }
  const reason = step.escalation_reason === "synthesis" ? "Synthesis" : "Fix";
  return { label: `Escalated: Pro (${reason})`, style: "border-pro/40 bg-pro/10 text-pro" };
}

const nodeMeta: Record<TraceStep["node"], { label: string; icon: LucideIcon }> = {
  agent: { label: "Router", icon: Cpu },
  tools: { label: "Tool", icon: Wrench },
  sql_fix: { label: "SQL self-fix", icon: ShieldAlert },
  synthesize: { label: "Synthesis", icon: Sparkles },
};

const toolIcon: Record<string, LucideIcon> = {
  query_fleet_telemetry: Database,
  get_live_weather: CloudSun,
  search_sop: BookOpen,
};

function Pill({ children, className }: { children: ReactNode; className: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium ${className}`}
    >
      {children}
    </span>
  );
}

function StatusPill({ status }: { status: TraceStep["status"] }) {
  if (status === "ok") {
    return (
      <Pill className="border-ok/40 bg-ok/10 text-ok">
        <CircleCheck className="h-3 w-3" aria-hidden /> ok
      </Pill>
    );
  }
  return (
    <Pill className="border-danger/40 bg-danger/10 text-danger">
      <CircleX className="h-3 w-3" aria-hidden /> {status}
    </Pill>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2) ?? "";
  const clipped = text.length > 4000 ? `${text.slice(0, 4000)}\n…` : text;
  return <pre className="code-block max-h-72">{clipped}</pre>;
}

interface SopHitPayload {
  citation: string;
  score: number;
  text: string;
}

function isSopHits(payload: unknown): payload is { hits: SopHitPayload[] } {
  return (
    typeof payload === "object" &&
    payload !== null &&
    "hits" in payload &&
    Array.isArray((payload as { hits: unknown }).hits)
  );
}

function StepDetail({ step }: { step: TraceStep }) {
  return (
    <div className="space-y-2 pt-2">
      {step.executed_sql ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wider text-ink-400">
            {step.node === "sql_fix" ? "Repaired SQL" : "Executed SQL"} · fde_views.vw_active_fleet
          </div>
          <pre className="code-block whitespace-pre-wrap">{step.executed_sql}</pre>
        </div>
      ) : null}

      {step.tool_name === "search_sop" && isSopHits(step.payload) ? (
        <ul className="space-y-2">
          {step.payload.hits.map((hit) => (
            <li key={hit.citation + hit.score} className="rounded-lg border border-ink-700 p-2">
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <SopBadge reference={hit.citation.replace(/^\[SOP:\s*|\]$/g, "")} />
                <span className="text-[11px] text-ink-400">relevance {hit.score.toFixed(2)}</span>
              </div>
              <p className="line-clamp-4 whitespace-pre-wrap text-xs text-ink-300">{hit.text}</p>
            </li>
          ))}
        </ul>
      ) : null}

      {step.tool_name === "get_live_weather" && step.payload ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wider text-ink-400">
            Open-Meteo payload
          </div>
          <JsonBlock value={step.payload} />
        </div>
      ) : null}

      {step.tool_name === "query_fleet_telemetry" && step.payload ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wider text-ink-400">
            Result preview
          </div>
          <JsonBlock value={step.payload} />
        </div>
      ) : null}

      {step.node === "agent" && step.payload ? <JsonBlock value={step.payload} /> : null}

      {step.error ? (
        <div className="rounded-lg border border-danger/40 bg-danger/10 p-2 text-xs text-danger">
          {step.error_code ? <strong>{step.error_code}: </strong> : null}
          {step.error}
        </div>
      ) : null}

      {step.usage ? (
        <div className="text-[11px] text-ink-400">
          {step.usage.prompt_tokens.toLocaleString()} in ({step.usage.cached_tokens.toLocaleString()}{" "}
          cached) · {step.usage.completion_tokens.toLocaleString()} out · $
          {step.usage.cost_usd.toFixed(6)}
        </div>
      ) : null}
    </div>
  );
}

function TraceStepRow({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(step.status !== "ok");
  const meta = nodeMeta[step.node];
  const Icon = (step.tool_name && toolIcon[step.tool_name]) || meta.icon;
  const badge = modelBadge(step);

  return (
    <li className="rounded-lg border border-ink-700 bg-ink-850">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 text-ink-400" aria-hidden />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-ink-400" aria-hidden />
        )}
        <span className="text-[11px] tabular-nums text-ink-400">#{step.step}</span>
        <Icon className="h-3.5 w-3.5 text-ink-300" aria-hidden />
        <span className="text-xs font-medium text-ink-100">{step.tool_name ?? meta.label}</span>
        {badge ? <Pill className={badge.style}>{badge.label}</Pill> : null}
        {step.node === "tools" && step.escalation_reason === "sql_fix" ? (
          <Pill className="border-pro/40 bg-pro/10 text-pro">retry with repaired SQL</Pill>
        ) : null}
        <StatusPill status={step.status} />
        <span className="min-w-0 flex-1 truncate text-xs text-ink-400">{step.summary}</span>
        <span className="text-[11px] tabular-nums text-ink-400">{step.latency_ms} ms</span>
      </button>
      {open ? (
        <div className="border-t border-ink-700 px-3 pb-3">
          <StepDetail step={step} />
        </div>
      ) : null}
    </li>
  );
}

function routeChain(steps: TraceStep[]): string[] {
  const chain: string[] = [];
  for (const step of steps) {
    const badge = modelBadge(step);
    if (badge && chain[chain.length - 1] !== badge.label) chain.push(badge.label);
  }
  return chain;
}

function TraceDrawer({ result }: { result: ChatResponse }) {
  const [open, setOpen] = useState(false);
  const chain = routeChain(result.trace_steps);

  return (
    <div className="mt-3 rounded-lg border border-ink-700 bg-ink-900">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-2 px-3 py-2 text-left text-xs text-ink-300"
      >
        {open ? (
          <ChevronDown className="h-4 w-4" aria-hidden />
        ) : (
          <ChevronRight className="h-4 w-4" aria-hidden />
        )}
        <span className="font-medium text-ink-100">Live Execution Trace</span>
        <span className="text-ink-400">
          {result.trace_steps.length} steps · {(result.latency_ms / 1000).toFixed(1)} s · $
          {result.usage.cost_usd.toFixed(5)}
        </span>
        <span className="flex flex-wrap items-center gap-1">
          {chain.map((label, index) => (
            <span key={`${label}-${index}`} className="flex items-center gap-1">
              {index > 0 ? <span className="text-ink-400">→</span> : null}
              <Pill
                className={
                  label.startsWith("Escalated")
                    ? "border-pro/40 bg-pro/10 text-pro"
                    : "border-signal/40 bg-signal/10 text-signal"
                }
              >
                {label}
              </Pill>
            </span>
          ))}
        </span>
      </button>
      {open ? (
        <ol className="space-y-2 border-t border-ink-700 p-3">
          {result.trace_steps.map((step) => (
            <TraceStepRow key={step.step} step={step} />
          ))}
        </ol>
      ) : null}
    </div>
  );
}

export default function DispatchChat({ onTurnComplete }: { onTurnComplete: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pending]);

  async function send(question: string) {
    const text = question.trim();
    if (!text || pending) return;
    const id = crypto.randomUUID();
    setTurns((current) => [...current, { id, question: text }]);
    setDraft("");
    setPending(true);
    try {
      const result = await api.chat(text, sessionId);
      setSessionId(result.session_id);
      setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, result } : turn)));
      onTurnComplete();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Unexpected error";
      setTurns((current) =>
        current.map((turn) => (turn.id === id ? { ...turn, error: message } : turn)),
      );
    } finally {
      setPending(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void send(draft);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send(draft);
    }
  }

  function resetSession() {
    setTurns([]);
    setSessionId(null);
  }

  return (
    <section aria-label="Autonomous Dispatch Console" className="panel flex min-h-[60vh] flex-col">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold">Autonomous Dispatch Console</h2>
          <p className="text-xs text-ink-400">
            Answers are grounded in live telemetry, Open-Meteo weather, and cited SOP excerpts.
          </p>
        </div>
        <button
          type="button"
          onClick={resetSession}
          disabled={pending || turns.length === 0}
          className="inline-flex items-center gap-1.5 rounded-lg border border-ink-700 px-2.5 py-1.5 text-xs text-ink-300 hover:border-ink-600 hover:text-ink-100 disabled:opacity-40"
        >
          <RotateCcw className="h-3.5 w-3.5" aria-hidden />
          New session
        </button>
      </header>

      <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4" aria-live="polite">
        {turns.length === 0 ? (
          <div className="mx-auto max-w-2xl py-8 text-center">
            <p className="text-sm text-ink-300">
              Ask about excursions, weather risk, batteries, or what the SOP requires.
            </p>
          </div>
        ) : null}

        {turns.map((turn) => (
          <article key={turn.id} className="space-y-3">
            <div className="flex justify-end">
              <p className="max-w-[85%] rounded-xl rounded-br-sm bg-signal-strong/90 px-3 py-2 text-sm text-white">
                {turn.question}
              </p>
            </div>
            <div className="max-w-full rounded-xl rounded-bl-sm border border-ink-700 bg-ink-850 p-4">
              {turn.result ? (
                <>
                  <div className="markdown">
                    <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                      {withSopBadges(turn.result.answer)}
                    </Markdown>
                  </div>
                  <TraceDrawer result={turn.result} />
                </>
              ) : turn.error ? (
                <p className="text-sm text-danger">{turn.error}</p>
              ) : (
                <p className="flex items-center gap-2 text-sm text-ink-300">
                  <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
                  Querying telemetry, weather, and SOPs…
                </p>
              )}
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>

      <div className="border-t border-ink-700 p-3">
        <div className="mb-2 flex flex-wrap gap-2">
          {PRESETS.map((preset) => (
            <button
              key={preset}
              type="button"
              disabled={pending}
              onClick={() => void send(preset)}
              className="rounded-full border border-ink-700 bg-ink-850 px-3 py-1 text-xs text-ink-300 hover:border-signal/60 hover:text-signal disabled:opacity-40"
            >
              {preset}
            </button>
          ))}
        </div>
        <form onSubmit={onSubmit} className="flex items-end gap-2">
          <label htmlFor="dispatch-input" className="sr-only">
            Message the copilot
          </label>
          <textarea
            id="dispatch-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            maxLength={4000}
            placeholder="e.g. Which pharma shipments are at risk in the next 6 hours?"
            className="min-h-[2.75rem] flex-1 resize-y rounded-lg border border-ink-700 bg-ink-950 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-400 focus:border-signal focus:outline-none"
          />
          <button
            type="submit"
            disabled={pending || !draft.trim()}
            className="inline-flex h-11 items-center gap-1.5 rounded-lg bg-signal-strong px-4 text-sm font-medium text-white hover:bg-signal disabled:opacity-40"
          >
            {pending ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Send className="h-4 w-4" aria-hidden />
            )}
            Send
          </button>
        </form>
        {sessionId ? (
          <p className="mt-2 text-[11px] text-ink-400">
            Session {sessionId.slice(0, 8)} · memory keeps the last few turns
          </p>
        ) : null}
      </div>
    </section>
  );
}
