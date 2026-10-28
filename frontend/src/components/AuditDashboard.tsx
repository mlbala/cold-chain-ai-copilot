import {
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  CircleX,
  FileClock,
  LoaderCircle,
  Lock,
  LockOpen,
  Play,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { ApiError, api } from "../api/client";
import type { AuditLogPage, RbacProbeResult, RbacTestReport } from "../api/client";
import { Button, Card, CardHeader, EmptyState, Pill, ScoreRing } from "./ui";
import type { Tone } from "./ui";

const PAGE_SIZE = 25;
const STATUS_FILTERS = ["", "ok", "error", "blocked", "rbac_violation", "retry_exhausted", "passed", "failed"];
const CATEGORY_LABEL: Record<RbacProbeResult["category"], string> = {
  read: "Read access",
  write: "Write access",
  ddl: "Schema changes",
  privilege: "Privilege escalation",
};

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Unexpected error";
}

function statusTone(status: string): Tone {
  if (status === "ok" || status === "passed") return "ok";
  if (status === "blocked" || status === "rbac_violation") return "warn";
  return "danger";
}

function PasswordGate({ onUnlock }: { onUnlock: (password: string) => Promise<void> }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!password) return;
    setBusy(true);
    setError(null);
    try {
      await onUnlock(password);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex justify-center py-8">
      <Card className="w-full max-w-md p-6">
        <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-accent to-pro text-white shadow-pop">
          <Lock className="h-5 w-5" aria-hidden />
        </div>
        <h2 className="mt-4 text-center text-base font-semibold text-fg">Security Audit</h2>
        <p className="mt-1 text-center text-xs text-fg-subtle">
          The immutable audit trail and the live RBAC penetration test are restricted to administrators.
        </p>
        <form onSubmit={submit} className="mt-5 space-y-3">
          <label htmlFor="admin-password" className="text-xs font-medium text-fg-muted">
            Admin password
          </label>
          <input
            id="admin-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="h-10 w-full rounded-lg border border-line bg-surface-2 px-3 text-sm text-fg focus:border-accent focus:outline-none"
          />
          {error ? <p className="text-xs text-danger">{error}</p> : null}
          <button
            type="submit"
            disabled={busy || !password}
            className="flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-accent text-sm font-medium text-accent-fg hover:bg-accent-strong disabled:opacity-40"
          >
            {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : <LockOpen className="h-4 w-4" aria-hidden />}
            Unlock
          </button>
        </form>
      </Card>
    </div>
  );
}

function PenTestPanel({ password }: { password: string }) {
  const [report, setReport] = useState<RbacTestReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      setReport(await api.rbacTest(password));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setRunning(false);
    }
  }

  const categories = report
    ? (Object.keys(CATEGORY_LABEL) as RbacProbeResult["category"][]).filter((c) =>
        report.results.some((r) => r.category === c),
      )
    : [];

  return (
    <Card>
      <CardHeader
        icon={ShieldCheck}
        title="Live RBAC penetration test"
        subtitle={
          <>
            Fixed attack probes sent straight to PostgreSQL as <code className="font-mono">usr_fde_ro</code>, bypassing
            the app's SQL guard. Every probe is rolled back.
          </>
        }
        actions={
          <Button variant="primary" size="sm" icon={running ? LoaderCircle : Play} onClick={() => void run()} disabled={running}>
            {running ? "Running…" : "Run pen test"}
          </Button>
        }
      />
      {error ? <p className="px-5 py-3 text-xs text-danger">{error}</p> : null}
      {report ? (
        <div className="space-y-4 p-4 sm:p-5">
          <div className="flex flex-wrap items-center gap-4 rounded-xl border border-line bg-surface-2/60 p-4">
            <ScoreRing
              value={report.passed_count}
              max={report.total}
              tone={report.passed ? "ok" : "danger"}
              size={72}
              label={`${report.passed_count}/${report.total}`}
            />
            <div>
              <div className={`text-sm font-semibold ${report.passed ? "text-ok" : "text-danger"}`}>
                {report.passed ? "Least privilege holds" : "Unexpected access detected"}
              </div>
              <div className="mt-0.5 text-xs text-fg-subtle">
                Role <code className="font-mono">{report.role}</code> · {new Date(report.executed_at_utc).toLocaleString()}
              </div>
            </div>
          </div>
          {categories.map((category) => (
            <section key={category}>
              <div className="label-caps mb-2">{CATEGORY_LABEL[category]}</div>
              <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
                {report.results
                  .filter((r) => r.category === category)
                  .map((probe) => (
                    <li key={probe.probe_id} className="grid gap-2 bg-surface p-3 md:grid-cols-[1fr_auto]">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          {probe.passed ? (
                            <CircleCheck className="h-4 w-4 shrink-0 text-ok" aria-label="passed" />
                          ) : (
                            <CircleX className="h-4 w-4 shrink-0 text-danger" aria-label="failed" />
                          )}
                          <span className="text-[13px] font-medium text-fg">{probe.title}</span>
                        </div>
                        <code className="mt-1 block truncate pl-6 font-mono text-[11px] text-fg-subtle" title={probe.sql}>
                          {probe.sql}
                        </code>
                        <div className="mt-1 pl-6 text-[11.5px] text-fg-muted">
                          {probe.sqlstate ? <span className="font-mono text-fg-subtle">[{probe.sqlstate}] </span> : null}
                          {probe.detail}
                        </div>
                      </div>
                      <div className="flex items-start gap-1.5 pl-6 md:pl-0">
                        <Pill tone="neutral">expect {probe.expected}</Pill>
                        <Pill tone={probe.actual === "blocked" ? "ok" : "accent"}>{probe.actual}</Pill>
                      </div>
                    </li>
                  ))}
              </ul>
            </section>
          ))}
        </div>
      ) : !error ? (
        <EmptyState icon={ShieldCheck} title="Prove least privilege live">
          Run the test to show the database itself enforces read-only access to the view and append-only access to the
          audit log.
        </EmptyState>
      ) : null}
    </Card>
  );
}

function AuditLogTable({ password }: { password: string }) {
  const [page, setPage] = useState<AuditLogPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPage(await api.auditLogs(password, { limit: PAGE_SIZE, offset, status: statusFilter || null }));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, [password, offset, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const total = page?.total ?? 0;
  return (
    <Card>
      <CardHeader
        icon={FileClock}
        title="Immutable audit trail"
        subtitle={
          <>
            <code className="font-mono">fde_views.agent_audit_log</code> · append-only, every model and tool call
          </>
        }
        actions={
          <>
            <select
              aria-label="Filter by status"
              value={statusFilter}
              onChange={(event) => {
                setOffset(0);
                setStatusFilter(event.target.value);
              }}
              className="h-8 rounded-lg border border-line bg-surface-2 px-2 text-xs text-fg"
            >
              {STATUS_FILTERS.map((value) => (
                <option key={value || "all"} value={value}>
                  {value ? `status: ${value}` : "All statuses"}
                </option>
              ))}
            </select>
            <Button size="sm" icon={RefreshCw} onClick={() => void load()} disabled={loading} aria-label="Refresh audit log">
              Refresh
            </Button>
          </>
        }
      />
      {error ? <p className="px-5 py-3 text-xs text-danger">{error}</p> : null}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-surface-2 text-fg-muted">
            <tr>
              {["Time", "Session", "Node", "Model", "Tool", "Status", "Latency", "SQL / error"].map((h) => (
                <th key={h} className={`px-3 py-2 font-semibold whitespace-nowrap ${h === "Latency" ? "text-right" : ""}`}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {page?.entries.map((entry) => (
              <tr key={entry.log_id} className="border-t border-line align-top hover:bg-surface-2/60">
                <td className="px-3 py-2 whitespace-nowrap text-fg-muted tabular-nums">
                  {new Date(entry.timestamp_utc).toLocaleString()}
                </td>
                <td className="px-3 py-2 font-mono text-fg-subtle" title={entry.session_id}>
                  {entry.session_id.slice(0, 8)}
                </td>
                <td className="px-3 py-2 text-fg">{entry.node_name}</td>
                <td className="px-3 py-2 text-fg-muted">{entry.model_used ?? "—"}</td>
                <td className="px-3 py-2 text-fg-muted">{entry.tool_called ?? "—"}</td>
                <td className="px-3 py-2">
                  <Pill tone={statusTone(entry.status)}>{entry.status}</Pill>
                </td>
                <td className="px-3 py-2 text-right text-fg-muted tabular-nums">
                  {entry.latency_ms !== null ? `${entry.latency_ms} ms` : "—"}
                </td>
                <td className="max-w-md px-3 py-2">
                  {entry.executed_sql ? (
                    <code className="line-clamp-2 font-mono text-[11px] break-all text-fg-muted" title={entry.executed_sql}>
                      {entry.executed_sql}
                    </code>
                  ) : null}
                  {entry.error_message ? (
                    <span className="line-clamp-2 text-danger" title={entry.error_message}>
                      {entry.error_message}
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {page && page.entries.length === 0 ? (
          <EmptyState icon={FileClock} title="No audit events yet">
            Run a copilot turn or the pen test.
          </EmptyState>
        ) : null}
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-line px-4 py-2.5 text-xs text-fg-subtle">
        <span className="tabular-nums">
          {total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
        </span>
        <div className="flex gap-1">
          <Button size="sm" icon={ChevronLeft} aria-label="Previous page" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} />
          <Button size="sm" icon={ChevronRight} aria-label="Next page" disabled={offset + PAGE_SIZE >= total || loading} onClick={() => setOffset(offset + PAGE_SIZE)} />
        </div>
      </div>
    </Card>
  );
}

export default function AuditDashboard() {
  const [password, setPassword] = useState<string | null>(null);

  async function unlock(candidate: string) {
    await api.auditLogs(candidate, { limit: 1 });
    setPassword(candidate);
  }

  if (password === null) return <PasswordGate onUnlock={unlock} />;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <Pill tone="ok" icon={LockOpen}>
          Admin session
        </Pill>
        <Button size="sm" icon={Lock} onClick={() => setPassword(null)}>
          Lock
        </Button>
      </div>
      <PenTestPanel password={password} />
      <AuditLogTable password={password} />
    </div>
  );
}
