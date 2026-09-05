/**
 * workspace.ts -- the one workspace this deployment is.
 *
 * There are no accounts and no workspace switcher: one deployment serves one
 * business. The backend still carries a `tenant_id` internally (it is how
 * every file, counter and suppression list is namespaced, and removing it
 * would be a rewrite for no user-visible gain), but nothing in this UI ever
 * asks the user to choose or create one.
 *
 * Everything here is EMPTY until the user fills it in. No sample business, no
 * sample person, no placeholder name that could end up on a real email.
 */

import type { Channel, Region } from "./lead";

// --------------------------------------------------------------------------- //
// Business profile -- the single source of truth for identity
// --------------------------------------------------------------------------- //

/**
 * Who the user is and what they sell.
 *
 * Every field is optional and blank by default. Anywhere the product would
 * show a sender identity, it reads from here; if the field is empty it says so
 * and links here, rather than inventing a name. A fabricated sender identity
 * is not a cosmetic problem -- it is a name that could go out on real mail.
 *
 * NOTE: these values are captured, persisted and displayed today. Feeding
 * `what_you_sell` / `tone_of_voice` into the outreach-drafting prompt is a
 * backend task for a later phase; see backend/routers/profile.py.
 */
export interface BusinessProfile {
  business_name: string;
  website: string;
  /** What you sell. */
  what_you_sell: string;
  /** Who you serve. */
  who_you_serve: string;
  /** Problems you solve for them. */
  problems_you_solve: string;
  /** What makes you different. */
  what_makes_you_different: string;
  /** The services offered, when they are worth naming one by one. */
  services: string;
  /** The markets served. Not the same as the audiences in Targeting: this is
      who the business already sells to, not who to go looking for. */
  target_markets: string;
  /** How your outreach should sound. */
  tone_of_voice: string;
  sender_name: string;
  sender_email: string;
  /**
   * Postal address. Required by US and Canadian rules for commercial email,
   * which is why it is on the profile and not buried in an advanced panel.
   */
  postal_address: string;
  /** Which LinkedIn account the user sends from, by hand. */
  linkedin_account: string;
}

export const EMPTY_BUSINESS_PROFILE: BusinessProfile = {
  business_name: "",
  website: "",
  what_you_sell: "",
  who_you_serve: "",
  problems_you_solve: "",
  what_makes_you_different: "",
  services: "",
  target_markets: "",
  tone_of_voice: "",
  sender_name: "",
  sender_email: "",
  postal_address: "",
  linkedin_account: "",
};

/** True once there is enough identity to put a name on an email. */
export function hasSenderIdentity(profile: BusinessProfile): boolean {
  return Boolean(profile.sender_name.trim() && profile.sender_email.trim());
}

// --------------------------------------------------------------------------- //
// Connections -- bring your own keys
// --------------------------------------------------------------------------- //

export type ConnectionState = "connected" | "not_connected" | "invalid";

/**
 * A job the product needs done, independent of who does it.
 *
 * These eight strings are the contract: they are the keys in the tenant
 * config's `providers:` block, the `capability` field on every provider the
 * API returns, and the path segment of `PUT /api/providers/selection/{...}`.
 * They are never shown to the user -- the heading and description for each
 * one come from the API, so the wording lives in one place.
 *
 * Discovery is two capabilities rather than one because the two are not
 * interchangeable: a contact database cannot find a dental practice in
 * Manchester, and a map search cannot find a Head of Support.
 */
export type Capability =
  | "llm"
  | "discovery_local"
  | "discovery_b2b"
  | "enrichment"
  | "email_sender"
  | "email_reader"
  | "crm"
  | "translation";

/**
 * One field a provider needs. Declared by the backend, not hard-coded here, so
 * adding a provider is a backend change and the UI follows automatically.
 */
export interface ProviderField {
  name: string;
  label: string;
  /** `secret` fields are write-only: masked on input, never sent back. */
  kind: "secret" | "text";
  placeholder: string;
  /** Shown under the field. Plain language, one line. */
  hint: string;
  required: boolean;
  /** True when the field takes a pasted multi-line blob (a JSON key file). */
  multiline?: boolean;
}

/**
 * A provider and whether this workspace has it connected.
 *
 * There is no key here, masked or otherwise. The backend never returns a saved
 * secret in any form -- see backend/secrets_store.py. `last4` is the only
 * fragment that comes back, and it exists so the user can tell which key they
 * saved, not so anything can be reconstructed from it.
 */
export interface Connection {
  id: string;
  name: string;
  /** Which job this provider does. */
  category: Capability;
  /** What it does for the user, one line. */
  purpose: string;
  /** The free allowance, e.g. "25 lookups a month". */
  free_tier: string;
  /** Where to get a key. */
  help_label: string;
  help_url: string;
  fields: ProviderField[];
  state: ConnectionState;
  /** Last four characters of the saved secret. Empty when not connected. */
  last4: string;
  /** When it was saved. */
  connected_at: string | null;
  /** Present when `state` is "invalid": what went wrong, in plain language. */
  problem: string;
  /** True when the product cannot run at all without this one. */
  essential: boolean;
  /** True when another provider in the same category is already connected. */
  alternative_connected: boolean;
  /**
   * False for a provider that is listed but not built -- shown so the user can
   * see what is planned, never offered as something they can connect.
   */
  enabled: boolean;
  /** True for the generic "point it at your own endpoint" provider. */
  is_custom: boolean;
  /** False when this one needs no account and no key. */
  needs_credentials: boolean;
}

// --------------------------------------------------------------------------- //
// Choosing who does each job
// --------------------------------------------------------------------------- //

/**
 * Everything that can do one job, plus how to describe that job.
 *
 * Served by `GET /api/providers`. The title and hint come from the backend on
 * purpose: they used to be a hardcoded array in Connections.tsx, which meant
 * adding a provider needed an edit here as well as in the registry, and the
 * two drifted.
 */
export interface CapabilityGroup {
  capability: Capability;
  /** The heading, in the user's terms. */
  title: string;
  /** One line under the heading. */
  hint: string;
  /** What a workspace gets if it has chosen nothing. */
  default: string;
  providers: Connection[];
}

/** What this workspace has chosen for one job. */
export interface ProviderSelection {
  capability: Capability;
  primary: string;
  /** Used when the primary cannot run. Empty when none is set. */
  fallback: string;
  /** The chosen provider's display name, so a summary needs no lookup. */
  primary_name: string;
  /**
   * Where the answer came from: the workspace's own settings, an older config
   * field, an environment variable, or the built-in default. Shown under
   * "Technical details" so "why is it using this one" has an answer.
   */
  chosen_by: string;
  /** True when the chosen provider has what it needs to actually run. */
  ready: boolean;
  /**
   * Non-secret configuration for a custom endpoint: URL, auth style, model.
   * Never contains a key -- the API refuses to store one here.
   */
  settings: Record<string, string>;
}

/** The whole selection, keyed by capability. */
export type SelectionMap = Partial<Record<Capability, ProviderSelection>>;

export interface ConnectionTestResult {
  ok: boolean;
  /** What the check found, in plain language. Shown whether it passed or not. */
  message: string;
  /** Extra context worth showing, e.g. the plan the key is on. */
  detail: string;
}

// --------------------------------------------------------------------------- //
// Targeting -- who to look for
// --------------------------------------------------------------------------- //

/**
 * One audience the user wants to find. Backed by the existing tenant config
 * schema -- this is an editor for that file, not a second store.
 */
export interface NicheConfig {
  id: string;
  label: string;
  /** Whether these are local businesses or other companies. */
  kind: "local_business" | "b2b";
  /** What to search for. */
  search_terms: string[];
  /** Job titles to target, for company audiences. */
  titles: string[];
  /** Cities or countries, grouped by region. */
  locations: Partial<Record<Region, string[]>>;
  /** Who they are, in a sentence or two. */
  description: string;
  /** What must be true for them to be worth contacting. */
  must_have: string[];
  /** The buying signals worth reaching out about. */
  good_signals: string[];
  /** What rules them out. */
  disqualifiers: string[];
  /**
   * Which channel this audience is reached on first.
   *
   * Explicit, and read directly by the backend. It used to be inferred from
   * the audience's id, which quietly broke for any name the convention did not
   * anticipate -- an audience called "chicago_law_firms" would have been
   * treated as a local business by accident.
   */
  channel_default: Channel;
  /** How this audience's outreach should sound. Falls back to the profile. */
  tone: string;
  /** Rough size, e.g. "2-8 staff". */
  size_hint: string;
}

/** A niche the product drafted from a description, awaiting confirmation. */
export interface NicheDraft extends NicheConfig {
  /** What the request was read as, shown back before saving. */
  interpretation: string;
}

export function emptyNiche(): NicheConfig {
  return {
    id: "",
    label: "",
    kind: "local_business",
    search_terms: [],
    titles: [],
    locations: {},
    description: "",
    must_have: [],
    good_signals: [],
    disqualifiers: [],
    channel_default: "email",
    tone: "",
    size_hint: "",
  };
}

// --------------------------------------------------------------------------- //
// Rules for Leo
// --------------------------------------------------------------------------- //

/**
 * A standing instruction the user has given Leo.
 *
 * NOTE: rules are captured, listed and persisted today. Feeding them into the
 * outreach-drafting prompt is a backend task for a later phase; see
 * backend/routers/agent_rules.py.
 */
export interface AgentRule {
  id: string;
  /** "always" reads as a thing to do, "never" as a thing to avoid. */
  kind: "always" | "never";
  text: string;
  created_at: string;
}

// --------------------------------------------------------------------------- //
// Everything else the settings screens edit
// --------------------------------------------------------------------------- //

/** Rules about sending. Region compliance profiles are read-only here. */
export interface OutreachRules {
  regions: Region[];
  language_map: Partial<Record<Region, string>>;
  channels: { enabled: Channel[]; preference_when_both: Channel };
  fit_score_threshold: number;
  max_leads_per_run: number;
  daily_send_limits: Record<string, number>;
  require_approval_before_send: boolean;
  blocked_domains: string[];
  follow_ups: { step: number; name: string; wait_days: number; channel: Channel }[];
  /** True when new runs default to Test Mode. */
  test_mode_default: boolean;
}

/** The whole workspace, as the settings screens see it. */
export interface WorkspaceSettings {
  /** Internal namespace. Displayed only inside Technical details. */
  tenant_id: string;
  profile: BusinessProfile;
  niches: NicheConfig[];
  rules: OutreachRules;
}

/**
 * What is still missing before the product can do anything useful. Drives the
 * Home checklist and the pre-flight check on Find Leads, from one calculation
 * rather than two that can disagree.
 */
export interface SetupState {
  has_ai: boolean;
  has_discovery: boolean;
  has_email_sending: boolean;
  has_profile: boolean;
  has_niches: boolean;
  /** True when nothing at all is configured -- a fresh install. */
  fresh: boolean;
  /** True when a run could actually produce something. */
  ready_to_run: boolean;
}
