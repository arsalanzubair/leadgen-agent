/**
 * liveApi.ts -- the parts of the product that have a real backend, wired to it.
 *
 * The settings service now runs searches: it reads a plain-language request,
 * starts the graph on a worker thread, and reports how far it has got. So
 * runs, and the leads they produce, come from HTTP.
 *
 * Everything else -- approving a draft, marking a LinkedIn message sent,
 * follow-up timing, the activity feed, the metrics -- is still derived in the
 * browser by `localApi`, because the service has no routes for those yet.
 * Rather than keep two disconnected worlds, the real leads are handed to
 * `localApi` on every read, so those derived screens are computed from
 * genuine run results instead of from nothing.
 *
 * The seam is deliberately visible. When an approvals route exists, its method
 * moves from the spread below to an `http` call and nothing else changes.
 */

import { http } from "./http";
import { __ingest, localApi } from "./localApi";
import type { ActivityFilters, LeadgenApi, Scope } from "./types";
import type { AgentRun } from "@/types/agent";
import type { Lead, LeadFilters } from "@/types/lead";

/**
 * Pull the real leads and runs in before a derived read.
 *
 * Deduplicated over a short window: one screen can easily ask for leads,
 * approvals and metrics in the same tick, and that should be one request each
 * rather than three of the same.
 */
let inFlight: Promise<void> | null = null;
let lastAt = 0;
const CACHE_MS = 1000;

async function sync(): Promise<void> {
  if (inFlight) return inFlight;
  if (Date.now() - lastAt < CACHE_MS) return;
  inFlight = (async () => {
    try {
      const [leads, runs] = await Promise.all([
        http.get<Lead[]>("/leads"),
        http.get<AgentRun[]>("/runs"),
      ]);
      __ingest({ leads, runs });
      lastAt = Date.now();
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

/** Wrap a derived read so it sees real data. */
function derived<A extends unknown[], R>(read: (...args: A) => Promise<R>) {
  return async (...args: A): Promise<R> => {
    await sync();
    return read(...args);
  };
}

export const liveApi: LeadgenApi = {
  ...localApi,

  // -- real routes --------------------------------------------------------- //

  parseRequest: (prompt: string, scope: Scope) =>
    http.post("/runs/parse", { prompt, ...scope }),

  startRun: (config, prompt, scope) =>
    http.post("/runs", {
      config,
      prompt,
      // Carried separately from the plan: the service saves the audience it
      // drafted only if the run actually needs one that does not exist yet.
      niche_draft: config.niche_draft ?? null,
      ...scope,
    }),

  getAgentRuns: async (scope) => {
    await sync();
    return localApi.getAgentRuns(scope);
  },

  // Not synced: this is the polling call on a run in flight, and it must
  // report the service's own view without a cache in front of it.
  getAgentRun: (runId: string) => http.get<AgentRun | null>(`/runs/${runId}`),

  // -- derived in the browser, from real leads ----------------------------- //

  getLeads: derived((filters: LeadFilters & Scope) => localApi.getLeads(filters)),
  getLead: derived((leadId: string, scope: Scope) => localApi.getLead(leadId, scope)),
  getApprovals: derived((scope: Scope) => localApi.getApprovals(scope)),
  getLinkedInQueue: derived((scope: Scope) => localApi.getLinkedInQueue(scope)),
  getFollowUps: derived((scope: Scope) => localApi.getFollowUps(scope)),
  getActivities: derived((filters: ActivityFilters) => localApi.getActivities(filters)),
  getAgentFeed: derived((scope: Scope, limit?: number) =>
    localApi.getAgentFeed(scope, limit),
  ),
  getOverviewMetrics: derived((scope: Scope) => localApi.getOverviewMetrics(scope)),
  getAnalytics: derived((scope: Scope) => localApi.getAnalytics(scope)),
};
