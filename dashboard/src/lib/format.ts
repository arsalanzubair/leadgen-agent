/**
 * format.ts -- shapes for numbers, dates and text.
 *
 * Formatting only. Turning a backend value into English happens in exactly one
 * other place, `lib/statusLabels.ts`, and never here: keeping the two apart is
 * what stops a second copy of a status label appearing by accident.
 */

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-GB").format(value);
}

export function formatPercent(value: number, digits = 0): string {
  return `${value.toFixed(digits)}%`;
}

export function formatDelta(value: number): string {
  if (value === 0) return "no change";
  return `${value > 0 ? "+" : ""}${value}%`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "-";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatTime(iso: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

/** "3 days ago" / "in 2 days". */
export function relativeTime(iso: string | null, now = new Date()): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  const diff = date.getTime() - now.getTime();
  const abs = Math.abs(diff);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["second", 1000],
    ["minute", 60_000],
    ["hour", 3_600_000],
    ["day", 86_400_000],
    ["week", 604_800_000],
    ["month", 2_629_800_000],
  ];
  let unit: Intl.RelativeTimeFormatUnit = "month";
  let divisor = 2_629_800_000;
  for (let i = units.length - 1; i >= 0; i--) {
    if (abs >= units[i][1] || i === 0) {
      [unit, divisor] = units[i];
      break;
    }
  }
  const formatter = new Intl.RelativeTimeFormat("en-GB", { numeric: "auto" });
  return formatter.format(Math.round(diff / divisor), unit);
}

/** Today / Tomorrow / later -- the Follow-ups grouping. */
export function dueBucket(
  iso: string | null,
  now = new Date(),
): "today" | "tomorrow" | "later" | "overdue" {
  if (!iso) return "later";
  const date = new Date(iso);
  const startOfDay = (d: Date) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(date) - startOfDay(now)) / 86_400_000);
  if (days < 0) return "overdue";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  return "later";
}

/** First initial + surname initial, for avatars. */
export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Bare domain, for compact display of a website URL. */
export function domainOf(url: string): string {
  if (!url) return "";
  return url
    .replace(/^https?:\/\//, "")
    .replace(/^www\./, "")
    .split("/")[0];
}

/** linkedin.com/company/x -> company/x, for compact display. */
export function linkedinHandle(url: string): string {
  if (!url) return "";
  const match = /linkedin\.com\/(.+)$/.exec(url.replace(/\/$/, ""));
  return match ? match[1] : domainOf(url);
}

export function truncate(text: string, max: number): string {
  if (!text || text.length <= max) return text;
  return `${text.slice(0, max - 1).trimEnd()}…`;
}

/** "3 businesses" / "1 business" -- pluralisation without a library. */
export function plural(count: number, singular: string, pluralForm?: string): string {
  const word = count === 1 ? singular : (pluralForm ?? `${singular}s`);
  return `${formatNumber(count)} ${word}`;
}

/** Sentence-joins a short list: "a, b and c". */
export function listSentence(items: string[]): string {
  const clean = items.filter(Boolean);
  if (!clean.length) return "";
  if (clean.length === 1) return clean[0];
  return `${clean.slice(0, -1).join(", ")} and ${clean[clean.length - 1]}`;
}

/** Ordinals for follow-up numbering: "first", "second", ... */
const ORDINALS = [
  "first",
  "second",
  "third",
  "fourth",
  "fifth",
  "sixth",
  "seventh",
  "eighth",
];

export function ordinalWord(n: number): string {
  return ORDINALS[n - 1] ?? `${n}th`;
}

/** Last four characters of a secret, for "which key did I save" recognition. */
export function maskedKey(last4: string): string {
  if (!last4) return "";
  return `••••••••${last4}`;
}
