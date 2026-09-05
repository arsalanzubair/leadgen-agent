/**
 * lead.ts -- a 1:1 mirror of `LeadState` in src/state.py.
 *
 * Field names and enum values are IDENTICAL to the Python contract. Nothing is
 * renamed, camel-cased or prettified here: `blocked_optout` stays
 * `blocked_optout`, and only render-time helpers in lib/format.ts turn it into
 * "Opted out". That is what makes swapping services/localApi.ts for a real
 * FastAPI response a non-event.
 *
 * Datetimes arrive as ISO-8601 strings (the shape state.py's `_cell()`
 * produces for the CRM and the shape a JSON API would return), not Date
 * objects. Parse at the edge of a component, never in the model.
 */

// --- enums (src/state.py) --------------------------------------------------- //

export type Region = "US" | "UK" | "EU" | "CA" | "AU" | "ME";
export const REGIONS: Region[] = ["US", "UK", "EU", "CA", "AU", "ME"];

export type Channel = "email" | "linkedin" | "both";
export const CHANNELS: Channel[] = ["email", "linkedin", "both"];

export type ApprovalStatus = "pending" | "approved" | "edited" | "rejected";
export const APPROVAL_STATUSES: ApprovalStatus[] = [
  "pending",
  "approved",
  "edited",
  "rejected",
];

/** state.py APPROVED_STATUSES -- `edited` counts as approved. */
export const APPROVED_STATUSES: ApprovalStatus[] = ["approved", "edited"];

export type SuppressionStatus = "clear" | "blocked_optout" | "blocked_compliance";
export const SUPPRESSION_STATUSES: SuppressionStatus[] = [
  "clear",
  "blocked_optout",
  "blocked_compliance",
];

export type SendStatus =
  | "not_sent"
  | "sent"
  | "failed"
  | "rate_limited"
  | "pending_manual_send";
export const SEND_STATUSES: SendStatus[] = [
  "not_sent",
  "sent",
  "failed",
  "rate_limited",
  "pending_manual_send",
];

export type ReplyCategory =
  | "interested"
  | "not_interested"
  | "objection"
  | "out_of_office"
  | "no_reply";
export const REPLY_CATEGORIES: ReplyCategory[] = [
  "interested",
  "not_interested",
  "objection",
  "out_of_office",
  "no_reply",
];

/** state.py AUTO_SUPPRESS_CATEGORIES. */
export const AUTO_SUPPRESS_CATEGORIES: ReplyCategory[] = ["not_interested"];
/** state.py EXITS_SEQUENCE_CATEGORIES. */
export const EXITS_SEQUENCE_CATEGORIES: ReplyCategory[] = [
  "interested",
  "not_interested",
];

/** `source` on LeadState -- the comment in state.py enumerates these. */
export type LeadSource = "google_places" | "osm" | "apollo" | "csv" | "fixture" | "hunter" | "scrape" | "unknown";

/**
 * `archive_reason` values actually written by the nodes. Grepped out of
 * src/nodes/ rather than guessed.
 */
export type ArchiveReason =
  | "below_threshold"
  | "rejected"
  | "suppressed"
  | "cadence_exhausted"
  | "unreachable"
  | "blocked_compliance"
  | "no_channel"
  | "no_company_name"
  | "connection_not_accepted"
  | "replied_interested"
  | "replied_not_interested"
  | "replied_objection"
  | "";

/** state.py MAX_LINKEDIN_CONNECTION_NOTE_CHARS. */
export const MAX_LINKEDIN_CONNECTION_NOTE_CHARS = 300;

/** state.py FREE_TIER_LIMITS. */
export const FREE_TIER_LIMITS = {
  hunter_lookups_per_month: 25,
  gmail_sends_per_day: 500,
  brevo_sends_per_day: 300,
  deepl_chars_per_month: 500_000,
} as const;

// --- draft structs (state.py EmailDraft / LinkedInDraft / DraftMessage) ----- //

export interface EmailDraft {
  subject?: string;
  body?: string;
}

export interface LinkedInDraft {
  connection_note?: string;
  followup_dm?: string;
}

export interface DraftMessage {
  email?: EmailDraft;
  linkedin?: LinkedInDraft;
  signal_referenced?: string;
  /** BCP-47-ish tag the draft was localised into. */
  language?: string;
  /** True if translation.py rewrote the source copy. */
  translated?: boolean;
  /** Set by N5 when a human rewrote the draft. */
  edited_by_human?: boolean;
}

/** state.py `errors` -- append-only {node, error, at} audit trail. */
export interface LeadError {
  node: string;
  error: string;
  at: string;
}

// --- the lead ---------------------------------------------------------------- //

export interface Lead {
  // identity
  tenant_id: string;
  niche_id: string;
  region: Region;
  language: string;
  lead_id: string;

  // company
  company_name: string;
  website: string;
  linkedin_url: string;
  location: string;
  industry: string;

  // contact
  contact_email: string;
  contact_name: string;

  // research
  signals: string[];

  // qualification
  fit_score: number; // 0-100
  fit_reason: string;

  // outreach
  channel: Channel;
  draft_message: DraftMessage;
  approval_status: ApprovalStatus;
  suppression_status: SuppressionStatus;
  send_status: SendStatus;

  // sequencing
  sequence_step: number;
  next_action: string;

  // replies
  reply_text: string;
  reply_category: ReplyCategory;

  // meta
  last_updated: string;

  // operational (the additive block in state.py)
  source: LeadSource;
  discovered_at: string;
  dedupe_key: string;
  unreachable: boolean;
  needs_manual_review: boolean;
  manual_review_reason: string;
  sent_at: string | null;
  last_touch_at: string | null;
  next_touch_due: string | null;
  archived: boolean;
  archive_reason: ArchiveReason;
  dry_run: boolean;
  errors: LeadError[];
}

/**
 * The exact header order from state.py CRM_COLUMNS. Used by the Leads export
 * and by the Analytics page's column picker, so the UI and the Google Sheet a
 * Looker report reads can never disagree.
 */
export const CRM_COLUMNS = [
  "lead_id",
  "tenant_id",
  "niche_id",
  "region",
  "language",
  "company_name",
  "website",
  "linkedin_url",
  "location",
  "industry",
  "contact_name",
  "contact_email",
  "fit_score",
  "fit_reason",
  "signals",
  "channel",
  "approval_status",
  "suppression_status",
  "send_status",
  "sequence_step",
  "next_action",
  "reply_category",
  "reply_text",
  "unreachable",
  "needs_manual_review",
  "archived",
  "archive_reason",
  "source",
  "discovered_at",
  "sent_at",
  "last_touch_at",
  "next_touch_due",
  "last_updated",
] as const satisfies readonly (keyof Lead)[];

export type CrmColumn = (typeof CRM_COLUMNS)[number];

/**
 * Derived outcome, mirroring `outcome_of()` in src/nodes/n9_crm_analytics.py.
 * Not stored on the lead in Python either -- it is computed there too, so
 * lib/outcome.ts recomputes it here with the identical precedence.
 */
export type LeadOutcome =
  | "needs_manual_review"
  | "interested_manual_handoff"
  | "unreachable"
  | "suppressed"
  | "blocked_compliance"
  | "queued_for_next_run"
  | "awaiting_manual_linkedin_send"
  | "sent_awaiting_reply"
  | "awaiting_approval"
  | "below_threshold"
  | "cadence_exhausted"
  | "rejected"
  | "no_channel"
  | "replied_interested"
  | "replied_not_interested"
  | "replied_objection"
  | "connection_not_accepted"
  | "in_progress"
  | (string & {}); // `in_sequence_touch_N_scheduled` and raw send_status values

// --- query shapes ------------------------------------------------------------ //

/**
 * Filters accepted by getLeads(). Named to match the CLI flags the Python side
 * already exposes (`--niche`, `--region`) so a future FastAPI route can pass
 * them straight through.
 */
export interface LeadFilters {
  tenant_id?: string;
  search?: string;
  region?: Region | "all";
  niche_id?: string | "all";
  channel?: Channel | "all";
  approval_status?: ApprovalStatus | "all";
  send_status?: SendStatus | "all";
  suppression_status?: SuppressionStatus | "all";
  /** Pipeline stage, for the Overview funnel's click-through. */
  stage?: PipelineStage | "all";
  archived?: boolean;
  min_fit_score?: number;
  sort_by?: keyof Lead;
  sort_dir?: "asc" | "desc";
}

/**
 * The funnel on Home, in order: Found, Qualified, Approved, Sent, Replied,
 * Booked.
 *
 * Six steps, and the sixth is the only one the backend cannot observe on its
 * own -- nothing in the pipeline sees a meeting get booked. It counts the
 * leads who replied and said yes, which is the last thing this product knows
 * about before the conversation becomes the user's.
 */
export type PipelineStage =
  | "discovered"
  | "qualified"
  | "approved"
  | "contacted"
  | "replied"
  | "booked";

export const PIPELINE_STAGES: PipelineStage[] = [
  "discovered",
  "qualified",
  "approved",
  "contacted",
  "replied",
  "booked",
];
