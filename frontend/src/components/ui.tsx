import type { LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

import type { ComponentState, RiskLevel } from "../api/client";

export type Tone = "neutral" | "accent" | "ok" | "warn" | "danger" | "pro";

export const toneText: Record<Tone, string> = {
  neutral: "text-fg-muted",
  accent: "text-accent",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
  pro: "text-pro",
};

const toneChip: Record<Tone, string> = {
  neutral: "border-line bg-surface-2 text-fg-muted",
  accent: "border-accent/25 bg-accent/10 text-accent",
  ok: "border-ok/25 bg-ok/10 text-ok",
  warn: "border-warn/25 bg-warn/10 text-warn",
  danger: "border-danger/25 bg-danger/10 text-danger",
  pro: "border-pro/25 bg-pro/10 text-pro",
};

const toneFill: Record<Tone, string> = {
  neutral: "bg-fg-subtle",
  accent: "bg-accent",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
  pro: "bg-pro",
};

export const levelTone: Record<RiskLevel, Tone> = {
  critical: "danger",
  high: "warn",
  elevated: "accent",
  normal: "neutral",
};

export const stateTone: Record<ComponentState, Tone> = {
  ok: "ok",
  degraded: "warn",
  down: "danger",
  unknown: "neutral",
};

export function Card({ className = "", children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={`card ${className}`} {...rest}>
      {children}
    </div>
  );
}

export function CardHeader(props: {
  icon?: LucideIcon;
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  const Icon = props.icon;
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3.5 sm:px-5">
      <div className="flex min-w-0 items-start gap-3">
        {Icon ? (
          <div className="mt-0.5 rounded-lg border border-line bg-surface-2 p-1.5 text-accent">
            <Icon className="h-4 w-4" aria-hidden />
          </div>
        ) : null}
        <div className="min-w-0">
          <h2 className="text-[14px] font-semibold text-fg">{props.title}</h2>
          {props.subtitle ? <p className="mt-0.5 text-xs text-fg-subtle">{props.subtitle}</p> : null}
        </div>
      </div>
      {props.actions ? <div className="flex items-center gap-2">{props.actions}</div> : null}
    </div>
  );
}

export function Pill({
  tone = "neutral",
  icon: Icon,
  className = "",
  children,
  title,
}: {
  tone?: Tone;
  icon?: LucideIcon;
  className?: string;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap ${toneChip[tone]} ${className}`}
    >
      {Icon ? <Icon className="h-3 w-3" aria-hidden /> : null}
      {children}
    </span>
  );
}

export function LevelBadge({ level }: { level: RiskLevel }) {
  return (
    <Pill tone={levelTone[level]} className="capitalize">
      {level}
    </Pill>
  );
}

export function StatusDot({ state, pulse = false }: { state: ComponentState; pulse?: boolean }) {
  const fill = toneFill[stateTone[state]];
  return (
    <span className="relative inline-flex h-2.5 w-2.5" aria-hidden>
      {pulse ? (
        <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-50 ${fill}`} />
      ) : null}
      <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${fill}`} />
    </span>
  );
}

export function Meter({
  value,
  max = 100,
  tone = "accent",
  className = "",
  label,
}: {
  value: number;
  max?: number;
  tone?: Tone;
  className?: string;
  label?: string;
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div
      className={`h-1.5 w-full overflow-hidden rounded-full bg-surface-3 ${className}`}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value}
      aria-label={label}
    >
      <div className={`h-full rounded-full transition-[width] duration-500 ${toneFill[tone]}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-surface-3 ${className}`} aria-hidden />;
}

export function ScoreRing({
  value,
  max,
  tone,
  size = 64,
  label,
}: {
  value: number;
  max: number;
  tone: Tone;
  size?: number;
  label: ReactNode;
}) {
  const radius = size / 2 - 6;
  const circumference = 2 * Math.PI * radius;
  const pct = max > 0 ? value / max : 0;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" strokeWidth="6" className="stroke-surface-3" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - pct)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          className={`transition-[stroke-dashoffset] duration-700 ${toneText[tone]}`}
          stroke="currentColor"
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center text-center text-xs font-semibold text-fg">
        {label}
      </div>
    </div>
  );
}

export function EmptyState({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <div className="rounded-xl border border-line bg-surface-2 p-2.5 text-fg-subtle">
        <Icon className="h-5 w-5" aria-hidden />
      </div>
      <p className="text-sm font-medium text-fg">{title}</p>
      {children ? <p className="max-w-sm text-xs text-fg-subtle">{children}</p> : null}
    </div>
  );
}

export function Button({
  variant = "secondary",
  size = "md",
  icon: Icon,
  className = "",
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost";
  size?: "sm" | "md";
  icon?: LucideIcon;
}) {
  const variants = {
    primary: "bg-accent text-accent-fg hover:bg-accent-strong shadow-card",
    secondary: "border border-line bg-surface text-fg-muted hover:border-line-strong hover:text-fg",
    ghost: "text-fg-muted hover:bg-surface-2 hover:text-fg",
  };
  const sizes = { sm: "h-8 px-2.5 text-xs gap-1.5", md: "h-9 px-3.5 text-sm gap-2" };
  return (
    <button
      type="button"
      className={`inline-flex items-center justify-center rounded-lg font-medium transition-colors disabled:pointer-events-none disabled:opacity-40 ${variants[variant]} ${sizes[size]} ${className}`}
      {...rest}
    >
      {Icon ? <Icon className={size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4"} aria-hidden /> : null}
      {children}
    </button>
  );
}

export function timeAgo(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "never";
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
}

export function usd(value: number): string {
  if (value === 0) return "$0";
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}
