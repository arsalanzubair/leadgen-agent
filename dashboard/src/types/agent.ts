/**
 * agent.ts -- runs, queues and progress.
 *
 * The backend executes a fixed sequence of processing steps. This file names
 * those steps for what they DO to a lead, not for what they are called
 * internally, and that naming is deliberate rather than cosmetic: the product
 * is sold to people who run outreach, and a screen full of internal step
 * identifiers is the fastest way to make a business tool read as somebody's
 * debugging console.
 *
 * The granularity is unchanged -- one `RunStage` per real processing step -- so
 * nothing is lost in translation and an activity log still shows exactly where
 * a lead is. Only the vocabulary is the user's instead of the engine's.
 * `lib/statusLabels.ts` turns these into sentences.
 */

import type { Channel, Lead, Region } from "./lead";

/**
 * The stages of a run, in order. One per real processing step.
 *
 * `linkedin_prep` is a stage on its own rather than part of `sending_email`
 * because LinkedIn is never sent by the software -- that stage prepares a
 * message for a person to send by hand, which is a different kind of event and
 * needs to read differently everywhere it appears.
 */
export type RunStage =
  | "understanding"
  | "finding"
  | "researching"
  | "matching"
  | "choosing_channel"
  | "drafting"
  | "reviewing"
  | "checking_rules"
  | "sending_email"
  | "linkedin_prep"
  | "watching_replies"
  | "following_up"
  | "saving";

export const RUN_STAGES: RunStage[] = [
  "understanding",
  "finding",
  "researching",
  "matching",
  "choosing_channel",
  "drafting",
  "reviewing",
  "checking_rules",
  "sending_email",
  "linkedin_prep",
  "watching_replies",
  "following_up",
  "saving",
];

export type StageStatus = "completed" | "running" | "waiting" | "skipped" | "failed";

/** A stage's state within one run. */
export interface StageProgress {
  stage: RunStage;
  status: StageStatus;
  /** Milliseconds. Null while waiting. */
  duration_ms: number | null;
  leads_in: number;
  leads_out: number;
  /** Leads this stage moved off the main path (not a match, needs a human). */
  leads_diverted: number;
  started_at: string | null;
  finished_at: string | null;
  /** One plain sentence about what happened here. */
  note: string;
}

export type RunStatus = "queued" | "running" | "paused" | "completed" | "failed";

/** One search-and-outreach run. */
export interface AgentRun {
  run_id: string;
  tenant_id: string;
  status: RunStatus;
  /** The backend's own name for Test Mode. Never rendered as "dry run". */
  dry_run: boolean;
  started_at: string;
  finished_at: string | null;
  /** The (niche, region) pairs this run covered. */
  targets: BatchTarget[];
  /** Targets that returned fewer new businesses than expected. */
  low_yield_targets: BatchTarget[];
  leads_discovered: number;
  leads_qualified: number;
  lead_ids: string[];
  needs_manual_review: string[];
  archived: string[];
  stages: StageProgress[];
  /** What the run was asked to do, in the user's own words. */
  prompt: string;
  config: RunConfig;
  /** Why it stopped, when it stopped badly. Empty on a run that finished. */
  error?: string;
}

/** One (niche, region) pair a run covered. Mirrors the backend's BatchTarget. */
export interface BatchTarget {
  niche_id: string;
  region: Region;
  language: string;
}

/**
 * What the product understood from a plain-language request, shown for
 * confirmation before anything runs. Every field maps to something the tenant
 * config schema already supports.
 */
export interface RunConfig {
  /**
   * The audience the request described, as the AI step read it, when the
   * workspace has nothing matching it yet. Passed straight back when the
   * search starts so the service can save it then rather than while the user
   * is still typing. Absent when an existing audience already covers it.
   */
  niche_draft?: Record<string, unknown> | null;
  /** One sentence saying what was understood. Not always present. */
  interpretation?: string;
  /** What the user sells -- free text, shown back for confirmation. */
  offering: string;
  /** Who they want to reach -- free text. */
  audience: string;
  niche_ids: string[];
  regions: Region[];
  /** Cities or countries named in the request, if any. */
  locations: string[];
  /** The buying signals to look for. */
  signals: string[];
  channels: Channel[];
  /** Language per region. */
  language_map: Partial<Record<Region, string>>;
  /** Minimum match score a business needs to be worth contacting. */
  fit_score_threshold: number;
  /** How many businesses to look for. */
  max_leads_per_run: number;
  /** Days between follow-ups. */
  follow_up_pace_days: number;
  /** True = Test Mode. */
  dry_run: boolean;
}

/**
 * Something a run needs before it can start. Surfaced BEFORE the run button is
 * pressed, never as a failure afterwards.
 */
export interface PreflightIssue {
  id: string;
  severity: "blocking" | "warning";
  /** What is missing, in one sentence. */
  message: string;
  /** Where to go and fix it. */
  action_label: string;
  action_href: string;
}

/** A single recorded event, for the Activity log. */
export interface ActivityEvent {
  id: string;
  tenant_id: string;
  run_id: string;
  stage: RunStage;
  lead_id: string | null;
  company_name: string | null;
  level: "info" | "warning" | "error";
  message: string;
  at: string;
  duration_ms: number | null;
}

/** The Home feed's shape -- a thinner, friendlier ActivityEvent. */
export interface AgentFeedItem {
  id: string;
  stage: RunStage;
  headline: string;
  detail: string;
  status: "success" | "warning" | "danger" | "neutral";
  at: string;
  lead_id: string | null;
}

/** A message waiting for the user's review. */
export interface ApprovalItem {
  lead_id: string;
  tenant_id: string;
  company_name: string;
  contact_name: string;
  contact_email: string;
  linkedin_url: string;
  region: Region;
  niche_id: string;
  language: string;
  channel: Channel;
  fit_score: number;
  fit_reason: string;
  signals: string[];
  /** 1-based touch number being reviewed. */
  sequence_step: number;
  signal_referenced: string;
  translated: boolean;
  email: { subject?: string; body?: string };
  linkedin: { connection_note?: string; followup_dm?: string };
  dry_run: boolean;
  queued_at: string;
}

/** Statuses in the manual LinkedIn queue. Mirrors the backend's own values. */
export type LinkedInQueueStatus = "pending" | "sent" | "connected" | "skipped";

/** One item the user sends by hand on LinkedIn. */
export interface LinkedInQueueItem {
  lead_id: string;
  tenant_id: string;
  sequence_step: number;
  company_name: string;
  contact_name: string;
  linkedin_url: string;
  region: Region;
  niche_id: string;
  fit_score: number;
  fit_reason: string;
  connection_note: string;
  followup_dm: string;
  language: string;
  /**
   * Which LinkedIn account the user intends to send from. Comes from their
   * Business Profile; blank until they fill it in, and never invented.
   */
  account_label: string;
  status: LinkedInQueueStatus;
  queued_at: string;
  sent_at?: string;
  connected_at?: string;
  dry_run: boolean;
}

/** A due or upcoming follow-up. */
export interface FollowUp {
  lead_id: string;
  tenant_id: string;
  company_name: string;
  contact_name: string;
  region: Region;
  channel: Channel;
  /** Touches already made. */
  sequence_step: number;
  /** The touch that is next. */
  next_touch_number: number;
  /** Plain name for the next touch, e.g. "First follow-up". */
  next_step_name: string;
  next_step_channel: Channel;
  next_touch_due: string;
  /** How many touches this lead can receive in total. */
  max_touches: number;
  /** Set when a follow-up is waiting on something, e.g. a LinkedIn accept. */
  blocked_reason: string;
  steps: FollowUpStepState[];
}

export interface FollowUpStepState {
  step: number;
  name: string;
  channel: Channel;
  status: "done" | "due" | "scheduled" | "blocked" | "skipped";
  at: string | null;
}

/** The numbers on Home. */
export interface OverviewMetrics {
  new_leads: number;
  qualified: number;
  contacted: number;
  replies: number;
  /** Replies that said yes. See PipelineStage's note on "booked". */
  booked: number;
  /** Percentage of contacted businesses that wrote back. */
  reply_rate: number;
  pending_approval: number;
  linkedin_waiting: number;
  followups_due: number;
  /** Percentage change against the previous equivalent period. */
  trends: Record<string, number>;
  /** The funnel, in plain stage names. */
  pipeline: { stage: string; label: string; count: number }[];
}

/**
 * How much of a provider's free allowance is used up. Every provider here has
 * a free tier with a hard ceiling, and the backend counts against it, so this
 * is a real budget rather than a vanity meter.
 */
export interface QuotaState {
  name: string;
  label: string;
  used: number;
  cap: number;
  period: "day" | "month";
  resets: string;
}

export type { Lead };
