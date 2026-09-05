/**
 * workspaceApi.ts -- the real implementation of `WorkspaceApi`.
 *
 * This one is never mocked. It talks to the FastAPI app in backend/, which
 * reads and writes the workspace's own configuration file and its encrypted
 * secrets store.
 *
 * The reason there is no fallback: a key that appears to save but has not is
 * worse than an error, and a rule for Leo that disappears on reload is not a
 * saved rule. When the service is not running, every call here raises
 * `ServiceOfflineError` and the screens say exactly that, with the command to
 * start it.
 *
 * On secrets specifically: values submitted to `testConnection` and
 * `saveConnection` travel to the local backend and are never held in this
 * client beyond the life of the form. Nothing here ever reads a key back --
 * `getConnections` returns status and a last-four fragment, and that is all
 * the server will give it.
 */

import { http } from "./http";
import type { WorkspaceApi } from "./types";
import type {
  AgentRule,
  BusinessProfile,
  CapabilityGroup,
  Connection,
  ConnectionTestResult,
  NicheConfig,
  NicheDraft,
  OutreachRules,
  SelectionMap,
} from "@/types/workspace";

export const workspaceApi: WorkspaceApi = {
  getProfile: () => http.get<BusinessProfile>("/profile"),
  saveProfile: (patch) => http.put<BusinessProfile>("/profile", patch),

  getConnections: () => http.get<Connection[]>("/integrations"),

  getCapabilities: () => http.get<CapabilityGroup[]>("/providers"),
  getSelection: () => http.get<SelectionMap>("/providers/selection"),
  setSelection: (capability, choice) =>
    http.put<SelectionMap>(`/providers/selection/${capability}`, {
      primary: choice.primary,
      fallback: choice.fallback ?? "",
      settings: choice.settings ?? {},
    }),

  testConnection: (providerId, values) =>
    http.post<ConnectionTestResult>(`/integrations/${providerId}/test`, { values }),
  saveConnection: (providerId, values) =>
    http.post<Connection>(`/integrations/${providerId}`, { values }),
  deleteConnection: (providerId) => http.del<Connection>(`/integrations/${providerId}`),

  getNiches: () => http.get<NicheConfig[]>("/niches"),
  createNiche: (niche) => http.post<NicheConfig>("/niches", niche),
  updateNiche: (id, niche) => http.put<NicheConfig>(`/niches/${id}`, niche),
  deleteNiche: (id) => http.del<{ id: string }>(`/niches/${id}`),
  draftNiche: (description) => http.post<NicheDraft>("/niches/draft", { description }),

  getAgentRules: () => http.get<AgentRule[]>("/agent-rules"),
  addAgentRule: (rule) => http.post<AgentRule>("/agent-rules", rule),
  deleteAgentRule: (id) => http.del<{ id: string }>(`/agent-rules/${id}`),

  getRules: () => http.get<OutreachRules>("/rules"),
  saveRules: (patch) => http.put<OutreachRules>("/rules", patch),

  getWorkspaceId: () => http.get<{ tenant_id: string }>("/workspace"),
};
