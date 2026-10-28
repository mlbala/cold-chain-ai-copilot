import {
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  CircleX,
  LoaderCircle,
  Lock,
  LogOut,
  Play,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { ApiError, api } from "../api/client";
import type { AuditLogPage, RbacTestReport } from "../api/client";

const PAGE_SIZE = 25;
const STATUS_FILTERS = ["", "ok", "error", "blocked", "rbac_violation", "retry_exhausted", "passed", "failed"];

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Unexpected error";
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
    <div className="panel mx-auto max-w-md p-6">
      <div className="mb-4 flex items-center gap-2">
        <Lock className="h-5 w-5 text-signal" aria-hidden />
        <h2 className="text-sm font-semibold">Security Audit · admin access</h2>
      </div>
      <p className="mb-4 text-xs text-ink-400">
        The audit trail and the live RBAC penetration test are restricted to administrators.
      </p>
      <form onSubmit={submit} className="space-y-3">
        <label htmlFor="admin-password" className="block text-xs text-ink-300">
          Admin password
        </label>
        <input
          id="admin-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full rounded-lg border border-ink-700 bg-ink-950 px-3 py-2 text-sm focus:border-signal focus:outline-none"
        />
        {error ? <p className="text-xs text-danger">{error}</p> : null}
        <button
          type="submit"
          disabled={busy || !password}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-signal-strong px-4 py-2 text-sm font-medium text-white hover:bg-signal disabled:opacity-40"
        >
          {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden /> : null}
          Unlock
        </button>
      </form>
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

  return (
    <section className="panel" aria-label="Live RBAC penetration test">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-4 py-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <ShieldCheck className="h-4 w-4 text-signal" aria-hidden />
            Live RBAC Penetration Test
          </h3>
          <p className="text-xs text-ink-400">
            Fixed attack probes sent straight to PostgreSQL as <code>usr_fde_ro</code>, bypassing the
            app's SQL guard. Every probe is rolled back.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void run()}
          disabled={running}
          className="inline-flex items-center gap-1.5 rounded-lg bg-signal-strong px-3 py-1.5 text-xs font-medium text-white hover:bg-signal disabled:opacity-40"
        >
          {running ? (
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <Play className="h-3.5 w-3.5" aria-hidden />
          )}
          Run pen test
        </button>
      </header>

      {error ? <p className="px-4 py-3 text-xs text-danger">{error}</p> : null}

      {report ? (
        <div className="space-y-3 p-4">
          <div
            className={`rounded-lg border px-3 py-2 text-sm ${
              report.passed
                ? "border-ok/40 bg-ok/10 text-ok"
                : "border-danger/40 bg-danger/10 text-danger"
            }`}
          >
            {report.passed_count}/{report.total} probes behaved as expected for role{" "}
            <code>{report.role}</code> ·{" "}
            {new Date(report.executed_at_utc).toLocaleString()}
          </div>
          <div className="overflow-x-auto rounded-lg border border-ink-700">
            <table className="w-full text-left text-xs">
              <thead className="bg-ink-800 text-ink-300">
                <tr>
                  <th className="px-3 py-2">Result</th>
                  <th className="px-3 py-2">Probe</th>
                  <th className="px-3 py-2">Expected</th>
                  <th className="px-3 py-2">Actual</th>
                  <th className="px-3 py-2">Engine response</th>
                </tr>
              </thead>
              <tbody>
                {report.results.map((probe) => (
                  <tr key={probe.probe_id} className="border-t border-ink-700 align-top">
                    <td className="px-3 py-2">
                      {probe.passed ? (
                        <CircleCheck className="h-4 w-4 text-ok" aria-label="passed" />
                      ) : (
                        <CircleX className="h-4 w-4 text-danger" aria-label="failed" />
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="font-medium text-ink-100">{probe.title}</div>
                      <code className="mt-1 block max-w-md whitespace-pre-wrap break-words font-mono text-[11px] text-ink-400">
                        {probe.sql}
                      </code>
                    </td>
                    <td className="px-3 py-2 uppercase text-ink-300">{probe.expected}</td>
                    <td
                      className={`px-3 py-2 uppercase ${
                        probe.actual === "blocked" ? "text-ok" : "text-signal"
                      }`}
                    >
                      {probe.actual}
                    </td>
                    <td className="px-3 py-2 text-ink-300">
                      {probe.sqlstate ? (
                        <span className="mr-1 font-mono text-ink-400">[{probe.sqlstate}]</span>
                      ) : null}
                      {probe.detail}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : !error ? (
        <p className="px-4 py-4 text-xs text-ink-400">
          Run the test to prove the database enforces read-only access to the view and append-only
          access to the audit log.
        </p>
      ) : null}
    </section>
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
      setPage(
        await api.auditLogs(password, {
          limit: PAGE_SIZE,
          offset,
          status: statusFilter || null,
        }),
      );
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
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + PAGE_SIZE, total);

  return (
    <section className="panel" aria-label="Agent audit log">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold">Immutable Audit Trail</h3>
          <p className="text-xs text-ink-400">
            <code>fde_views.agent_audit_log</code> · append-only, every model call and tool call
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="status-filter" className="sr-only">
            Filter by status
          </label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(event) => {
              setOffset(0);
              setStatusFilter(event.target.value);
            }}
            className="rounded-lg border border-ink-700 bg-ink-950 px-2 py-1.5 text-xs text-ink-100"
          >
            {STATUS_FILTERS.map((value) => (
              <option key={value || "all"} value={value}>
                {value ? `status: ${value}` : "all statuses"}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            aria-label="Refresh audit log"
            className="rounded-lg border border-ink-700 p-1.5 text-ink-300 hover:text-ink-100 disabled:opacity-40"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden />
          </button>
        </div>
      </header>

      {error ? <p className="px-4 py-3 text-xs text-danger">{error}</p> : null}

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-ink-800 text-ink-300">
            <tr>
              <th className="px-3 py-2">Time (local)</th>
              <th className="px-3 py-2">Session</th>
              <th className="px-3 py-2">Node</th>
              <th className="px-3 py-2">Model</th>
              <th className="px-3 py-2">Tool</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2 text-right">Latency</th>
              <th className="px-3 py-2">SQL / error</th>
            </tr>
          </thead>
          <tbody>
            {page?.entries.map((entry) => (
              <tr key={entry.log_id} className="border-t border-ink-700 align-top">
                <td className="whitespace-nowrap px-3 py-2 text-ink-300">
                  {new Date(entry.timestamp_utc).toLocaleString()}
                </td>
                <td className="px-3 py-2 font-mono text-ink-400" title={entry.session_id}>
                  {entry.session_id.slice(0, 8)}
                </td>
                <td className="px-3 py-2">{entry.node_name}</td>
                <td className="px-3 py-2 text-ink-300">{entry.model_used ?? "—"}</td>
                <td className="px-3 py-2 text-ink-300">{entry.tool_called ?? "—"}</td>
                <td
                  className={`px-3 py-2 font-medium ${
                    ["ok", "passed"].includes(entry.status) ? "text-ok" : "text-danger"
                  }`}
                >
                  {entry.status}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-ink-300">
                  {entry.latency_ms !== null ? `${entry.latency_ms} ms` : "—"}
                </td>
                <td className="max-w-md px-3 py-2">
                  {entry.executed_sql ? (
                    <code
                      className="line-clamp-2 break-all font-mono text-[11px] text-ink-300"
                      title={entry.executed_sql}
                    >
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
            {page && page.entries.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-ink-400">
                  No audit events yet. Run a copilot turn or the pen test.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <footer className="flex items-center justify-between gap-2 border-t border-ink-700 px-4 py-2 text-xs text-ink-400">
        <span>
          {first}–{last} of {total}
        </span>
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0 || loading}
            aria-label="Previous page"
            className="rounded-lg border border-ink-700 p-1 disabled:opacity-40"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </button>
          <button
            type="button"
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= total || loading}
            aria-label="Next page"
            className="rounded-lg border border-ink-700 p-1 disabled:opacity-40"
          >
            <ChevronRight className="h-4 w-4" aria-hidden />
          </button>
        </div>
      </footer>
    </section>
  );
}

export default function AuditDashboard() {
  const [password, setPassword] = useState<string | null>(null);

  async function unlock(candidate: string) {
    await api.auditLogs(candidate, { limit: 1 });
    setPassword(candidate);
  }

  if (password === null) {
    return <PasswordGate onUnlock={unlock} />;
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setPassword(null)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-ink-700 px-2.5 py-1.5 text-xs text-ink-300 hover:text-ink-100"
        >
          <LogOut className="h-3.5 w-3.5" aria-hidden />
          Lock
        </button>
      </div>
      <PenTestPanel password={password} />
      <AuditLogTable password={password} />
    </div>
  );
}
