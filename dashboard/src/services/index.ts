/**
 * index.ts -- what the app imports.
 *
 * Components import `api` and `workspace` from here and never from a concrete
 * implementation, so there is exactly one place where the wiring is decided.
 */

import { httpApi } from "./api";
import { localApi } from "./localApi";
import { workspaceApi } from "./workspaceApi";
import type { LeadgenApi, WorkspaceApi } from "./types";

/**
 * Lead and campaign data is held in the browser for the session, and starts
 * empty. There is no sample data behind it: what the screens show is what has
 * actually happened in this session, which is nothing until a run produces
 * something.
 *
 * Flip this to false and set VITE_API_BASE_URL to serve it from the HTTP
 * routes in api.ts instead, once the backend has them. Both sides implement
 * the same interface, so the compiler checks the swap for you.
 */
const USE_IN_SESSION_LEAD_DATA = true;

export const api: LeadgenApi = USE_IN_SESSION_LEAD_DATA ? localApi : httpApi;

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
