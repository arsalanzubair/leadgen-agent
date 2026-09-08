/**
 * index.ts -- what the app imports.
 *
 * Components import `api` and `workspace` from here and never from a concrete
 * implementation, so there is exactly one place where the wiring is decided.
 */

import { liveApi } from "./liveApi";
import { workspaceApi } from "./workspaceApi";
import type { LeadgenApi, WorkspaceApi } from "./types";

/**
 * Searches are real: they run in the settings service, against the user's own
 * connections, and the leads on these screens are the ones those runs found.
 *
 * The screens that act on a lead -- approving a draft, marking a LinkedIn
 * message sent -- are still worked out in the browser, because the service has
 * no routes for them yet. `liveApi` documents exactly which is which, and
 * feeds the real leads to the derived side so nothing is computed from
 * nothing.
 */
export const api: LeadgenApi = liveApi;

/**
 * The user's own configuration is always real. There is no mock of this and
 * no switch to one -- see the note at the top of workspaceApi.ts.
 */
export const workspace: WorkspaceApi = workspaceApi;

export { ApiError, ServiceOfflineError } from "./types";
export type {
  ActivityFilters,
  ApprovalDecision,
  LeadgenApi,
  Scope,
  WorkspaceApi,
} from "./types";
export { __resetSessionState, __setWorkspaceContext } from "./localApi";
