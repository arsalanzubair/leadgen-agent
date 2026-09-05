/**
 * useWorkspace.tsx -- the one workspace, and everything the user configured.
 *
 * There are no accounts and no workspace switcher: one deployment serves one
 * business. The backend still namespaces its files by an internal id, so this
 * provider holds that id and hands it to the service layer as `scope`; no
 * screen ever names it, and nothing ever asks the user to pick one.
 *
 * It also holds the four things every screen needs to behave correctly:
 * who the user is, which providers they have connected, who they are looking
 * for, and the rules they set. Loading those once here is what lets Home show
 * a setup checklist and Find Leads run a pre-flight check from the same
 * numbers, instead of two screens disagreeing about whether the product is
 * ready to use.
 *
 * When the settings service is not running, the provider says so plainly
 * (`offline`), shows the defaults from the workspace's config file so nothing
 * renders broken, and refuses every save rather than pretending one worked.
 */

import * as React from "react";

import { DEFAULT_RULES, WORKSPACE_ID } from "@/data/workspaceDefaults";
import { ServiceOfflineError, workspace as service, __setWorkspaceContext } from "@/services";
import type {
  AgentRule,
  BusinessProfile,
  Connection,
  NicheConfig,
  OutreachRules,
  SetupState,
} from "@/types/workspace";
import { EMPTY_BUSINESS_PROFILE, hasSenderIdentity } from "@/types/workspace";

interface WorkspaceValue {
  /** Internal namespace. Shown only under "Technical details". */
  tenantId: string;
  /** What the service layer wants. */
  scope: { tenant_id: string };

  profile: BusinessProfile;
  connections: Connection[];
  niches: NicheConfig[];
  rules: OutreachRules;
  agentRules: AgentRule[];
  setup: SetupState;

  /**
   * Test Mode. The backend calls this `dry_run`; the product never does.
   * Defaults to on, which matches the backend's own default -- nothing sends
   * to a real person until somebody deliberately says so.
   */
  testMode: boolean;
  setTestMode: (on: boolean) => void;

  loading: boolean;
  /** True when the settings service could not be reached. */
  offline: boolean;
  error: string | null;
  refresh: () => void;

  saveProfile: (patch: Partial<BusinessProfile>) => Promise<void>;
  saveRules: (patch: Partial<OutreachRules>) => Promise<void>;
  saveNiche: (niche: NicheConfig, mode: "create" | "update") => Promise<void>;
  removeNiche: (id: string) => Promise<void>;
  addAgentRule: (rule: { kind: "always" | "never"; text: string }) => Promise<void>;
  removeAgentRule: (id: string) => Promise<void>;
  reloadConnections: () => Promise<void>;
}

const WorkspaceContext = React.createContext<WorkspaceValue | null>(null);

const TEST_MODE_KEY = "outreachr.testMode";

function readTestMode(fallback: boolean): boolean {
  try {
    const stored = window.localStorage.getItem(TEST_MODE_KEY);
    return stored === null ? fallback : stored === "true";
  } catch {
    return fallback;
  }
}

/** What is configured, and therefore what the product can actually do. */
function computeSetup(
  profile: BusinessProfile,
  connections: Connection[],
  niches: NicheConfig[],
): SetupState {
  const connected = (category: Connection["category"]) =>
    connections.some((c) => c.category === category && c.state === "connected");

  const has_ai = connected("llm");
  // Either kind of discovery counts: a workspace that only chases local
  // businesses has no reason to connect a contact database.
  const has_discovery =
    connected("discovery_local") || connected("discovery_b2b");
  const has_email_sending = connected("email_sender");
  const has_profile = hasSenderIdentity(profile);
  const has_niches = niches.length > 0;

  return {
    has_ai,
    has_discovery,
    has_email_sending,
    has_profile,
    has_niches,
    fresh: !has_ai && !has_discovery && !has_profile,
    // A run needs something to search for, somewhere to search, and something
    // to write with. Sending can be set up later -- Test Mode does not need it.
    ready_to_run: has_ai && has_discovery && has_niches,
  };
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [tenantId, setTenantId] = React.useState(WORKSPACE_ID);
  const [profile, setProfile] = React.useState<BusinessProfile>(EMPTY_BUSINESS_PROFILE);
  const [connections, setConnections] = React.useState<Connection[]>([]);
  const [niches, setNiches] = React.useState<NicheConfig[]>([]);
  const [rules, setRules] = React.useState<OutreachRules>(DEFAULT_RULES);
  const [agentRules, setAgentRules] = React.useState<AgentRule[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [offline, setOffline] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [nonce, setNonce] = React.useState(0);
  const [testMode, setTestModeState] = React.useState(() =>
    readTestMode(DEFAULT_RULES.test_mode_default),
  );

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);

    Promise.all([
      service.getWorkspaceId(),
      service.getProfile(),
      service.getConnections(),
      service.getNiches(),
      service.getRules(),
      service.getAgentRules(),
    ])
      .then(([id, loadedProfile, loadedConnections, loadedNiches, loadedRules, loadedAgentRules]) => {
        if (cancelled) return;
        setTenantId(id.tenant_id);
        setProfile(loadedProfile);
        setConnections(loadedConnections);
        setNiches(loadedNiches);
        setRules(loadedRules);
        setAgentRules(loadedAgentRules);
        setOffline(false);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ServiceOfflineError) {
          // Show the shipped sending defaults so no screen renders broken,
          // and flag it loudly. Saves stay blocked. Audiences stay empty --
          // guessing at who the user searches for would be inventing their
          // configuration back at them.
          setOffline(true);
          setNiches([]);
          setRules(DEFAULT_RULES);
          setError(null);
        } else {
          setError(err instanceof Error ? err.message : "Your settings could not be loaded.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [nonce]);

  // Match quality depends on the user's own match bar, and leads are scored
  // against it. Handing the real rules and audiences to the lead layer keeps
  // the numbers on Leads consistent with what Settings says.
  React.useEffect(() => {
    __setWorkspaceContext({ rules, niches });
  }, [rules, niches]);

  const refresh = React.useCallback(() => setNonce((n) => n + 1), []);

  const setTestMode = React.useCallback((on: boolean) => {
    setTestModeState(on);
    try {
      window.localStorage.setItem(TEST_MODE_KEY, String(on));
    } catch {
      // A browser with storage blocked still gets a working session, it just
      // starts in Test Mode again next time. That is the safe direction.
    }
  }, []);

  const guardOffline = React.useCallback(() => {
    if (offline) {
      throw new ServiceOfflineError("/api");
    }
  }, [offline]);

  const saveProfile = React.useCallback(
    async (patch: Partial<BusinessProfile>) => {
      guardOffline();
      setProfile(await service.saveProfile(patch));
    },
    [guardOffline],
  );

  const saveRules = React.useCallback(
    async (patch: Partial<OutreachRules>) => {
      guardOffline();
      setRules(await service.saveRules(patch));
    },
    [guardOffline],
  );

  const saveNiche = React.useCallback(
    async (niche: NicheConfig, mode: "create" | "update") => {
      guardOffline();
      const saved =
        mode === "create"
          ? await service.createNiche(niche)
          : await service.updateNiche(niche.id, niche);
      setNiches((current) => {
        const without = current.filter((n) => n.id !== saved.id);
        return [...without, saved].sort((a, b) => a.label.localeCompare(b.label));
      });
    },
    [guardOffline],
  );

  const removeNiche = React.useCallback(
    async (id: string) => {
      guardOffline();
      await service.deleteNiche(id);
      setNiches((current) => current.filter((n) => n.id !== id));
    },
    [guardOffline],
  );

  const addAgentRule = React.useCallback(
    async (rule: { kind: "always" | "never"; text: string }) => {
      guardOffline();
      const saved = await service.addAgentRule(rule);
      setAgentRules((current) => [...current, saved]);
    },
    [guardOffline],
  );

  const removeAgentRule = React.useCallback(
    async (id: string) => {
      guardOffline();
      await service.deleteAgentRule(id);
      setAgentRules((current) => current.filter((r) => r.id !== id));
    },
    [guardOffline],
  );

  const reloadConnections = React.useCallback(async () => {
    guardOffline();
    setConnections(await service.getConnections());
  }, [guardOffline]);

  const setup = React.useMemo(
    () => computeSetup(profile, connections, niches),
    [profile, connections, niches],
  );

  const value = React.useMemo<WorkspaceValue>(
    () => ({
      tenantId,
      scope: { tenant_id: tenantId },
      profile,
      connections,
      niches,
      rules,
      agentRules,
      setup,
      testMode,
      setTestMode,
      loading,
      offline,
      error,
      refresh,
      saveProfile,
      saveRules,
      saveNiche,
      removeNiche,
      addAgentRule,
      removeAgentRule,
      reloadConnections,
    }),
    [
      tenantId, profile, connections, niches, rules, agentRules, setup, testMode,
      setTestMode, loading, offline, error, refresh, saveProfile, saveRules,
      saveNiche, removeNiche, addAgentRule, removeAgentRule, reloadConnections,
    ],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceValue {
  const context = React.useContext(WorkspaceContext);
  if (!context) {
    throw new Error("useWorkspace must be used inside a WorkspaceProvider");
  }
  return context;
}

/** The label for an audience id, from the user's own configuration. */
export function useNicheLabel() {
  const { niches } = useWorkspace();
  return React.useCallback(
    (nicheId: string) => niches.find((n) => n.id === nicheId)?.label ?? nicheId,
    [niches],
  );
}
