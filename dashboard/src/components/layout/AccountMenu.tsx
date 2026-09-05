/**
 * AccountMenu.tsx -- the row pinned to the bottom of the rail.
 *
 * It carries the only remaining editor for who the user is. The Business
 * Profile screen is gone and Settings holds connections and nothing else, so
 * without this there would be nowhere to say what name and address outreach
 * goes out under -- and the greeting on Home could never be personalised.
 *
 * Nothing here is invented. Until the user types a name, the row says so and
 * the greeting stays name-free: the product does not guess at an identity and
 * then present it back as fact.
 */

import * as Popover from "@radix-ui/react-popover";
import { ChevronDown, Loader2 } from "lucide-react";
import * as React from "react";

import { Button, Input, Label } from "@/components/ui/primitives";
import { useWorkspace } from "@/hooks/useWorkspace";
import { initials, truncate } from "@/lib/format";
import { cn } from "@/lib/utils";

export function AccountMenu({ collapsed }: { collapsed: boolean }) {
  const { profile, saveProfile, offline } = useWorkspace();
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState(profile.sender_name);
  const [email, setEmail] = React.useState(profile.sender_email);
  const [business, setBusiness] = React.useState(profile.business_name);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // Re-seed the fields whenever the popover is opened, so it always shows what
  // is actually stored rather than a stale edit from a previous visit.
  React.useEffect(() => {
    if (!open) return;
    setName(profile.sender_name);
    setEmail(profile.sender_email);
    setBusiness(profile.business_name);
    setError(null);
  }, [open, profile]);

  const displayName = profile.sender_name.trim();
  const displayEmail = profile.sender_email.trim();

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await saveProfile({
        sender_name: name.trim(),
        sender_email: email.trim(),
        business_name: business.trim(),
      });
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "That could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label="Your account"
          className={cn(
            "flex w-full items-center rounded-card transition-colors duration-150 hover:bg-surface-raised",
            collapsed ? "h-11 justify-center" : "h-14 gap-2.5 px-2",
          )}
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-micro font-semibold text-accent">
            {displayName ? initials(displayName) : "?"}
          </span>
          {!collapsed ? (
            <>
              <span className="min-w-0 flex-1 text-left">
                <span className="block truncate text-meta font-semibold text-primary">
                  {displayName || "Add your name"}
                </span>
                <span className="block truncate text-micro text-tertiary">
                  {displayEmail || "Not set yet"}
                </span>
              </span>
              <ChevronDown size={15} className="shrink-0 text-tertiary" />
            </>
          ) : null}
        </button>
      </Popover.Trigger>

      <Popover.Portal>
        <Popover.Content
          side="top"
          align="start"
          sideOffset={8}
          className="z-50 w-[300px] rounded-card border border-border bg-bg p-4 shadow-raised"
        >
          <p className="text-meta font-semibold text-primary">Your details</p>
          <p className="mt-1 text-micro text-tertiary">
            Outreach goes out under this name and address.
          </p>

          <div className="mt-3 space-y-3">
            <div>
              <Label htmlFor="account-name">Your name</Label>
              <Input
                id="account-name"
                className="mt-1.5"
                value={name}
                autoComplete="name"
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="account-email">Your email address</Label>
              <Input
                id="account-email"
                type="email"
                className="mt-1.5"
                value={email}
                autoComplete="email"
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="account-business">Your business name</Label>
              <Input
                id="account-business"
                className="mt-1.5"
                value={business}
                autoComplete="organization"
                onChange={(event) => setBusiness(event.target.value)}
              />
              <p className="mt-1 text-micro text-tertiary">
                Replaces {truncate("LeadFlow", 20)} in the sidebar.
              </p>
            </div>
          </div>

          {error ? <p className="mt-3 text-micro text-danger">{error}</p> : null}
          {offline ? (
            <p className="mt-3 text-micro text-warning">
              The settings service is not running, so this cannot be saved yet.
            </p>
          ) : null}

          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void save()}
              disabled={saving || offline}
            >
              {saving ? <Loader2 size={14} className="animate-spin" /> : null}
              Save
            </Button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
