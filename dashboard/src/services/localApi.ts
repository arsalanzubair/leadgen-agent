/**
 * localApi.ts -- the in-session implementation of `LeadgenApi`.
 *
 * It starts EMPTY. There is no seeded lead, run, message or number anywhere in
 * this file or behind it: every screen shows its own empty state until a real
 * run puts something here, and every roll-up (the Home numbers, the funnel,
 * the campaign cards, Performance) is computed from whatever is actually
 * present, which at rest is nothing.
 *
 * That is the point. Sample leads made the product demonstrable and made it
 * impossible to tell, at a glance, whether a number on screen was the user's
 * or the fixture's -- and a fabricated business carrying a fabricated draft
 * message is exactly the thing that must never be mistaken for real outreach.
 *
 * Mutations (approvals, LinkedIn marks, starting a run) update the in-memory
 * copy so the product behaves like a real system within a session, and a
 * reload clears it. Nothing here is persisted.
 *
 * What is NOT here: the user's business profile, their API keys, their
 * audiences and their rules for Leo. Those were never mocked -- they go to the
 * real service in backend/ through `workspaceApi.ts`.
 *
 * When the backend grows the lead routes that `api.ts` already declares,
 * `index.ts` switches to them in one line and this file goes away.
 */

import { DEFAULT_RULES } from "@/data/workspaceDefaults";
import { reachedStage } from "@/lib/outcome";
import { hasReplied, isAwaitingReview, isContacted } from "@/lib/leadFields";
import { stageShort } from "@/lib/statusLabels";
import { groupBy, pct, sleep } from "@/lib/utils";
import type {
  ActivityEvent,
  AgentFeedItem,
  AgentRun,
  ApprovalItem,
  FollowUp,
  LinkedInQueueItem,
  OverviewMetrics,
  QuotaState,
  RunConfig,
  RunStage,
  StageProgress,
} from "@/types/agent";
import { RUN_STAGES } from "@/types/agent";
import type { AnalyticsBundle, SegmentRow } from "@/types/campaign";
import type {
  ApprovalStatus,
  Channel,
  Lead,
  LeadFilters,
  PipelineStage,
  Region,
} from "@/types/lead";
import { PIPELINE_STAGES, REGIONS } from "@/types/lead";
import type { NicheConfig, OutreachRules } from "@/types/workspace";
import { pipelineStageLabel } from "@/lib/statusLabels";
import { ApiError, type LeadgenApi, type Scope } from "./types";

/** Latency band, so loading states are exercised rather than skipped past. */
const LATENCY: [number, number] = [180, 420];

function latency() {
  const [min, max] = LATENCY;
  return sleep(min + Math.random() * (max - min));
}

// --------------------------------------------------------------------------- //
// Mutable session state
// --------------------------------------------------------------------------- //

let leads: Lead[] = [];
let queue: LinkedInQueueItem[] = [];
let runs: AgentRun[] = [];
let activity: ActivityEvent[] = [];

/**
 * The user's real sending rules and audiences, once loaded.
 *
 * Match quality depends on the match bar, and the match bar is the user's own
 * setting held by the real service. Rather than keep a second copy that could
 * disagree with what Settings shows, the workspace provider hands the real one
 * over as soon as it has it. Until then the sending rules are the shipped
 * defaults and there are no audiences, because a workspace nobody has
 * configured has none.
 */
let rules: OutreachRules = { ...DEFAULT_RULES };
let niches: NicheConfig[] = [];

export function __setWorkspaceContext(next: {
  rules?: OutreachRules;
  niches?: NicheConfig[];
}) {
  if (next.rules) rules = next.rules;
  if (next.niches) niches = next.niches;
}

/** Reset hook, for tests. Back to empty, which is also where it started. */
export function __resetSessionState() {
  leads = [];
  queue = [];
  runs = [];
  activity = [];
  rules = { ...DEFAULT_RULES };
  niches = [];
}

// --------------------------------------------------------------------------- //
// Scoping
// --------------------------------------------------------------------------- //

/**
 * Every read filters by workspace first. The user never chooses it, but a
 * component that forgets to pass it gets an error rather than data, which is
 * the failure mode you want out of a namespacing bug.
 */
function scoped<T extends { tenant_id: string }>(items: T[], scope: Scope): T[] {
  if (!scope?.tenant_id) {
    throw new ApiError("Every read has to name its workspace", 400);
  }
  return items.filter((item) => item.tenant_id === scope.tenant_id);
}

function threshold(): number {
  return rules.fit_score_threshold;
}

function nicheLabelFor(nicheId: string): string {
  return niches.find((niche) => niche.id === nicheId)?.label ?? nicheId;
}

// --------------------------------------------------------------------------- //
// Leads
// --------------------------------------------------------------------------- //

function matchesFilters(lead: Lead, filters: LeadFilters): boolean {
  if (filters.search) {
    const needle = filters.search.toLowerCase();
    const haystack = [
      lead.company_name,
      lead.contact_name,
      lead.contact_email,
      lead.location,
      lead.industry,
      nicheLabelFor(lead.niche_id),
      ...lead.signals,
    ]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  if (filters.region && filters.region !== "all" && lead.region !== filters.region) return false;
  if (filters.niche_id && filters.niche_id !== "all" && lead.niche_id !== filters.niche_id) return false;
  if (filters.channel && filters.channel !== "all" && lead.channel !== filters.channel) return false;
  if (filters.approval_status && filters.approval_status !== "all" && lead.approval_status !== filters.approval_status) return false;
  if (filters.send_status && filters.send_status !== "all" && lead.send_status !== filters.send_status) return false;
  if (filters.suppression_status && filters.suppression_status !== "all" && lead.suppression_status !== filters.suppression_status) return false;
  if (typeof filters.min_fit_score === "number" && lead.fit_score < filters.min_fit_score) return false;
  if (typeof filters.archived === "boolean" && lead.archived !== filters.archived) return false;
  if (filters.stage && filters.stage !== "all" && !reachedStage(lead, filters.stage, threshold())) return false;
  return true;
}

function sortLeads(items: Lead[], filters: LeadFilters): Lead[] {
  const key = filters.sort_by ?? "fit_score";
  const dir = filters.sort_dir ?? "desc";
  return [...items].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    let cmp = 0;
    if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
    else cmp = String(av ?? "").localeCompare(String(bv ?? ""));
    return dir === "asc" ? cmp : -cmp;
  });
}

// --------------------------------------------------------------------------- //
// Messages waiting for review
// --------------------------------------------------------------------------- //

function buildApprovals(scope: Scope): ApprovalItem[] {
  return scoped(leads, scope)
    .filter(isAwaitingReview)
    .sort((a, b) => a.channel.localeCompare(b.channel) || b.fit_score - a.fit_score)
    .map((lead) => ({
      lead_id: lead.lead_id,
      tenant_id: lead.tenant_id,
      company_name: lead.company_name,
      contact_name: lead.contact_name,
      contact_email: lead.contact_email,
      linkedin_url: lead.linkedin_url,
      region: lead.region,
      niche_id: lead.niche_id,
      language: lead.language,
      channel: lead.channel,
      fit_score: lead.fit_score,
      fit_reason: lead.fit_reason,
      signals: lead.signals,
      sequence_step: lead.sequence_step + 1,
      signal_referenced: lead.draft_message.signal_referenced ?? "",
      translated: !!lead.draft_message.translated,
      email: lead.draft_message.email ?? {},
      linkedin: lead.draft_message.linkedin ?? {},
      dry_run: lead.dry_run,
      queued_at: lead.last_updated,
    }));
}

// --------------------------------------------------------------------------- //
// Follow-ups
// --------------------------------------------------------------------------- //

/**
 * How many times a region allows contacting someone who has not replied.
 * These come from the backend's regional rules; the effective ceiling is the
 * shorter of this and the user's own follow-up plan, which is why the UI needs
 * both to show "2 of 3" correctly.
 */
const REGION_TOUCH_CAP: Record<Region, number> = {
  US: 4,
  UK: 3,
  EU: 2,
  CA: 3,
  AU: 3,
  ME: 2,
};

function buildFollowUps(scope: Scope): FollowUp[] {
  const plan = rules.follow_ups;
  return scoped(leads, scope)
    .filter((lead) => !!lead.next_touch_due && !lead.archived)
    .sort((a, b) => (a.next_touch_due ?? "").localeCompare(b.next_touch_due ?? ""))
    .map((lead) => {
      const maxTouches = Math.min(plan.length, REGION_TOUCH_CAP[lead.region] ?? plan.length);
      const nextNumber = lead.sequence_step + 1;
      const nextStep = plan.find((s) => s.step === nextNumber) ?? plan[plan.length - 1];
      const blocked = lead.next_action.startsWith("holding:")
        ? "Waiting for them to accept your LinkedIn request"
        : "";

      return {
        lead_id: lead.lead_id,
        tenant_id: lead.tenant_id,
        company_name: lead.company_name,
        contact_name: lead.contact_name,
        region: lead.region,
        channel: lead.channel,
        sequence_step: lead.sequence_step,
        next_touch_number: nextNumber,
        next_step_name: nextStep?.name ?? "",
        next_step_channel: nextStep?.channel ?? lead.channel,
        next_touch_due: lead.next_touch_due!,
        max_touches: maxTouches,
        blocked_reason: blocked,
        steps: plan.slice(0, maxTouches).map((step) => ({
          step: step.step,
          name: step.name,
          channel: step.channel,
          status:
            step.step <= lead.sequence_step
              ? ("done" as const)
              : step.step === nextNumber
                ? blocked
                  ? ("blocked" as const)
                  : ("due" as const)
                : ("scheduled" as const),
          at: step.step <= lead.sequence_step ? lead.last_touch_at : null,
        })),
      };
    });
}

// --------------------------------------------------------------------------- //
// The numbers on Home
// --------------------------------------------------------------------------- //

function buildOverview(scope: Scope): OverviewMetrics {
  const workspaceLeads = scoped(leads, scope);
  const qualified = workspaceLeads.filter((l) => reachedStage(l, "qualified", threshold()));
  const contacted = workspaceLeads.filter((l) => reachedStage(l, "contacted", threshold()));
  const replies = workspaceLeads.filter(hasReplied);
  const followUps = buildFollowUps(scope);

  return {
    new_leads: workspaceLeads.length,
    qualified: qualified.length,
    contacted: contacted.length,
    replies: replies.length,
    booked: workspaceLeads.filter((l) => l.reply_category === "interested").length,
    reply_rate: pct(replies.length, contacted.length),
    pending_approval: buildApprovals(scope).length,
    linkedin_waiting: scoped(queue, scope).filter((item) => item.status === "pending").length,
    followups_due: followUps.filter((f) => new Date(f.next_touch_due) <= new Date()).length,
    // Change against the previous run of the same searches. Zero until there
    // are two runs to compare -- and a card hides a zero rather than printing
    // a movement that did not happen.
    trends: { new_leads: 0, qualified: 0, contacted: 0, replies: 0 },
    pipeline: PIPELINE_STAGES.map((stage: PipelineStage) => ({
      stage,
      label: pipelineStageLabel(stage),
      count: workspaceLeads.filter((lead) => reachedStage(lead, stage, threshold())).length,
    })),
  };
}

// --------------------------------------------------------------------------- //
// Performance
// --------------------------------------------------------------------------- //

/**
 * The Performance table: one row per (place, audience, channel) that has had
 * something sent to it.
 *
 * Rows with nothing sent are left out rather than listed as zeroes -- an
 * audience nobody has contacted has no performance to report, and a table of
 * empty rows buries the two rows that do.
 */
function buildSegments(workspaceLeads: Lead[]): SegmentRow[] {
  const contacted = workspaceLeads.filter(isContacted);
  const groups = groupBy(
    contacted,
    (lead) => `${lead.region}|${lead.niche_id}|${lead.channel}`,
  );

  return Object.entries(groups)
    .map(([key, group]) => {
      const replies = group.filter(hasReplied);
      const [region, nicheId, channel] = key.split("|");
      return {
        key,
        region: region as Region,
        niche_id: nicheId,
        niche_label: nicheLabelFor(nicheId),
        channel: channel as Channel,
        sent: group.length,
        replies: replies.length,
        reply_rate: pct(replies.length, group.length),
        booked: group.filter((l) => l.reply_category === "interested").length,
      };
    })
    .sort((a, b) => b.sent - a.sent);
}

function buildAnalytics(scope: Scope): AnalyticsBundle {
  const workspaceLeads = scoped(leads, scope);
  const contacted = workspaceLeads.filter((l) => reachedStage(l, "contacted", threshold()));
  const qualified = workspaceLeads.filter((l) => reachedStage(l, "qualified", threshold()));

  // Fourteen days of history, every point measured. There used to be a
  // synthetic ramp behind the earlier days so the chart had a shape; a line
  // nobody's work produced is worse than a flat one.
  const days = 14;
  const timeseries = Array.from({ length: days }, (_, index) => {
    const day = new Date();
    day.setDate(day.getDate() - (days - 1 - index));
    const key = day.toISOString().slice(0, 10);
    const discovered = workspaceLeads.filter((l) => l.discovered_at.slice(0, 10) === key);
    return {
      date: key,
      leads: discovered.length,
      qualified: discovered.filter((l) => l.fit_score >= threshold()).length,
      contacted: discovered.filter((l) => !!l.last_touch_at).length,
      replies: discovered.filter(hasReplied).length,
    };
  });

  const byRegion = REGIONS.map((region) => {
    const group = workspaceLeads.filter((l) => l.region === region);
    return {
      key: region,
      label: region,
      value: group.length,
      secondary: group.filter((l) => l.fit_score >= threshold()).length,
    };
  }).filter((point) => point.value > 0);

  const byNiche = niches
    .map((niche) => {
      const group = workspaceLeads.filter((l) => l.niche_id === niche.id);
      return {
        key: niche.id,
        label: niche.label,
        value: group.length,
        secondary: group.filter((l) => l.fit_score >= threshold()).length,
      };
    })
    .filter((point) => point.value > 0);

  const channelPerformance = (["email", "linkedin", "both"] as Channel[]).map((channel) => {
    const group = contacted.filter((l) => l.channel === channel);
    const groupReplies = group.filter(hasReplied);
    return {
      channel,
      sent: group.length,
      replies: groupReplies.length,
      interested: group.filter((l) => l.reply_category === "interested").length,
      reply_rate: pct(groupReplies.length, group.length),
    };
  });

  const buckets = ["0-19", "20-39", "40-59", "60-79", "80-100"];
  const fitDistribution = buckets.map((bucket) => {
    const [low, high] = bucket.split("-").map(Number);
    const count = workspaceLeads.filter((l) => l.fit_score >= low && l.fit_score <= high).length;
    return { bucket, count, below_threshold: high < threshold() };
  });

  return {
    timeseries,
    by_segment: buildSegments(workspaceLeads),
    by_region: byRegion,
    by_niche: byNiche,
    channel_performance: channelPerformance,
    fit_distribution: fitDistribution,
    qualification_rate: pct(qualified.length, workspaceLeads.length),
    reply_rate: pct(workspaceLeads.filter(hasReplied).length, contacted.length),
    fit_score_threshold: threshold(),
  };
}

// --------------------------------------------------------------------------- //
// Reading a plain-language request
// --------------------------------------------------------------------------- //

/**
 * Words that name nobody in particular.
 *
 * A request built only from these has issued an instruction without ever
 * saying who it is about: "find leads" is not a description of anyone.
 */
const GENERIC_SUBJECTS = new Set([
  "leads", "lead", "businesses", "business", "companies", "company",
  "prospects", "prospect", "people", "clients", "customers", "contacts",
  "someone", "anyone", "everyone", "some", "more", "new",
]);

/** A request usually opens with an instruction. The audience is what follows it. */
const OPENING_INSTRUCTION =
  /^\s*(?:please\s+)?(?:can\s+you\s+)?(?:go\s+)?(?:and\s+)?(?:find|get|search(?:\s+for)?|look(?:\s+for)?|show(?:\s+me)?|give(?:\s+me)?|list|fetch|pull(?:\s+up)?)\s+(?:me\s+)?(?:some\s+|any\s+|a\s+|the\s+)?/i;

/**
 * Who the request is about, in the user's own words.
 *
 * This returns "" only when the text genuinely names nobody -- an empty box,
 * or an instruction with no subject. Anything else counts, because somebody
 * who typed a sentence has said who they mean, whether or not their wording
 * happens to match an audience the workspace has configured.
 *
 * Matched against the original text rather than the lowercased copy: this is
 * the user's own phrasing, kept intact so it can be shown back to them.
 */
function describeAudience(prompt: string): string {
  // "I sell websites and I'm looking for dentists" -- the audience is the part
  // after the verb, not the whole sentence.
  const stated =
    /(?:looking for|want to reach|interested in|reach out to|target|contact)\s+([^.,;]{3,80})/i.exec(
      prompt,
    );
  const candidate = (stated ? stated[1] : prompt.replace(OPENING_INSTRUCTION, ""))
    .trim()
    .replace(/[.,;!?\s]+$/, "");
  if (!candidate) return "";
  const words = candidate.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.every((word) => GENERIC_SUBJECTS.has(word))) return "";
  return candidate.slice(0, 80);
}

/**
 * Turn what somebody typed into a plan.
 *
 * Keyword matching, deliberately: this is the sample implementation, and the
 * real one is a model call using the user's own AI key. Two properties matter
 * more than cleverness, and both are preserved here:
 *
 *   - It matches against the user's OWN configured audiences and regions, so
 *     it can only produce a plan the backend would accept. There is no
 *     built-in list of industries anywhere in it.
 *   - It never runs anything. The plan comes back for confirmation first.
 */
function readRequest(prompt: string): RunConfig {
  const text = prompt.toLowerCase();

  // An audience matches on any word from its own label, search terms or job
  // titles. That works for "cleaning companies" or "machine shops" exactly as
  // well as for anything else, because the vocabulary is the user's.
  const matchedNiches = niches.filter((niche) => {
    const vocabulary = [
      niche.label,
      niche.id.replace(/_/g, " "),
      ...niche.search_terms,
      ...niche.titles,
    ]
      .join(" ")
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((word) => word.length > 3);
    return vocabulary.some((word) => text.includes(word));
  });

  // Places come from the audiences' own configured locations plus the region
  // names, so a request naming a city the user actually targets is recognised.
  const knownPlaces = new Set<string>();
  for (const niche of niches) {
    for (const list of Object.values(niche.locations)) {
      for (const place of list ?? []) knownPlaces.add(place.toLowerCase());
    }
  }
  const locations = [...knownPlaces].filter((place) => {
    const bare = place.replace(/\s+[A-Z]{2}$/i, "").trim();
    return text.includes(bare);
  });

  const regionHints: Record<Region, string[]> = {
    US: ["us", "usa", "united states", "america", "american"],
    UK: ["uk", "united kingdom", "britain", "british", "england"],
    EU: ["eu", "europe", "european"],
    CA: ["canada", "canadian"],
    AU: ["australia", "australian"],
    ME: ["middle east", "gulf", "uae", "saudi"],
  };
  const regions = (Object.keys(regionHints) as Region[]).filter((region) => {
    if (!rules.regions.includes(region)) return false;
    if (regionHints[region].some((hint) => new RegExp(`\\b${hint}\\b`).test(text))) return true;
    // A named city implies its region, without needing the region spelled out.
    return niches.some((niche) =>
      (niche.locations[region] ?? []).some((place) =>
        text.includes(place.replace(/\s+[A-Z]{2}$/i, "").trim().toLowerCase()),
      ),
    );
  });

  const channels: Channel[] = [];
  if (/\bemail(s|ed|ing)?\b/.test(text)) channels.push("email");
  if (/linkedin/.test(text)) channels.push("linkedin");

  const resolvedNiches = matchedNiches.length ? matchedNiches : niches;
  const resolvedRegions = regions.length ? regions : rules.regions;

  // Signals: anything the user spelled out, else the audience's own list.
  const spelledOut = resolvedNiches
    .flatMap((niche) => niche.good_signals)
    .filter((signal) => {
      const words = signal.toLowerCase().split(/\s+/).filter((w) => w.length > 4);
      return words.length > 0 && words.filter((w) => text.includes(w)).length >= 1;
    });
  const signals = spelledOut.length
    ? [...new Set(spelledOut)].slice(0, 5)
    : resolvedNiches.flatMap((niche) => niche.good_signals).slice(0, 4);

  const scoreMatch = /(?:match|fit|score)\D{0,12}(\d{1,3})/.exec(text);
  const countMatch = /(\d{1,3})\s*(?:leads|businesses|companies|prospects|firms)/.exec(text);
  const paceMatch = /(?:every|after)\s*(\d{1,2})\s*days?/.exec(text);

  // What they sell and who they want: taken from the sentence itself, because
  // echoing the user's own words back is the point of the confirmation step.
  const sellMatch =
    /(?:i|we)\s+(?:sell|offer|provide|do|build|run)\s+([^.,;]{3,80})/i.exec(prompt);

  return {
    offering: sellMatch ? sellMatch[1].trim() : "",
    audience: describeAudience(prompt),
    niche_ids: resolvedNiches.map((niche) => niche.id),
    regions: resolvedRegions,
    locations,
    signals,
    channels: channels.length ? channels : rules.channels.enabled,
    language_map: Object.fromEntries(
      resolvedRegions.map((region) => [region, rules.language_map[region] ?? "en"]),
    ),
    fit_score_threshold: scoreMatch
      ? Math.min(100, Number(scoreMatch[1]))
      : rules.fit_score_threshold,
    max_leads_per_run: countMatch
      ? Math.min(200, Number(countMatch[1]))
      : rules.max_leads_per_run,
    follow_up_pace_days: paceMatch ? Math.min(30, Number(paceMatch[1])) : 3,
    // Test Mode is the default for a fresh plan. Going live is an explicit
    // act, never an inherited default.
    dry_run: true,
  };
}

// --------------------------------------------------------------------------- //
// The Home feed
// --------------------------------------------------------------------------- //

function buildFeed(scope: Scope, limit: number): AgentFeedItem[] {
  return scoped(activity, scope)
    .slice(0, limit)
    .map((event) => ({
      id: event.id,
      stage: event.stage,
      headline: headlineFor(event),
      detail: event.message,
      status:
        event.level === "error" ? "danger" : event.level === "warning" ? "warning" : "success",
      at: event.at,
      lead_id: event.lead_id,
    }));
}

/** A short headline; the fuller sentence becomes the supporting line. */
function headlineFor(event: ActivityEvent): string {
  const who = event.company_name ?? "";
  switch (event.stage) {
    case "understanding":
      return "Your request was read";
    case "finding":
      return who ? `Skipped ${who}` : "Finished searching";
    case "researching":
      return who ? `Researched ${who}` : "Finished researching";
    case "matching":
      return who ? `Scored ${who}` : "Checking matches";
    case "choosing_channel":
      return who ? `Picked how to reach ${who}` : "Picked the best channels";
    case "drafting":
      return who ? `Wrote to ${who}` : "Finished writing";
    case "reviewing":
      return who ? `Review recorded for ${who}` : "Reviews recorded";
    case "checking_rules":
      return who ? `${who} passed the rule check` : "Rule checks finished";
    case "sending_email":
      return who ? `Email step for ${who}` : "Emails finished";
    case "linkedin_prep":
      return who ? `LinkedIn message ready for ${who}` : "LinkedIn list updated";
    case "watching_replies":
      return who ? `Checked for a reply from ${who}` : "Reply check finished";
    case "following_up":
      return who ? `Follow-up planned for ${who}` : "Follow-ups planned";
    case "saving":
      return "Results saved";
    default:
      return stageShort(event.stage);
  }
}

// --------------------------------------------------------------------------- //
// The implementation
// --------------------------------------------------------------------------- //

export const localApi: LeadgenApi = {
  async getLeads(filters) {
    await latency();
    const workspaceLeads = scoped(leads, filters);
    return sortLeads(
      workspaceLeads.filter((lead) => matchesFilters(lead, filters)),
      filters,
    ).map((lead) => ({ ...lead }));
  },

  async getLead(leadId, scope) {
    await latency();
    const lead = scoped(leads, scope).find((item) => item.lead_id === leadId);
    return lead ? { ...lead } : null;
  },

  async getAgentRuns(scope) {
    await latency();
    return scoped(runs, scope)
      .slice()
      .sort((a, b) => b.started_at.localeCompare(a.started_at))
      .map((run) => ({ ...run }));
  },

  async getAgentRun(runId, scope) {
    await latency();
    const run = scoped(runs, scope).find((item) => item.run_id === runId);
    return run ? { ...run } : null;
  },

  async parseRequest(prompt, scope) {
    if (!scope?.tenant_id) throw new ApiError("Every read has to name its workspace", 400);
    // Slightly longer than a normal read: this is a model call in the real
    // implementation, and the waiting state should be seen rather than flashed.
    await sleep(700);
    if (!prompt.trim()) {
      throw new ApiError("Tell us what you sell and who you want to reach first.", 422);
    }
    return readRequest(prompt);
  },

  async startRun(config, prompt, scope) {
    await sleep(500);
    if (!scope?.tenant_id) throw new ApiError("Every read has to name its workspace", 400);
    // Who to look for comes from the request itself.
    //
    // This used to require `niche_ids` -- audiences the workspace had been
    // configured with -- and rejected every plain-language request that did
    // not happen to match one, telling the user to "pick at least one
    // audience" from a list this product has no screen for. It also made the
    // outcome depend on whether that list had finished loading, so the same
    // sentence submitted or failed depending on how fast the user typed.
    //
    // The words of the request decide it now, and configured audiences only
    // narrow what the run searches.
    if (!config.audience.trim()) {
      throw new ApiError("Tell us who you're looking for.", 422, "Nothing was started.");
    }
    if (!config.regions.length) {
      throw new ApiError("Pick at least one place to search in.", 422, "Nothing was started.");
    }

    // With no configured audience matched, the request's own description is
    // what the run is looking for. `nicheLabelFor` falls back to the id it is
    // given, so this reads as the user's own phrase wherever it is shown.
    const targetNicheIds = config.niche_ids.length
      ? config.niche_ids
      : [config.audience.trim()];

    const stages: StageProgress[] = RUN_STAGES.map((stage) => ({
      stage,
      status: stage === "understanding" ? "running" : "waiting",
      duration_ms: null,
      leads_in: 0,
      leads_out: 0,
      leads_diverted: 0,
      started_at: stage === "understanding" ? new Date().toISOString() : null,
      finished_at: null,
      note: stage === "understanding" ? "Working out where to search" : "",
    }));

    const run: AgentRun = {
      run_id: Math.random().toString(16).slice(2, 14),
      tenant_id: scope.tenant_id,
      status: "running",
      dry_run: config.dry_run,
      started_at: new Date().toISOString(),
      finished_at: null,
      targets: targetNicheIds.flatMap((niche_id) =>
        config.regions.map((region) => ({
          niche_id,
          region,
          language: config.language_map[region] ?? "en",
        })),
      ),
      low_yield_targets: [],
      leads_discovered: 0,
      leads_qualified: 0,
      lead_ids: [],
      needs_manual_review: [],
      archived: [],
      stages,
      prompt,
      config,
    };
    runs = [run, ...runs];
    return { ...run };
  },

  async getApprovals(scope) {
    await latency();
    return buildApprovals(scope);
  },

  async submitApproval(leadId, decision, scope) {
    await sleep(220);
    const lead = scoped(leads, scope).find((item) => item.lead_id === leadId);
    if (!lead) throw new ApiError("That business is not in this workspace.", 404);

    let status: ApprovalStatus;
    if (decision.action === "reject") {
      status = "rejected";
      lead.archived = true;
      lead.archive_reason = "rejected";
      lead.next_action = "You rejected this message, so nothing was sent";
    } else {
      status = decision.action === "edit" ? "edited" : "approved";
      if (decision.action === "edit") {
        lead.draft_message = {
          ...lead.draft_message,
          email: { ...lead.draft_message.email, ...decision.email },
          linkedin: { ...lead.draft_message.linkedin, ...decision.linkedin },
          edited_by_human: true,
        };
      }
      lead.next_action =
        lead.channel === "linkedin"
          ? "Approved - waiting for you to send it on LinkedIn"
          : "Approved - ready to go out";
    }
    lead.approval_status = status;
    lead.last_updated = new Date().toISOString();
    return { lead_id: leadId, approval_status: status };
  },

  async getLinkedInQueue(scope) {
    await latency();
    return scoped(queue, scope)
      .slice()
      .sort((a, b) => {
        const order = { pending: 0, connected: 1, sent: 2, skipped: 3 };
        return order[a.status] - order[b.status] || b.fit_score - a.fit_score;
      })
      .map((item) => ({ ...item }));
  },

  async markLinkedInItem(leadId, status, scope) {
    await sleep(200);
    const item = scoped(queue, scope).find((entry) => entry.lead_id === leadId);
    if (!item) return null;
    item.status = status;
    const now = new Date().toISOString();
    if (status === "sent") item.sent_at = now;
    if (status === "connected") item.connected_at = now;

    // Keep the lead consistent with the list, the way the backend does when
    // the same thing is recorded from the command line.
    const lead = leads.find((l) => l.lead_id === leadId);
    if (lead && status === "sent") {
      lead.send_status = "pending_manual_send";
      lead.last_touch_at = now;
    }
    return { ...item };
  },

  async getFollowUps(scope) {
    await latency();
    return buildFollowUps(scope);
  },

  async getActivities(filters) {
    await latency();
    let items = scoped(activity, filters);
    if (filters.stage && filters.stage !== "all") {
      items = items.filter((event) => event.stage === filters.stage);
    }
    if (filters.run_id && filters.run_id !== "all") {
      items = items.filter((event) => event.run_id === filters.run_id);
    }
    if (filters.lead_id) {
      items = items.filter((event) => event.lead_id === filters.lead_id);
    }
    if (filters.level && filters.level !== "all") {
      items = items.filter((event) => event.level === filters.level);
    }
    if (filters.search) {
      const needle = filters.search.toLowerCase();
      items = items.filter((event) =>
        `${event.message} ${event.company_name ?? ""} ${stageShort(event.stage)}`
          .toLowerCase()
          .includes(needle),
      );
    }
    const sorted = [...items].sort((a, b) => b.at.localeCompare(a.at));
    return (filters.limit ? sorted.slice(0, filters.limit) : sorted).map((event) => ({
      ...event,
    }));
  },

  async getAgentFeed(scope, limit = 8) {
    await latency();
    return buildFeed(scope, limit);
  },

  async getOverviewMetrics(scope) {
    await latency();
    return buildOverview(scope);
  },

  async getAnalytics(scope) {
    await latency();
    return buildAnalytics(scope);
  },

  /**
   * Free-tier usage.
   *
   * Empty, and honestly so: the counters that hold this are durable and live
   * in the agent (`src.counters`), and nothing in this client can read them
   * yet. The invented "2 of 25 used" numbers were the one piece of sample data
   * a user could act on -- deciding not to run a search because a made-up bar
   * looked nearly full. The screen already says these appear once a mailbox
   * has been connected and used, which is true.
   */
  async getQuotas(scope) {
    await latency();
    if (!scope?.tenant_id) throw new ApiError("Every read has to name its workspace", 400);
    return [] as QuotaState[];
  },
};

export type { RunStage };
