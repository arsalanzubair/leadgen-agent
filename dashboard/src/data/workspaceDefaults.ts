/**
 * workspaceDefaults.ts -- what this deployment ships with, before the user
 * changes anything.
 *
 * Only the sending rules are here, and only as a FALLBACK: `useWorkspace`
 * shows them when the settings service is unreachable, so no screen renders
 * broken and the offline banner has something to sit above. When the service
 * is up, everything comes from it. Saves stay blocked either way.
 *
 * Audiences used to be here too, as four worked examples transcribed from the
 * configuration file. They are gone. A region list and a follow-up plan are
 * genuine product defaults that a fresh workspace really does start with; four
 * industries are not, and showing them while the service was down told the
 * user they were set up to search for dental clinics when nobody had said so.
 * With no audiences, Find Leads already blocks with "You have not described
 * anyone to look for yet", which is the truth.
 */

import type { OutreachRules } from "@/types/workspace";

/**
 * The workspace this deployment is.
 *
 * Not shown to the user and not selectable -- one deployment, one workspace --
 * but the backend namespaces every file it writes by this id, so the client
 * carries it on each read. `useWorkspace` replaces this with whatever
 * `/api/workspace` reports as soon as the service answers.
 */
export const WORKSPACE_ID = "example_tenant";

export const DEFAULT_RULES: OutreachRules = {
  regions: ["US", "UK", "EU", "CA", "AU", "ME"],
  language_map: { US: "en", UK: "en", EU: "fr", CA: "en", AU: "en", ME: "en" },
  channels: { enabled: ["email", "linkedin"], preference_when_both: "both" },
  fit_score_threshold: 60,
  max_leads_per_run: 40,
  daily_send_limits: { gmail_smtp: 40, brevo: 40, linkedin_manual: 15 },
  require_approval_before_send: true,
  // Free mailbox providers, so a personal address never receives cold outreach.
  // The user's own domain is added from their Business Profile, not hard-coded.
  blocked_domains: ["gmail.com", "outlook.com", "yahoo.com", "hotmail.com"],
  follow_ups: [
    { step: 1, name: "First email", wait_days: 0, channel: "email" },
    { step: 2, name: "LinkedIn request", wait_days: 2, channel: "linkedin" },
    { step: 3, name: "Second email", wait_days: 5, channel: "email" },
    { step: 4, name: "Last email", wait_days: 7, channel: "email" },
  ],
  test_mode_default: true,
};
