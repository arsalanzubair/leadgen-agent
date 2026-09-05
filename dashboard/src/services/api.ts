/**
 * api.ts -- the real HTTP implementation of `LeadgenApi`.
 *
 * Not in use yet: `index.ts` serves lead data from `localApi.ts`. This file is
 * here, fully typed and compiled, so that switching over is one line rather
 * than a rewrite -- and so that the shape of the routes is decided now, while
 * the client's needs are fresh, rather than guessed at later.
 *
 * Each route notes which existing backend function it would wrap. The pieces
 * already exist as command-line entry points; what is missing is only the HTTP
 * surface in front of them.
 */

import { http } from "./http";
import type { ActivityFilters, ApprovalDecision, LeadgenApi, Scope } from "./types";
import type { Lead, LeadFilters } from "@/types/lead";
import type { RunConfig } from "@/types/agent";

export const httpApi: LeadgenApi = {
  // src.cli.run_batch + the CRM sheet the run writes
  getLeads: (filters: LeadFilters & Scope) => http.get<Lead[]>("/leads", filters),
  getLead: (leadId, scope) => http.get<Lead | null>(`/leads/${leadId}`, scope),

  getAgentRuns: (scope) => http.get("/runs", scope),
  getAgentRun: (runId, scope) => http.get(`/runs/${runId}`, scope),

  // a model call using the user's own AI key, same as /api/niches/draft
  parseRequest: (prompt: string, scope: Scope) =>
    http.post("/runs/parse", { prompt, ...scope }),

  // src.cli.run_batch.run_batch, started in the background
  startRun: (config: RunConfig, prompt: string, scope: Scope) =>
    http.post("/runs", { config, prompt, ...scope }),

  // src.cli.approve.pending_threads
  getApprovals: (scope) => http.get("/approvals", scope),
  // src.cli.approve -- resumes the paused run with the human's decision
  submitApproval: (leadId: string, decision: ApprovalDecision, scope: Scope) =>
    http.post(`/approvals/${leadId}`, { ...decision, ...scope }),

  // src.cli.linkedin_queue
  getLinkedInQueue: (scope) => http.get("/linkedin/queue", scope),
  markLinkedInItem: (leadId, status, scope) =>
    http.post(`/linkedin/queue/${leadId}`, { status, ...scope }),

  // derived from each lead's next_touch_due plus the follow-up plan
  getFollowUps: (scope) => http.get("/follow-ups", scope),

  getActivities: (filters: ActivityFilters) => http.get("/activity", filters),
  getAgentFeed: (scope, limit) => http.get("/activity/feed", { ...scope, limit }),

  getOverviewMetrics: (scope) => http.get("/metrics/overview", scope),
  getAnalytics: (scope) => http.get("/metrics/performance", scope),
  // src.counters -- the durable free-tier counters
  getQuotas: (scope) => http.get("/quotas", scope),
};
