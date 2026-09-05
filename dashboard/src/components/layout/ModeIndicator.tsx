/**
 * ModeIndicator.tsx -- Test Mode vs Live Mode, made impossible to confuse.
 *
 * The thing behind this UI calls it `dry_run`. The product never does, and it
 * never renders a message that was not sent as though it was. That is not a
 * style preference: somebody looking at this screen has to be able to tell, at
 * a glance and without reading carefully, whether a real stranger received a
 * real email.
 *
 * Three pieces, so the distinction is made once rather than remembered twelve
 * times:
 *
 *   ModeBanner     a strip above any message body
 *   ModeTag        an inline marker for table rows
 *   ModeChoice     the Test/Live selector, with Live behind a confirmation
 *
 * The Test Mode treatments carry diagonal hatching as well as colour, because
 * colour alone is not enough of a difference for something this consequential
 * -- and it survives a colourblind viewer, a bad monitor and a screenshot.
 */

import { Eye, Send, ShieldCheck } from "lucide-react";
import * as React from "react";

import { modeDescription, modeLabel, sendVerb } from "@/lib/statusLabels";
import { cn } from "@/lib/utils";
import type { Channel } from "@/types/lead";

export { sendVerb };

/** The hatch pattern used by every Test Mode surface. */
const HATCH = {
  backgroundImage:
    "repeating-linear-gradient(-45deg, rgba(217,164,65,.10) 0 8px, rgba(217,164,65,.03) 8px 16px)",
};

// --------------------------------------------------------------------------- //
// The top-bar pill
// --------------------------------------------------------------------------- //

// --------------------------------------------------------------------------- //
// The banner above a message body
// --------------------------------------------------------------------------- //

export function ModeBanner({
  testMode,
  channel,
  className,
}: {
  testMode: boolean;
  channel: Channel;
  className?: string;
}) {
  if (testMode) {
    return (
      <div
        className={cn(
          "flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-warning/30 px-4 py-2",
          "text-micro text-warning",
          className,
        )}
        style={HATCH}
      >
        <Eye size={13} className="shrink-0" />
        <span className="font-medium">Test Mode</span>
        <span className="text-warning/80">
          {channel === "linkedin"
            ? "this message would be queued for you to send - nothing is in your list"
            : "this email would be sent - nothing has left your mailbox"}
        </span>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-border bg-surface-raised px-4 py-2",
        "text-micro text-secondary",
        className,
      )}
    >
      <Send size={13} className="shrink-0 text-success" />
      <span className="font-medium text-primary">Live Mode</span>
      <span>
        {channel === "linkedin"
          ? "approving this puts it in your list to send by hand"
          : "approving this sends it, within the sending hours for their country"}
      </span>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// The inline marker
// --------------------------------------------------------------------------- //

export function ModeTag({ testMode }: { testMode: boolean }) {
  if (!testMode) return null;
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-warning/30 px-1.5 py-px text-[10px] font-medium text-warning"
      style={HATCH}
    >
      Test Mode
    </span>
  );
}

// --------------------------------------------------------------------------- //
// The chooser
// --------------------------------------------------------------------------- //

/**
 * Choosing Test Mode is one click. Choosing Live Mode is two, and the second
 * one spells out what it means. The asymmetry is the safety.
 */
export function ModeChoice({
  testMode,
  onChange,
  className,
}: {
  testMode: boolean;
  onChange: (testMode: boolean) => void;
  className?: string;
}) {
  const [confirming, setConfirming] = React.useState(false);

  return (
    <div className={className}>
      <div className="grid gap-2 sm:grid-cols-2">
        <button
          type="button"
          onClick={() => {
            setConfirming(false);
            onChange(true);
          }}
          className={cn(
            "rounded-card border p-3 text-left transition-colors duration-150",
            testMode
              ? "border-warning/40 text-primary"
              : "border-border bg-surface hover:border-border-strong",
          )}
          style={testMode ? HATCH : undefined}
        >
          <span className="flex items-center gap-2 text-meta font-medium text-primary">
            <Eye size={14} className={testMode ? "text-warning" : "text-tertiary"} />
            {modeLabel(true)}
          </span>
          <span className="mt-1 block text-micro text-secondary">
            {modeDescription(true)}
          </span>
        </button>

        <button
          type="button"
          onClick={() => (testMode ? setConfirming(true) : onChange(false))}
          className={cn(
            "rounded-card border p-3 text-left transition-colors duration-150",
            !testMode
              ? "border-success/40 bg-success-muted"
              : "border-border bg-surface hover:border-border-strong",
          )}
        >
          <span className="flex items-center gap-2 text-meta font-medium text-primary">
            <Send size={14} className={!testMode ? "text-success" : "text-tertiary"} />
            {modeLabel(false)}
          </span>
          <span className="mt-1 block text-micro text-secondary">
            {modeDescription(false)}
          </span>
        </button>
      </div>

      {confirming ? (
        <div className="mt-2 animate-fade-in rounded-card border border-success/30 bg-success-muted/40 p-3">
          <p className="flex items-start gap-2 text-meta text-primary">
            <ShieldCheck size={15} className="mt-0.5 shrink-0 text-success" />
            <span>
              In Live Mode, every message you approve is really sent to a real
              person. Nothing sends without your approval first.
            </span>
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
                onChange(false);
              }}
              className="rounded-control bg-success px-3 py-1.5 text-meta font-medium text-accent-contrast transition-colors duration-150 hover:bg-success/90"
            >
              Yes, switch to Live Mode
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-control border border-border px-3 py-1.5 text-meta text-secondary transition-colors duration-150 hover:text-primary"
            >
              Stay in Test Mode
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
