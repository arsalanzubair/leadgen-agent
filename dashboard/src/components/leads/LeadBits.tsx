/**
 * LeadBits.tsx -- the small lead-specific display pieces, shared by every
 * screen that shows a business.
 *
 * Keeping them here means a match score looks identical in the table, on the
 * detail page, in the approvals queue and in the LinkedIn list -- and that
 * changing how a score reads is one edit rather than five.
 */

import { Building2, Linkedin, Mail } from "lucide-react";
import { Link } from "react-router-dom";

import { Badge, Num } from "@/components/ui/primitives";
import { matchQuality } from "@/lib/matchQuality";
import { leadOutcome } from "@/lib/outcome";
import {
  channelLabel,
  matchQualityLabel,
  matchQualityTone,
  outcomeLabel,
  outcomeTone,
  regionLabel,
  regionShort,
  scoreLabel,
} from "@/lib/statusLabels";
import { cn } from "@/lib/utils";
import type { Channel, Lead, Region } from "@/types/lead";

/**
 * The match score, as a compact bar plus the number.
 *
 * A bar rather than a big coloured ring, deliberately: a column of these is
 * scannable, and the marker for the user's own bar is what they actually read
 * -- is this business above or below the line I set.
 */
export function MatchScore({
  score,
  threshold,
  width = 44,
  showNumber = true,
}: {
  score: number;
  threshold?: number;
  width?: number;
  showNumber?: boolean;
}) {
  const above = threshold === undefined || score >= threshold;
  return (
    <span
      className="inline-flex items-center gap-2"
      title={threshold !== undefined ? scoreLabel(score, threshold) : undefined}
    >
      {showNumber ? (
        <Num
          className={cn(
            "w-[26px] text-right text-meta",
            above ? "text-primary" : "text-secondary",
          )}
        >
          {score}
        </Num>
      ) : null}
      <span
        className="relative h-1.5 overflow-hidden rounded-full bg-surface-sunken"
        style={{ width }}
        aria-hidden="true"
      >
        <span
          className={cn(
            "absolute inset-y-0 left-0 rounded-full",
            above ? "bg-accent" : "bg-border-strong",
          )}
          style={{ width: `${Math.max(2, score)}%` }}
        />
        {threshold !== undefined ? (
          // The user's own bar, as a hairline. Without it the number is just a
          // number.
          <span
            className="absolute inset-y-0 w-px bg-tertiary"
            style={{ left: `${threshold}%` }}
          />
        ) : null}
      </span>
    </span>
  );
}

/** Which of the six buckets this business is in. */
export function MatchBadge({ lead, threshold }: { lead: Lead; threshold: number }) {
  const quality = matchQuality(lead, threshold);
  if (!quality) {
    // No bucket fits -- show what actually happened instead of forcing one.
    return <StatusBadge lead={lead} />;
  }
  return (
    <Badge tone={matchQualityTone(quality)} dot>
      {matchQualityLabel(quality)}
    </Badge>
  );
}

/** What is happening to this business right now. */
export function StatusBadge({ lead }: { lead: Lead }) {
  const outcome = leadOutcome(lead);
  return (
    <Badge tone={outcomeTone(outcome)} dot>
      {outcomeLabel(outcome)}
    </Badge>
  );
}

/** How they are being reached, as icons so "both" is legible at a glance. */
export function ChannelBadge({
  channel,
  showLabel = false,
}: {
  channel: Channel;
  showLabel?: boolean;
}) {
  const email = channel === "email" || channel === "both";
  const linkedin = channel === "linkedin" || channel === "both";
  return (
    <span
      className="inline-flex items-center gap-1.5 text-meta text-secondary"
      title={channelLabel(channel)}
    >
      {email ? <Mail size={14} /> : null}
      {linkedin ? <Linkedin size={14} /> : null}
      {showLabel ? <span>{channelLabel(channel)}</span> : null}
    </span>
  );
}

/** Where they are, short enough for a table cell. */
export function RegionPill({ region }: { region: Region }) {
  return (
    <span
      className="inline-flex h-5 items-center rounded border border-border bg-surface-raised px-1.5 text-micro text-secondary"
      title={regionLabel(region)}
    >
      {regionShort(region)}
    </span>
  );
}

/** Shown only when the message was actually written in another language. */
/** Company cell: name, what they do, and a link through to the detail page. */
export function CompanyCell({
  lead,
  to,
}: {
  lead: Pick<Lead, "company_name" | "location" | "industry" | "lead_id">;
  to?: string;
}) {
  const body = (
    <span className="flex min-w-0 items-center gap-2.5">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-control border border-border bg-surface-raised">
        <Building2 size={13} className="text-secondary" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-meta font-medium text-primary">
          {lead.company_name}
        </span>
        <span className="block truncate text-micro text-tertiary">
          {lead.industry || lead.location}
        </span>
      </span>
    </span>
  );

  if (!to) return body;
  return (
    <Link to={to} className="block min-w-0 transition-opacity duration-150 hover:opacity-90">
      {body}
    </Link>
  );
}

/** Contact cell: name over email, or an explicit "nobody found". */
export function ContactCell({
  name,
  email,
  linkedinUrl,
}: {
  name: string;
  email: string;
  linkedinUrl?: string;
}) {
  if (!name && !email && !linkedinUrl) {
    return <span className="text-meta text-tertiary">Nobody found yet</span>;
  }
  return (
    <span className="block min-w-0">
      <span className="block truncate text-meta text-primary">
        {name || "Name unknown"}
      </span>
      <span className="block truncate text-micro text-tertiary">
        {email || (linkedinUrl ? "LinkedIn only" : "")}
      </span>
    </span>
  );
}
