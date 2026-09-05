/**
 * statusLabels.ts -- the ONLY place a backend value becomes English.
 *
 * The data model keeps the backend's own vocabulary: `blocked_optout` stays
 * `blocked_optout`, `pending_manual_send` stays `pending_manual_send`. That
 * fidelity is what makes the mock service layer swappable for a real API. But
 * no user ever sees those strings, and no user ever sees the name of an
 * internal processing step.
 *
 * Rules for this file:
 *
 *   1. Every enum the UI renders has a mapping here. If you find yourself
 *      writing `status === "pending" ? "Waiting" : ...` in a component, the
 *      mapping belongs here instead -- two copies of a label drift, and the
 *      one that drifts is always the one a customer is looking at.
 *   2. Nothing here mentions the pipeline's internals. Not the framework, not
 *      "state", not step numbers, not step ids. The product describes what is
 *      happening to the user's leads, in the words they would use.
 *   3. Labels are sentence case, not Title Case, and never end in a period.
 */

import type {
  ApprovalStatus,
  ArchiveReason,
  Channel,
  LeadSource,
  Region,
  ReplyCategory,
  SendStatus,
  SuppressionStatus,
} from "@/types/lead";
import type { LinkedInQueueStatus, RunStage, RunStatus } from "@/types/agent";

/**
 * The terminology table, verbatim.
 *
 * Every one of these is a concept that exists in the backend under a different
 * name. The left-hand names are not "discouraged" -- they do not appear in the
 * product at all, including in tooltips, aria-labels and console output. Import
 * from here rather than retyping the phrase, so there is one string to change
 * if the wording ever moves.
 */
export const TERMS = {
  /** Running a search. Never "run the agent", never "execute". */
  findLeads: "Find Leads",
  /** The suppression list and the compliance gate, together, in one noun. */
  contactProtection: "Contact protection",
  /** A checkpoint. */
  savedProgress: "Saved progress",
  /** Writing to the CRM. */
  updatingRecords: "Updating your records",
  /** An API credential. */
  connectYourTool: "Connect your tool",
  testMode: "Test Mode",
  liveMode: "Live Mode",
} as const;

/** Small helper: fall back to a readable form of an unmapped value. */
function humanize(value: string): string {
  if (!value) return "";
  const spaced = value.replace(/_/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// --------------------------------------------------------------------------- //
// Where and who
// --------------------------------------------------------------------------- //

const REGION_LABELS: Record<Region, string> = {
  US: "United States",
  UK: "United Kingdom",
  EU: "Europe",
  CA: "Canada",
  AU: "Australia",
  ME: "Middle East",
};

/** Short form for table cells and pills, where the full name will not fit. */
const REGION_SHORT: Record<Region, string> = {
  US: "US",
  UK: "UK",
  EU: "EU",
  CA: "Canada",
  AU: "Australia",
  ME: "Middle East",
};

export function regionLabel(region: Region | string): string {
  return REGION_LABELS[region as Region] ?? humanize(String(region));
}

export function regionShort(region: Region | string): string {
  return REGION_SHORT[region as Region] ?? String(region);
}

const LANGUAGE_LABELS: Record<string, string> = {
  en: "English",
  fr: "French",
  de: "German",
  nl: "Dutch",
  es: "Spanish",
  it: "Italian",
  pt: "Portuguese",
  ar: "Arabic",
};

export function languageLabel(code: string): string {
  if (!code) return "";
  return LANGUAGE_LABELS[code.toLowerCase()] ?? code.toUpperCase();
}

// --------------------------------------------------------------------------- //
// How we reach them
// --------------------------------------------------------------------------- //

const CHANNEL_LABELS: Record<Channel, string> = {
  email: "Email",
  linkedin: "LinkedIn",
  both: "Email and LinkedIn",
};

export function channelLabel(channel: Channel | string): string {
  return CHANNEL_LABELS[channel as Channel] ?? humanize(String(channel));
}

// --------------------------------------------------------------------------- //
// Approval
// --------------------------------------------------------------------------- //

const APPROVAL_LABELS: Record<ApprovalStatus, string> = {
  pending: "Waiting for your review",
  approved: "Approved by you",
  edited: "Edited and approved",
  rejected: "Rejected",
};

export function approvalLabel(status: ApprovalStatus | string): string {
  return APPROVAL_LABELS[status as ApprovalStatus] ?? humanize(String(status));
}

// --------------------------------------------------------------------------- //
// Sending
// --------------------------------------------------------------------------- //

const SEND_LABELS: Record<SendStatus, string> = {
  not_sent: "Not sent yet",
  sent: "Sent",
  failed: "Could not be delivered",
  rate_limited: "Held back - daily limit reached",
  // The whole LinkedIn design: the software never sends, the person does.
  pending_manual_send: "Ready to send on LinkedIn",
};

export function sendLabel(status: SendStatus | string): string {
  return SEND_LABELS[status as SendStatus] ?? humanize(String(status));
}

// --------------------------------------------------------------------------- //
// Suppression
//
// These are not shown as lead statuses in normal browsing -- a lead that has
// opted out or that local rules forbid contacting simply does not appear in the
// active list (see lib/matchQuality.ts `isActive`). The labels exist for the
// two places a suppressed lead is legitimately visible: an explicitly chosen
// "Opted out" filter, and the Technical details panel on a lead.
// --------------------------------------------------------------------------- //

const SUPPRESSION_LABELS: Record<SuppressionStatus, string> = {
  clear: "OK to contact",
  blocked_optout: "Opted out - will not be contacted",
  blocked_compliance: "Cannot be contacted under local rules",
};

export function suppressionLabel(status: SuppressionStatus | string): string {
  return SUPPRESSION_LABELS[status as SuppressionStatus] ?? humanize(String(status));
}

// --------------------------------------------------------------------------- //
// Replies
// --------------------------------------------------------------------------- //

const REPLY_LABELS: Record<ReplyCategory, string> = {
  interested: "Interested",
  not_interested: "Not interested",
  // "Objection" is a sales-training word. What it actually means to the user is
  // that the person wrote back with a question or a doubt.
  objection: "Replied with questions",
  out_of_office: "Out of office",
  no_reply: "No reply yet",
};

export function replyLabel(category: ReplyCategory | string): string {
  return REPLY_LABELS[category as ReplyCategory] ?? humanize(String(category));
}

// --------------------------------------------------------------------------- //
// Where a lead came from
// --------------------------------------------------------------------------- //

const SOURCE_LABELS: Record<LeadSource, string> = {
  google_places: "Google Maps",
  osm: "OpenStreetMap",
  apollo: "Apollo",
  csv: "List you imported",
  fixture: "Sample data",
  hunter: "Email lookup",
  scrape: "Their website",
  unknown: "Unknown",
};

export function sourceLabel(source: LeadSource | string): string {
  return SOURCE_LABELS[source as LeadSource] ?? humanize(String(source));
}

// --------------------------------------------------------------------------- //
// Why a lead stopped
// --------------------------------------------------------------------------- //

const ARCHIVE_LABELS: Record<string, string> = {
  below_threshold: "Not a match",
  rejected: "You rejected the message",
  suppressed: "Opted out",
  cadence_exhausted: "Followed up, no reply",
  unreachable: "No way to contact them",
  blocked_compliance: "Local rules do not allow contact",
  no_channel: "No usable way to reach them",
  no_company_name: "Record was incomplete",
  connection_not_accepted: "LinkedIn request was not accepted",
  replied_interested: "Interested - over to you",
  replied_not_interested: "Said no thanks",
  replied_objection: "Replied with questions",
  "": "",
};

export function archiveReasonLabel(reason: ArchiveReason | string): string {
  return ARCHIVE_LABELS[String(reason)] ?? humanize(String(reason));
}

// --------------------------------------------------------------------------- //
// Outcome
//
// `lib/outcome.ts` derives these from the lead exactly the way the backend
// does. This is where they become sentences.
// --------------------------------------------------------------------------- //

const OUTCOME_LABELS: Record<string, string> = {
  needs_manual_review: "Needs you to take a look",
  interested_manual_handoff: "Interested - over to you",
  unreachable: "No way to contact them",
  suppressed: "Opted out",
  blocked_compliance: "Local rules do not allow contact",
  queued_for_next_run: "Queued for the next run",
  awaiting_manual_linkedin_send: "Ready to send on LinkedIn",
  sent_awaiting_reply: "Sent, waiting for a reply",
  awaiting_approval: "Waiting for your review",
  below_threshold: "Not a match",
  cadence_exhausted: "Followed up, no reply",
  rejected: "You rejected the message",
  no_channel: "No usable way to reach them",
  replied_interested: "Interested",
  replied_not_interested: "Not interested",
  replied_objection: "Replied with questions",
  connection_not_accepted: "LinkedIn request was not accepted",
  in_progress: "Working on it",
  not_sent: "Not sent yet",
  failed: "Could not be delivered",
  rate_limited: "Held back - daily limit reached",
};

/** Matches the backend's parameterised `in_sequence_touch_<n>_scheduled`. */
const SEQUENCE_OUTCOME = /^in_sequence_touch_(\d+)_scheduled$/;

export function outcomeLabel(outcome: string): string {
  if (SEQUENCE_OUTCOME.test(outcome)) return "Follow-up scheduled";
  return OUTCOME_LABELS[outcome] ?? humanize(outcome);
}

/**
 * The extra clause worth showing next to the label, where there is one --
 * "follow-up 2 of 3" rather than burying the number inside the status.
 */
export function outcomeDetail(outcome: string): string {
  const match = SEQUENCE_OUTCOME.exec(outcome);
  return match ? `follow-up ${match[1]}` : "";
}

/** The tone a status should be drawn in. Colour is never the only signal. */
export type StatusTone = "positive" | "attention" | "negative" | "neutral" | "accent";

const OUTCOME_TONES: Record<string, StatusTone> = {
  replied_interested: "positive",
  interested_manual_handoff: "positive",
  sent_awaiting_reply: "accent",
  awaiting_approval: "attention",
  needs_manual_review: "attention",
  awaiting_manual_linkedin_send: "attention",
  rate_limited: "attention",
  failed: "negative",
  rejected: "negative",
  replied_not_interested: "negative",
  suppressed: "negative",
  blocked_compliance: "negative",
  unreachable: "neutral",
  below_threshold: "neutral",
  cadence_exhausted: "neutral",
  no_channel: "neutral",
  connection_not_accepted: "neutral",
  replied_objection: "attention",
  queued_for_next_run: "neutral",
  in_progress: "accent",
  not_sent: "neutral",
};

export function outcomeTone(outcome: string): StatusTone {
  if (SEQUENCE_OUTCOME.test(outcome)) return "accent";
  return OUTCOME_TONES[outcome] ?? "neutral";
}

// --------------------------------------------------------------------------- //
// The LinkedIn queue
//
// Every verb here is something a person did, not something the software did.
// --------------------------------------------------------------------------- //

const LINKEDIN_LABELS: Record<LinkedInQueueStatus, string> = {
  pending: "To send",
  sent: "You sent it",
  connected: "They accepted",
  skipped: "Skipped",
};

export function linkedInStatusLabel(status: LinkedInQueueStatus | string): string {
  return LINKEDIN_LABELS[status as LinkedInQueueStatus] ?? humanize(String(status));
}

// --------------------------------------------------------------------------- //
// A run in progress
// --------------------------------------------------------------------------- //

const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  queued: "Waiting to start",
  running: "Running",
  paused: "Paused for your review",
  completed: "Finished",
  failed: "Stopped early",
};

export function runStatusLabel(status: RunStatus | string): string {
  return RUN_STATUS_LABELS[status as RunStatus] ?? humanize(String(status));
}

/**
 * What each stage of a run is doing, in the user's terms.
 *
 * The backend runs thirteen distinct processing steps. Naming them here would
 * be the fastest possible way to turn this back into an internal tool, so the
 * product describes the work rather than the machinery. `RunStage` in
 * types/agent.ts carries the same granularity under business names, which is
 * why the mapping is one-to-one and not lossy.
 */
const STAGE_LABELS: Record<RunStage, string> = {
  understanding: "Understanding your request",
  finding: "Finding businesses that match your criteria",
  researching: "Researching each business",
  matching: "Checking how well each business matches",
  choosing_channel: "Choosing the best way to reach them",
  drafting: "Writing your outreach",
  reviewing: "Waiting for your review",
  checking_rules: "Contact protection checks",
  sending_email: "Sending emails",
  linkedin_prep: "Preparing LinkedIn messages for you to send",
  watching_replies: "Watching for replies",
  following_up: "Scheduling follow-ups",
  saving: "Updating your records",
};

export function stageLabel(stage: RunStage | string): string {
  return STAGE_LABELS[stage as RunStage] ?? humanize(String(stage));
}

/** The short form, for a progress list where the full sentence is too long. */
const STAGE_SHORT: Record<RunStage, string> = {
  understanding: "Understanding your request",
  finding: "Finding businesses",
  researching: "Researching",
  matching: "Checking matches",
  choosing_channel: "Choosing a channel",
  drafting: "Writing outreach",
  reviewing: "Your review",
  checking_rules: "Contact protection",
  sending_email: "Sending emails",
  linkedin_prep: "Preparing LinkedIn",
  watching_replies: "Watching for replies",
  following_up: "Follow-ups",
  saving: "Updating your records",
};

export function stageShort(stage: RunStage | string): string {
  return STAGE_SHORT[stage as RunStage] ?? humanize(String(stage));
}

/**
 * The four-beat progress list shown while a run is going. Deliberately coarser
 * than `RunStage`: someone watching a progress list wants to know roughly
 * where it is, and thirteen ticking rows reads as noise.
 */
export const RUN_PROGRESS_STEPS: { id: string; label: string; stages: RunStage[] }[] = [
  { id: "understanding", label: "Understanding your request", stages: ["understanding"] },
  { id: "finding", label: "Finding businesses", stages: ["finding", "researching"] },
  {
    id: "matching",
    label: "Checking how well they match",
    stages: ["matching", "choosing_channel"],
  },
  {
    id: "preparing",
    label: "Preparing outreach",
    stages: ["drafting", "checking_rules", "linkedin_prep", "sending_email", "saving"],
  },
];

// --------------------------------------------------------------------------- //
// Test Mode vs Live Mode
//
// The backend calls this `dry_run` and will keep calling it that. In the
// product it is a mode the user chooses, and the two must never look alike:
// see components/layout/ModeIndicator.tsx.
// --------------------------------------------------------------------------- //

export function modeLabel(dryRun: boolean): string {
  return dryRun ? "Test Mode" : "Live Mode";
}

export function modeDescription(dryRun: boolean): string {
  return dryRun
    ? "Nothing is sent. You see exactly what would go out."
    : "Approved messages are really sent to real people.";
}

/**
 * The verb to use about a message, given the mode it was produced in.
 * A message that was never sent must never be described as sent.
 */
export function sendVerb(dryRun: boolean, channel: Channel | string): string {
  if (channel === "linkedin") {
    return dryRun ? "would be queued for you to send" : "queued for you to send";
  }
  return dryRun ? "would be sent" : "sent";
}

// --------------------------------------------------------------------------- //
// Match quality
// --------------------------------------------------------------------------- //

export type MatchQuality =
  | "good_match"
  | "needs_review"
  | "not_a_match"
  | "unreachable"
  | "contacted"
  | "replied";

const MATCH_QUALITY_LABELS: Record<MatchQuality, string> = {
  good_match: "Good match",
  needs_review: "Needs review",
  not_a_match: "Not a match",
  unreachable: "Unreachable",
  contacted: "Contacted",
  replied: "Replied",
};

export function matchQualityLabel(quality: MatchQuality | string): string {
  return MATCH_QUALITY_LABELS[quality as MatchQuality] ?? humanize(String(quality));
}

const MATCH_QUALITY_TONES: Record<MatchQuality, StatusTone> = {
  good_match: "positive",
  needs_review: "attention",
  not_a_match: "neutral",
  unreachable: "neutral",
  contacted: "accent",
  replied: "positive",
};

export function matchQualityTone(quality: MatchQuality | string): StatusTone {
  return MATCH_QUALITY_TONES[quality as MatchQuality] ?? "neutral";
}

/**
 * How a match score reads next to the threshold. Wording rather than a bare
 * number, because "68 out of 60" is not a sentence anybody thinks in.
 */
export function scoreLabel(score: number, threshold: number): string {
  if (score <= 0) return "Not scored yet";
  const gap = score - threshold;
  if (gap >= 20) return "Strong match";
  if (gap >= 0) return "Good match";
  if (gap >= -15) return "Just below your bar";
  return "Not a match";
}

// --------------------------------------------------------------------------- //
// Providers, for Connections
// --------------------------------------------------------------------------- //

export type ConnectionState = "connected" | "not_connected" | "invalid" | "testing";

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connected: "Connected",
  not_connected: "Not connected",
  invalid: "Needs attention",
  testing: "Testing",
};

export function connectionLabel(state: ConnectionState | string): string {
  return CONNECTION_LABELS[state as ConnectionState] ?? humanize(String(state));
}

const CONNECTION_TONES: Record<ConnectionState, StatusTone> = {
  connected: "positive",
  not_connected: "neutral",
  invalid: "negative",
  testing: "accent",
};

export function connectionTone(state: ConnectionState | string): StatusTone {
  return CONNECTION_TONES[state as ConnectionState] ?? "neutral";
}

// --------------------------------------------------------------------------- //
// The funnel on Home
// --------------------------------------------------------------------------- //

const PIPELINE_STAGE_LABELS: Record<string, string> = {
  discovered: "Found",
  qualified: "Qualified",
  approved: "Approved",
  contacted: "Sent",
  replied: "Replied",
  booked: "Booked",
};

export function pipelineStageLabel(stage: string): string {
  return PIPELINE_STAGE_LABELS[stage] ?? humanize(stage);
}

/** One line on what each funnel step counts. No internals, no step numbers. */
const PIPELINE_STAGE_HINTS: Record<string, string> = {
  discovered: "Businesses found, with anything you had already seen removed",
  qualified: "Scored at or above the match bar you set",
  approved: "You approved the message that was written for them",
  contacted: "A message has gone out, or is waiting for you to send on LinkedIn",
  replied: "A person wrote back - out-of-office replies do not count",
  booked: "They replied and said yes - the conversation is yours from here",
};

export function pipelineStageHint(stage: string): string {
  return PIPELINE_STAGE_HINTS[stage] ?? "";
}

// --------------------------------------------------------------------------- //
// Audiences
// --------------------------------------------------------------------------- //

/**
 * The label for one of the user's configured audiences.
 *
 * There is deliberately no built-in list of audience names here. Audiences are
 * whatever the user configured -- cleaning companies, law firms, machine shops
 * -- so the label comes from their own config, and the fallback derives
 * something readable from the id rather than guessing at an industry.
 */
export function nicheLabel(nicheId: string, configured?: string): string {
  if (configured) return configured;
  if (!nicheId) return "";
  return humanize(nicheId.replace(/^(local|b2b|saas)_/, ""));
}

/** "Local businesses" / "Companies" -- the two kinds of audience. */
export function nicheKindLabel(kind: string): string {
  if (kind === "local_business") return "Local businesses";
  if (kind === "b2b") return "Companies";
  return humanize(kind);
}
