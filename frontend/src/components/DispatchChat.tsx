import {
  ArrowUp,
  BatteryLow,
  BookOpen,
  Check,
  ChevronDown,
  CircleCheck,
  CircleX,
  CloudLightning,
  CloudSun,
  Copy,
  Cpu,
  Database,
  LoaderCircle,
  MessageSquare,
  RotateCcw,
  ShieldAlert,
  Snowflake,
  Sparkles,
  Thermometer,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import Markdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError, api } from "../api/client";
import type { ChatResponse, TraceStep } from "../api/client";
import { Button, Card, CardHeader, Pill, usd } from "./ui";
import type { Tone } from "./ui";

const PRESETS: { text: string; icon: LucideIcon; hint: string }[] = [
  { text: "Audit active temperature excursions", icon: Thermometer, hint: "Severity, SOP actions, deadlines" },
  {
    text: "Which trucks are delayed by weather, and what is the wind at their location right now?",
    icon: CloudLightning,
    hint: "Live Open-Meteo at each truck",
  },
  {
    text: "What does the SOP require for a biologics shipment with an open door?",
    icon: BookOpen,
    hint: "Cited policy, no data needed",
  },
  { text: "List trucks with battery below 25% and their ETA", icon: BatteryLow, hint: "Telemetry query" },
];

const CAPABILITIES: { icon: LucideIcon; title: string; body: string }[] = [
  { icon: Database, title: "Live telemetry", body: "Read-only SQL over the margin-free fleet view" },
  { icon: CloudSun, title: "Live weather", body: "Open-Meteo conditions and 6-hour gusts per truck" },
  { icon: BookOpen, title: "Cited SOPs", body: "Every action traced to a policy section" },
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

/** GFM tables need a blank line before them; models often start one right under a sentence. */
function separateTables(markdown: string): string {
  const out: string[] = [];
  for (const line of markdown.split("\n")) {
    const previous = out[out.length - 1];
    const isRow = line.trimStart().startsWith("|");
    if (isRow && previous !== undefined && previous.trim() !== "" && !previous.trimStart().startsWith("|")) {
      out.push("");
    }
    out.push(line);
  }
  return out.join("\n");
}

function SopBadge({ reference }: { reference: string }) {
  const [file, section] = reference.split("§");
  return (
    <span
      className="mx-0.5 inline-flex max-w-full items-center gap-1 rounded-md border border-ok/30 bg-ok/10 px-1.5 py-px align-baseline font-sans text-[11px] font-medium text-ok"
      title={`SOP citation: ${reference}`}
    >
      <BookOpen className="h-3 w-3 shrink-0" aria-hidden />
      <span className="truncate">{section ? `§${section.trim()}` : file?.trim()}</span>
    </span>
  );
}

const markdownComponents: Components = {
  code({ children, className }) {
    const text = String(children ?? "");
    if (text.startsWith(SOP_MARK)) return <SopBadge reference={text.slice(SOP_MARK.length)} />;
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

const NODE_META: Record<TraceStep["node"], { label: string; icon: LucideIcon; tone: Tone }> = {
  agent: { label: "Flash router", icon: Cpu, tone: "accent" },
  tools: { label: "Tool", icon: Wrench, tone: "neutral" },
  sql_fix: { label: "Pro SQL repair", icon: ShieldAlert, tone: "pro" },
  synthesize: { label: "Pro synthesis", icon: Sparkles, tone: "pro" },
};

const TOOL_ICON: Record<string, LucideIcon> = {
  query_fleet_telemetry: Database,
  get_live_weather: CloudSun,
  search_sop: BookOpen,
};

function modelBadge(step: TraceStep): { label: string; tone: Tone } | null {
  if (!step.tier) return null;
  if (step.tier === "flash") return { label: "V4 Flash", tone: "accent" };
  return { label: step.escalation_reason === "synthesis" ? "Pro · Synthesis" : "Pro · Fix", tone: "pro" };
}

interface SopHitPayload {
  citation: string;
  score: number;
  text: string;
}

function sopHits(payload: unknown): SopHitPayload[] {
  if (typeof payload === "object" && payload !== null && "hits" in payload) {
    const hits = (payload as { hits: unknown }).hits;
    if (Array.isArray(hits)) return hits as SopHitPayload[];
  }
  return [];
}

function JsonBlock({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2) ?? "";
  return <pre className="code-block max-h-64">{text.length > 4000 ? `${text.slice(0, 4000)}\n…` : text}</pre>;
}

function StepDetail({ step }: { step: TraceStep }) {
  const hits = step.tool_name === "search_sop" ? sopHits(step.payload) : [];
  return (
    <div className="mt-2 space-y-2">
      {step.executed_sql ? (
        <div>
          <div className="label-caps mb-1">{step.node === "sql_fix" ? "Repaired SQL" : "Executed SQL"}</div>
          <pre className="code-block whitespace-pre-wrap">{step.executed_sql}</pre>
        </div>
      ) : null}
      {hits.length ? (
        <ul className="space-y-1.5">
          {hits.map((hit) => (
            <li key={hit.citation + hit.score} className="rounded-lg border border-line bg-surface-2 p-2">
              <div className="mb-1 flex items-center justify-between gap-2">
                <SopBadge reference={hit.citation.replace(/^\[SOP:\s*|\]$/g, "")} />
                <span className="text-[11px] text-fg-subtle tabular-nums">{hit.score.toFixed(2)}</span>
              </div>
              <p className="line-clamp-3 text-xs whitespace-pre-wrap text-fg-muted">{hit.text}</p>
            </li>
          ))}
        </ul>
      ) : null}
      {step.tool_name && step.tool_name !== "search_sop" && step.payload ? <JsonBlock value={step.payload} /> : null}
      {step.node === "agent" && step.payload ? <JsonBlock value={step.payload} /> : null}
      {step.error ? (
        <div className="rounded-lg border border-danger/30 bg-danger/10 p-2 text-xs text-danger">
          {step.error_code ? <strong>{step.error_code}: </strong> : null}
          {step.error}
        </div>
      ) : null}
      {step.usage ? (
        <div className="text-[11px] text-fg-subtle tabular-nums">
          {step.usage.prompt_tokens.toLocaleString()} in · {step.usage.cached_tokens.toLocaleString()} cached ·{" "}
          {step.usage.completion_tokens.toLocaleString()} out · {usd(step.usage.cost_usd)}
        </div>
      ) : null}
    </div>
  );
}

function TimelineStep({ step, maxLatency, last }: { step: TraceStep; maxLatency: number; last: boolean }) {
  const [open, setOpen] = useState(step.status !== "ok");
  const meta = NODE_META[step.node];
  const Icon = (step.tool_name && TOOL_ICON[step.tool_name]) || meta.icon;
  const badge = modelBadge(step);
  const failed = step.status !== "ok";
  return (
    <li className="relative pl-8">
      {!last ? <span className="absolute top-7 bottom-0 left-[13px] w-px bg-line" aria-hidden /> : null}
      <span
        className={`absolute top-1.5 left-0 flex h-[27px] w-[27px] items-center justify-center rounded-full border ${
          failed ? "border-danger/40 bg-danger/10 text-danger" : "border-line bg-surface-2 text-fg-muted"
        }`}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden />
      </span>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="w-full rounded-lg px-2 py-1.5 text-left hover:bg-surface-2"
      >
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs font-semibold text-fg">{step.tool_name ?? meta.label}</span>
          {badge ? <Pill tone={badge.tone}>{badge.label}</Pill> : null}
          {step.node === "tools" && step.escalation_reason === "sql_fix" ? <Pill tone="pro">repaired SQL</Pill> : null}
          {failed ? (
            <Pill tone="danger" icon={CircleX}>
              {step.error_code ?? step.status}
            </Pill>
          ) : null}
          <span className="ml-auto text-[11px] text-fg-subtle tabular-nums">{step.latency_ms} ms</span>
          <ChevronDown className={`h-3.5 w-3.5 text-fg-subtle transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
        </div>
        <div className="mt-1 flex items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-[11.5px] text-fg-muted">{step.summary}</span>
          <span className="h-1 w-16 overflow-hidden rounded-full bg-surface-3" aria-hidden>
            <span
              className={`block h-full rounded-full ${step.tier === "pro" ? "bg-pro" : "bg-accent"}`}
              style={{ width: `${Math.max(4, (step.latency_ms / Math.max(maxLatency, 1)) * 100)}%` }}
            />
          </span>
        </div>
      </button>
      {open ? (
        <div className="px-2 pb-2">
          <StepDetail step={step} />
        </div>
      ) : null}
    </li>
  );
}

function routeChain(steps: TraceStep[]): { label: string; tone: Tone }[] {
  const chain: { label: string; tone: Tone }[] = [];
  for (const step of steps) {
    const badge = modelBadge(step);
    if (badge && chain[chain.length - 1]?.label !== badge.label) chain.push(badge);
  }
  return chain;
}

function AnswerCard({ result }: { result: ChatResponse }) {
  const [showTrace, setShowTrace] = useState(false);
  const [copied, setCopied] = useState(false);
  const chain = routeChain(result.trace_steps);
  const maxLatency = Math.max(...result.trace_steps.map((s) => s.latency_ms), 1);
  const saved = result.usage.all_pro_cost_usd - result.usage.cost_usd;

  async function copy() {
    try {
      await navigator.clipboard.writeText(result.answer);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <>
      <div className="markdown">
        <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
          {withSopBadges(separateTables(result.answer))}
        </Markdown>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <div className="flex flex-wrap items-center gap-1">
          {chain.map((item, index) => (
            <span key={`${item.label}-${index}`} className="flex items-center gap-1">
              {index > 0 ? <span className="text-[11px] text-fg-subtle">→</span> : null}
              <Pill tone={item.tone}>{item.label}</Pill>
            </span>
          ))}
        </div>
        <span className="text-[11px] text-fg-subtle tabular-nums">
          {(result.latency_ms / 1000).toFixed(1)} s · {usd(result.usage.cost_usd)}
          {saved > 0 ? ` (saved ${usd(saved)})` : ""}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button size="sm" variant="ghost" icon={copied ? Check : Copy} onClick={() => void copy()}>
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            icon={ChevronDown}
            aria-expanded={showTrace}
            onClick={() => setShowTrace((value) => !value)}
            className={showTrace ? "[&>svg]:rotate-180" : ""}
          >
            Execution trace · {result.trace_steps.length}
          </Button>
        </div>
      </div>
      {showTrace ? (
        <ol className="mt-3 space-y-1 rounded-xl border border-line bg-surface p-2 animate-fade-up">
          {result.trace_steps.map((step, index) => (
            <TimelineStep
              key={step.step}
              step={step}
              maxLatency={maxLatency}
              last={index === result.trace_steps.length - 1}
            />
          ))}
        </ol>
      ) : null}
    </>
  );
}

function Welcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mx-auto flex max-w-2xl flex-col items-center py-6 text-center sm:py-10">
      <div className="rounded-2xl bg-gradient-to-br from-accent to-pro p-3 text-white shadow-pop">
        <Snowflake className="h-6 w-6" aria-hidden />
      </div>
      <h3 className="mt-4 text-lg font-semibold text-fg">Ask the control tower</h3>
      <p className="mt-1 text-sm text-fg-muted">
        Plain-language questions, answered from live data and cited policy.
      </p>
      <div className="mt-5 grid w-full gap-2 sm:grid-cols-3">
        {CAPABILITIES.map(({ icon: Icon, title, body }) => (
          <div key={title} className="rounded-xl border border-line bg-surface-2 p-3 text-left">
            <Icon className="h-4 w-4 text-accent" aria-hidden />
            <div className="mt-2 text-xs font-semibold text-fg">{title}</div>
            <div className="mt-0.5 text-[11.5px] text-fg-subtle">{body}</div>
          </div>
        ))}
      </div>
      <div className="mt-4 grid w-full gap-2 sm:grid-cols-2">
        {PRESETS.map(({ text, icon: Icon, hint }) => (
          <button
            key={text}
            type="button"
            onClick={() => onPick(text)}
            className="group flex items-start gap-3 rounded-xl border border-line bg-surface p-3 text-left transition-colors hover:border-accent/50 hover:bg-accent/5"
          >
            <span className="rounded-lg bg-surface-2 p-1.5 text-fg-muted group-hover:text-accent">
              <Icon className="h-4 w-4" aria-hidden />
            </span>
            <span className="min-w-0">
              <span className="block text-[13px] font-medium text-fg">{text}</span>
              <span className="mt-0.5 block text-[11.5px] text-fg-subtle">{hint}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function DispatchChat({
  onTurnComplete,
  prefill,
}: {
  onTurnComplete: () => void;
  prefill: { id: number; text: string } | null;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pending]);

  useEffect(() => {
    if (!prefill) return;
    setDraft(prefill.text);
    inputRef.current?.focus();
  }, [prefill]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }, [draft]);

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
      setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, error: message } : turn)));
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

  return (
    <Card className="flex min-h-[70dvh] flex-col">
      <CardHeader
        icon={MessageSquare}
        title="Autonomous Dispatch Console"
        subtitle="Hybrid DeepSeek routing over live telemetry, weather, and SOPs"
        actions={
          <>
            {sessionId ? <Pill title={sessionId}>Session {sessionId.slice(0, 6)}</Pill> : null}
            <Button
              size="sm"
              icon={RotateCcw}
              disabled={pending || turns.length === 0}
              onClick={() => {
                setTurns([]);
                setSessionId(null);
              }}
            >
              New session
            </Button>
          </>
        }
      />

      <div className="flex-1 space-y-6 overflow-y-auto px-4 py-5 sm:px-6" aria-live="polite">
        {turns.length === 0 ? <Welcome onPick={(text) => void send(text)} /> : null}
        {turns.map((turn) => (
          <article key={turn.id} className="space-y-3 animate-fade-up">
            <div className="flex justify-end">
              <p className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-3.5 py-2 text-sm text-accent-fg shadow-card">
                {turn.question}
              </p>
            </div>
            <div className="flex gap-3">
              <div className="hidden h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-pro text-white sm:flex">
                <Snowflake className="h-4 w-4" aria-hidden />
              </div>
              <div className="min-w-0 flex-1 rounded-2xl rounded-tl-md border border-line bg-surface px-4 py-3.5">
                {turn.result ? (
                  <AnswerCard result={turn.result} />
                ) : turn.error ? (
                  <p className="flex items-center gap-2 text-sm text-danger">
                    <CircleX className="h-4 w-4" aria-hidden />
                    {turn.error}
                  </p>
                ) : (
                  <div className="flex items-center gap-2 text-sm text-fg-muted">
                    <LoaderCircle className="h-4 w-4 animate-spin text-accent" aria-hidden />
                    Querying telemetry, weather, and SOPs…
                  </div>
                )}
              </div>
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>

      <div className="border-t border-line p-3 sm:p-4">
        {turns.length > 0 ? (
          <div className="mb-2 flex gap-1.5 overflow-x-auto pb-1">
            {PRESETS.map(({ text, icon: Icon }) => (
              <button
                key={text}
                type="button"
                disabled={pending}
                onClick={() => void send(text)}
                className="inline-flex shrink-0 items-center gap-1 rounded-full border border-line px-2.5 py-1 text-[11px] text-fg-muted hover:border-accent/50 hover:text-accent disabled:opacity-40"
              >
                <Icon className="h-3 w-3" aria-hidden />
                <span className="max-w-[220px] truncate">{text}</span>
              </button>
            ))}
          </div>
        ) : null}
        <form
          onSubmit={onSubmit}
          className="flex items-end gap-2 rounded-xl border border-line bg-surface-2 p-1.5 focus-within:border-accent"
        >
          <label htmlFor="dispatch-input" className="sr-only">
            Message the copilot
          </label>
          <textarea
            id="dispatch-input"
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            maxLength={4000}
            placeholder="Ask about excursions, weather risk, batteries, or SOP requirements…"
            className="max-h-[180px] min-h-[38px] flex-1 resize-none bg-transparent px-2 py-2 text-sm text-fg placeholder:text-fg-subtle focus:outline-none"
          />
          <button
            type="submit"
            aria-label="Send"
            disabled={pending || !draft.trim()}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg transition-colors hover:bg-accent-strong disabled:opacity-40"
          >
            {pending ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : <ArrowUp className="h-4 w-4" aria-hidden />}
          </button>
        </form>
        <div className="mt-1.5 flex items-center justify-between px-1 text-[11px] text-fg-subtle">
          <span>Enter to send · Shift+Enter for a new line</span>
          <span className="flex items-center gap-1">
            <CircleCheck className="h-3 w-3 text-ok" aria-hidden />
            Read-only · every call audited
          </span>
        </div>
      </div>
    </Card>
  );
}
