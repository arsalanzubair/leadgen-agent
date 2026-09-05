/**
 * types.ts -- the service contracts, declared once.
 *
 * There are two, and the split is deliberate:
 *
 *   LeadgenApi    lead and campaign data. Served by `localApi.ts` today,
 *                 which holds it in the browser for the session and starts
 *                 empty; `api.ts` is the real HTTP implementation, fully
 *                 typed, and documents which existing backend function each
 *                 route wraps. Swapping them is one line in index.ts.
 *
 *   WorkspaceApi  the user's own configuration -- their business profile,
 *                 their API keys, their audiences, their rules for Leo. This
 *                 one is REAL, always, and only ever talks to the FastAPI app
 *                 in backend/. There is no mock: a key that appears to save
 *                 but does not is worse than an error, and a rule that
 *                 vanishes on reload is not a saved rule.
 *
 * Both implementations of `LeadgenApi` are checked against this file by the
 * compiler, so a signature cannot drift without breaking the build.
 */

import type { ApprovalStatus, Lead, LeadFilters } from "@/types/lead";
import type {
  ActivityEvent,
  AgentFeedItem,
  AgentRun,
  ApprovalItem,
  FollowUp,
  LinkedInQueueItem,
  LinkedInQueueStatus,
  OverviewMetrics,
  QuotaState,
  RunConfig,
} from "@/types/agent";
import type { AnalyticsBundle } from "@/types/campaign";
import type {
  AgentRule,
  BusinessProfile,
  Capability,
  CapabilityGroup,
  Connection,
  ConnectionTestResult,
  NicheConfig,
  NicheDraft,
  SelectionMap,
  OutreachRules,
} from "@/types/workspace";

/**
 * Every read is namespaced.
 *
 * The user never sees or chooses this -- one deployment is one workspace -- but
 * the backend namespaces every file, counter and suppression list by it, and
 * pretending otherwise in the client would mean inventing the value somewhere
 * less visible. `hooks/useWorkspace` supplies it; components pass it through.
 */
export interface Scope {
  tenant_id: string;
}

export interface ActivityFilters extends Scope {
  stage?: string | "all";
  run_id?: string | "all";
  lead_id?: string;
  level?: "info" | "warning" | "error" | "all";
  search?: string;
  since?: string;
  limit?: number;
}

export interface ApprovalDecision {
  action: "approve" | "edit" | "reject";
  email?: { subject?: string; body?: string };
  linkedin?: { connection_note?: string; followup_dm?: string };
}

// --------------------------------------------------------------------------- //
// Lead and campaign data
// --------------------------------------------------------------------------- //

export interface LeadgenApi {
  getLeads(filters: LeadFilters & Scope): Promise<Lead[]>;
  getLead(leadId: string, scope: Scope): Promise<Lead | null>;

  getAgentRuns(scope: Scope): Promise<AgentRun[]>;
  getAgentRun(runId: string, scope: Scope): Promise<AgentRun | null>;
  /**
   * Read a plain-language request into a structured, editable plan.
   * Nothing runs as a result of this -- the plan is shown for confirmation
   * first, always.
   */
  parseRequest(prompt: string, scope: Scope): Promise<RunConfig>;
  startRun(config: RunConfig, prompt: string, scope: Scope): Promise<AgentRun>;

  getApprovals(scope: Scope): Promise<ApprovalItem[]>;
  submitApproval(
    leadId: string,
    decision: ApprovalDecision,
    scope: Scope,
  ): Promise<{ lead_id: string; approval_status: ApprovalStatus }>;

  getLinkedInQueue(scope: Scope): Promise<LinkedInQueueItem[]>;
  /**
   * Record what the user did on LinkedIn. There is deliberately no "send"
   * verb: the person sends the message in LinkedIn, then tells the product
   * they did. Nothing here ever touches a LinkedIn account.
   */
  markLinkedInItem(
    leadId: string,
    status: LinkedInQueueStatus,
    scope: Scope,
  ): Promise<LinkedInQueueItem | null>;

  getFollowUps(scope: Scope): Promise<FollowUp[]>;

  getActivities(filters: ActivityFilters): Promise<ActivityEvent[]>;
  getAgentFeed(scope: Scope, limit?: number): Promise<AgentFeedItem[]>;

  getOverviewMetrics(scope: Scope): Promise<OverviewMetrics>;
  getAnalytics(scope: Scope): Promise<AnalyticsBundle>;
  getQuotas(scope: Scope): Promise<QuotaState[]>;
}

// --------------------------------------------------------------------------- //
// The user's own configuration
// --------------------------------------------------------------------------- //

export interface WorkspaceApi {
  // -- identity ------------------------------------------------------------ //
  getProfile(): Promise<BusinessProfile>;
  saveProfile(patch: Partial<BusinessProfile>): Promise<BusinessProfile>;

  // -- keys ---------------------------------------------------------------- //
  /** Status only. A saved key is never returned, in any form. */
  getConnections(): Promise<Connection[]>;
  /**
   * Make one real, lightweight call to the provider with the submitted values
   * and report back. Nothing is saved by this -- testing before saving is the
   * whole point.
   */
  testConnection(
    providerId: string,
    values: Record<string, string>,
  ): Promise<ConnectionTestResult>;
  /** Saves encrypted, and only after a test has passed. */
  saveConnection(
    providerId: string,
    values: Record<string, string>,
  ): Promise<Connection>;
  deleteConnection(providerId: string): Promise<Connection>;

  // -- who does each job --------------------------------------------------- //
  /**
   * Everything that can do each job, grouped by job, with the heading and
   * description for each.
   *
   * Served from the backend's provider registry -- the same list the agent
   * resolves from -- so this client holds no vendor names of its own.
   */
  getCapabilities(): Promise<CapabilityGroup[]>;
  /** What this workspace has chosen, and whether each choice can run. */
  getSelection(): Promise<SelectionMap>;
  /**
   * Point one job at a provider. `settings` carries a custom endpoint's URL
   * and auth style; a key in there is refused by the server, because a
   * credential belongs in the encrypted store and not in a config file.
   */
  setSelection(
    capability: Capability,
    choice: { primary: string; fallback?: string; settings?: Record<string, string> },
  ): Promise<SelectionMap>;

  // -- audiences ----------------------------------------------------------- //
  getNiches(): Promise<NicheConfig[]>;
  createNiche(niche: NicheConfig): Promise<NicheConfig>;
  updateNiche(id: string, niche: NicheConfig): Promise<NicheConfig>;
  deleteNiche(id: string): Promise<{ id: string }>;
  /**
   * Draft an audience from a description, using the user's own AI key. Returns
   * a draft for review -- same confirm-before-commit shape as Find Leads.
   */
  draftNiche(description: string): Promise<NicheDraft>;

  // -- rules for Leo ------------------------------------------------------- //
  getAgentRules(): Promise<AgentRule[]>;
  addAgentRule(rule: { kind: "always" | "never"; text: string }): Promise<AgentRule>;
  deleteAgentRule(id: string): Promise<{ id: string }>;

  // -- sending rules ------------------------------------------------------- //
  getRules(): Promise<OutreachRules>;
  saveRules(patch: Partial<OutreachRules>): Promise<OutreachRules>;

  /** Which workspace this deployment is. Shown only in Technical details. */
  getWorkspaceId(): Promise<{ tenant_id: string }>;
}

// --------------------------------------------------------------------------- //
// Errors
// --------------------------------------------------------------------------- //

/** Thrown by every implementation, so error UI is written once. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** What to do about it, in one sentence. Shown in the error state. */
    readonly remedy?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * The settings service is not running.
 *
 * Its own error type because the remedy is completely different from every
 * other failure -- there is nothing wrong with the request, the process just
 * is not up -- and the UI says so instead of showing a generic red box.
 */
export class ServiceOfflineError extends ApiError {
  constructor(readonly endpoint: string) {
    super(
      "The settings service is not running, so your configuration cannot be loaded or saved.",
      503,
      "Start it from the project root: python -m uvicorn backend.main:app --port 8000",
    );
    this.name = "ServiceOfflineError";
  }
}
