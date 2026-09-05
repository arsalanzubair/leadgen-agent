/**
 * Connections.tsx -- the whole of Settings.
 *
 * A plain list of providers grouped by the job they do. No capability grid, no
 * primary/fallback picker: connecting an account is the only decision on this
 * screen, and connecting one points the job at it.
 *
 * That last part matters. With the picker gone, a connection that did not also
 * become the choice would be a key the user entered, saw marked "Connected",
 * and which nothing ever used -- which is precisely the failure this product
 * has already been bitten by once.
 *
 * The groups and their membership are declared here because the product spec
 * names them exactly, including Apollo appearing under two headings from one
 * connection. Everything else about each row -- its name, its one-line
 * purpose, its fields, its help link -- still comes from `GET /api/providers`,
 * so no vendor detail is duplicated in this file.
 *
 * Nothing on this screen ever displays a saved key. See ConnectionRow.
 */

import { KeyRound, Lock, ShieldCheck } from "lucide-react";
import * as React from "react";

import { ConnectionRow } from "@/components/settings/ConnectionRow";
import { PageHeader } from "@/components/layout/AppShell";
import { Card } from "@/components/ui/primitives";
import { EmptyState, ErrorState, InlineError, SkeletonCards } from "@/components/ui/states";
import { useWorkspace } from "@/hooks/useWorkspace";
import { workspace } from "@/services";
import type { CapabilityGroup, Connection, SelectionMap } from "@/types/workspace";

/**
 * The sections, in order, and which providers appear under each.
 *
 * Apollo is listed twice on purpose: it finds businesses AND finds addresses,
 * and a user who has connected it for one should not be asked to connect it
 * again for the other. Both entries resolve to the same connection, so
 * connecting in either place connects both.
 */
const SECTIONS: { title: string; hint: string; providers: string[] }[] = [
  {
    title: "Email Finding",
    hint: "Finds a real address for a business you have found.",
    providers: ["hunter", "apollo", "anymail_finder", "website_only", "custom_enrichment"],
  },
  {
    title: "Lead Discovery",
    hint: "Finds the businesses in the first place.",
    providers: ["apollo", "osm", "csv_import", "custom_discovery_local"],
  },
  {
    title: "AI / LLM",
    hint: "Reads your request, scores each business, and writes each message.",
    providers: ["gemini", "openai", "anthropic", "deepseek", "custom_llm"],
  },
  {
    title: "Email Sending",
    hint: "Sends the messages you have approved, from your own mailbox.",
    providers: ["gmail_smtp", "brevo", "smtp", "custom_email_sender"],
  },
  {
    title: "Email Reading",
    hint: "Watches for replies so follow-ups stop when somebody answers.",
    providers: ["imap", "custom_email_reader"],
  },
  {
    title: "Your Records",
    hint: "Where every lead is written down, so the list is yours.",
    providers: [
      "google_sheets",
      "airtable",
      "csv",
      "hubspot",
      "pipedrive",
      "custom_crm",
    ],
  },
];

export function ConnectionsPage() {
  const { connections, reloadConnections, offline } = useWorkspace();

  const [groups, setGroups] = React.useState<CapabilityGroup[]>([]);
  const [selection, setSelection] = React.useState<SelectionMap>({});
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [nonce, setNonce] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);

    Promise.all([workspace.getCapabilities(), workspace.getSelection()])
      .then(([loadedGroups, loadedSelection]) => {
        if (cancelled) return;
        setGroups(loadedGroups);
        setSelection(loadedSelection);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error ? err.message : "The provider list could not be loaded.",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [nonce]);

  /**
   * Reload after any change. Both halves, always: connecting a key can flip a
   * job from "needs setting up" to "ready", and refreshing only one half
   * leaves the other describing a state that is no longer true.
   */
  const refresh = React.useCallback(async () => {
    setNonce((n) => n + 1);
    try {
      await reloadConnections();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That did not work.");
    }
  }, [reloadConnections]);

  /** The registry's entry for an id, merged with what the server knows. */
  const resolve = React.useCallback(
    (id: string): Connection | undefined => {
      const listed = groups
        .flatMap((group) => group.providers)
        .find((provider) => provider.id === id);
      const live = connections.find((c) => c.id === id);
      if (!listed) return live;
      return live ? { ...listed, ...live } : listed;
    },
    [groups, connections],
  );

  /**
   * Save a credential, then point the job at the provider it belongs to.
   *
   * The selection call is best-effort: if it fails, the key is still stored
   * and correct, and the failure surfaces rather than rolling back a
   * connection the user successfully made.
   */
  const connect = React.useCallback(
    async (provider: Connection, values: Record<string, string>) => {
      await workspace.saveConnection(provider.id, values);
      const current = selection[provider.category];
      if (current?.primary !== provider.id) {
        try {
          await workspace.setSelection(provider.category, {
            primary: provider.id,
            fallback: current?.fallback === provider.id ? "" : (current?.fallback ?? ""),
          });
        } catch (err) {
          setError(
            err instanceof Error
              ? `${provider.name} was connected, but could not be selected: ${err.message}`
              : `${provider.name} was connected, but could not be selected.`,
          );
        }
      }
      await refresh();
    },
    [selection, refresh],
  );

  if (error && !groups.length) {
    return (
      <div>
        <PageHeader
          title="Settings"
          subtitle="Your own accounts, your own keys, your own free tiers."
        />
        <ErrorState message={error} onRetry={() => setNonce((n) => n + 1)} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Settings"
        subtitle="Your own accounts, your own keys, your own free tiers."
      />

      <Card weight="standard" className="mb-6 p-4">
        <p className="flex items-start gap-2.5 text-meta text-secondary">
          <Lock size={16} className="mt-0.5 shrink-0 text-tertiary" />
          <span>
            <span className="font-semibold text-primary">
              Keys are encrypted on this machine and never shown again.
            </span>{" "}
            Once saved, a key cannot be read back by this screen or by anything
            else — you will only ever see its last four characters, so you can
            tell which one you saved. To change one, you replace it.
          </span>
        </p>
        <p className="mt-2.5 flex items-start gap-2.5 text-micro text-tertiary">
          <ShieldCheck size={15} className="mt-0.5 shrink-0" />
          Every key is checked against the real provider before it is stored, so
          a typo is caught here rather than halfway through a search.
        </p>
      </Card>

      {error ? (
        <div className="mb-4">
          <InlineError message={error} />
        </div>
      ) : null}

      {loading && !groups.length ? (
        <SkeletonCards count={3} />
      ) : groups.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          title="No providers available"
          body="The settings service did not report anything that can be connected. Check that it started cleanly."
        />
      ) : (
        <div className="space-y-8">
          {SECTIONS.map((section) => {
            const rows = section.providers
              .map(resolve)
              .filter((provider): provider is Connection => !!provider);

            if (!rows.length) return null;

            return (
              <section key={section.title}>
                <h2 className="text-section font-bold text-primary">{section.title}</h2>
                <p className="mt-1 text-meta text-tertiary">{section.hint}</p>

                <ul className="mt-4 space-y-2.5">
                  {rows.map((provider) => (
                    <li key={`${section.title}-${provider.id}`}>
                      {provider.fields.length === 0 ? (
                        <NoSetupRow provider={provider} />
                      ) : (
                        <ConnectionRow
                          connection={provider}
                          disabled={offline}
                          onTest={(values) =>
                            workspace.testConnection(provider.id, values)
                          }
                          onSave={(values) => connect(provider, values)}
                          onDisconnect={async () => {
                            await workspace.deleteConnection(provider.id);
                            await refresh();
                          }}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * A provider that needs no account at all.
 *
 * It gets a row rather than being hidden, because "there is a free option here
 * that needs nothing from you" is the single most useful thing this screen can
 * tell somebody who has just arrived and connected nothing.
 */
function NoSetupRow({ provider }: { provider: Connection }) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-card border border-border bg-bg px-4 py-3.5">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-success-muted">
        <ShieldCheck size={17} className="text-success" />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-meta font-semibold text-primary">{provider.name}</p>
        <p className="mt-0.5 text-micro text-tertiary">{provider.purpose}</p>
      </div>
      <span className="shrink-0 rounded-control border border-border bg-surface-raised px-2.5 py-1 text-micro text-secondary">
        No signup needed
      </span>
    </div>
  );
}
