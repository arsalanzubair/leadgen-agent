/**
 * App.tsx -- routing.
 *
 * Four screens and Settings. Everything else is a redirect: the product used
 * to have a dozen routes, and a bookmark or a link in somebody's notes should
 * land on the screen that absorbed the old one rather than on a 404.
 *
 * Approvals and follow-ups have no route of their own any more. Both were
 * folded into the channel they belong to, and their redirects say so by
 * pointing at Email.
 */

import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { WorkspaceProvider } from "@/hooks/useWorkspace";
import { ConnectionsPage } from "@/pages/settings/Connections";
import { EmailPage } from "@/pages/Email";
import { HomePage } from "@/pages/Home";
import { LeadsPage } from "@/pages/Leads";
import { LinkedInPage } from "@/pages/LinkedIn";
import { NotFoundPage } from "@/pages/NotFound";

export function App() {
  return (
    <BrowserRouter
      // Opt in to the v7 behaviours now: it silences the upgrade warnings and
      // means the router's semantics will not change under us on a bump.
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <WorkspaceProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<HomePage />} />
            <Route path="/leads" element={<LeadsPage />} />
            <Route path="/email" element={<EmailPage />} />
            <Route path="/linkedin" element={<LinkedInPage />} />
            <Route path="/settings" element={<ConnectionsPage />} />

            {/* Where the old routes went. */}
            <Route path="/find" element={<Navigate to="/" replace />} />
            <Route path="/agent" element={<Navigate to="/" replace />} />
            <Route path="/agent/runs/:id" element={<Navigate to="/" replace />} />
            <Route path="/leads/:id" element={<Navigate to="/leads" replace />} />
            <Route path="/campaigns" element={<Navigate to="/leads" replace />} />
            <Route path="/outreach" element={<Navigate to="/email" replace />} />
            <Route path="/outreach/email" element={<Navigate to="/email" replace />} />
            <Route path="/outreach/linkedin" element={<Navigate to="/linkedin" replace />} />
            <Route path="/outreach/approvals" element={<Navigate to="/email" replace />} />
            <Route path="/outreach/follow-ups" element={<Navigate to="/email" replace />} />
            <Route path="/approvals" element={<Navigate to="/email" replace />} />
            <Route path="/follow-ups" element={<Navigate to="/email" replace />} />
            <Route path="/results" element={<Navigate to="/leads" replace />} />
            <Route path="/results/performance" element={<Navigate to="/leads" replace />} />
            <Route path="/results/activity" element={<Navigate to="/leads" replace />} />
            <Route path="/analytics" element={<Navigate to="/leads" replace />} />
            <Route path="/activity" element={<Navigate to="/leads" replace />} />
            <Route path="/settings/connections" element={<Navigate to="/settings" replace />} />
            <Route path="/settings/profile" element={<Navigate to="/settings" replace />} />
            <Route path="/settings/targeting" element={<Navigate to="/settings" replace />} />
            <Route path="/settings/leo" element={<Navigate to="/settings" replace />} />

            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </WorkspaceProvider>
    </BrowserRouter>
  );
}
