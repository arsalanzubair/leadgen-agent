/**
 * ConnectionRow.tsx -- one provider, one row.
 *
 * The rules this component exists to enforce:
 *
 *   - Save is disabled until a test has passed. Nothing is stored on the
 *     strength of somebody typing carefully.
 *   - A saved key is never displayed. Not masked, not partially, not on
 *     request. The only thing that comes back from the server is the last four
 *     characters, and that exists so the user can tell which key they saved --
 *     not so anything can be reconstructed from it.
 *   - Editing an existing connection starts from empty, never from the stored
 *     value, because the stored value is not available to this client at all.
 *   - Every provider links to the exact page where its key comes from. "Get an
 *     API key" with no destination is where setup goes to die.
 */

import {
  ArrowUpRight,
  Check,
  Eye,
  EyeOff,
  Loader2,
  Plug,
  Trash2,
  TriangleAlert,
  Zap,
} from "lucide-react";
import * as React from "react";

import { Button, Card, Input, Label, Textarea } from "@/components/ui/primitives";
import { connectionLabel, connectionTone } from "@/lib/statusLabels";
import { maskedKey } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Connection, ConnectionTestResult } from "@/types/workspace";

export function ConnectionRow({
  connection,
  onTest,
  onSave,
  onDisconnect,
  disabled,
}: {
  connection: Connection;
  onTest: (values: Record<string, string>) => Promise<ConnectionTestResult>;
  onSave: (values: Record<string, string>) => Promise<void>;
  onDisconnect: () => Promise<void>;
  disabled?: boolean;
}) {
  const [open, setOpen] = React.useState(false);
  const [values, setValues] = React.useState<Record<string, string>>({});
  const [reveal, setReveal] = React.useState<Record<string, boolean>>({});
  const [testing, setTesting] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [result, setResult] = React.useState<ConnectionTestResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const connected = connection.state === "connected";
  const tone = connectionTone(connection.state);

  const required = connection.fields.filter((field) => field.required);
  const complete = required.every((field) => (values[field.name] ?? "").trim().length > 0);
  const passed = result?.ok === true;

  const reset = () => {
    setValues({});
    setReveal({});
    setResult(null);
    setError(null);
  };

  const close = () => {
    setOpen(false);
    reset();
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    setResult(null);
    try {
      setResult(await onTest(values));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The check could not be run.");
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave(values);
      close();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card weight="flat" className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <span
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-control border",
            connected
              ? "border-success/30 bg-success-muted"
              : connection.state === "invalid"
                ? "border-danger/30 bg-danger-muted"
                : "border-border bg-surface-raised",
          )}
        >
          {connected ? (
            <Check size={15} className="text-success" />
          ) : connection.state === "invalid" ? (
            <TriangleAlert size={15} className="text-danger" />
          ) : (
            <Plug size={15} className="text-tertiary" />
          )}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-meta font-medium text-primary">{connection.name}</p>
            <span
              className={cn(
                "rounded-control border px-1.5 py-px text-[10px]",
                tone === "positive"
                  ? "border-success/25 bg-success-muted text-success"
                  : tone === "negative"
                    ? "border-danger/25 bg-danger-muted text-danger"
                    : "border-border bg-surface-raised text-secondary",
              )}
            >
              {connectionLabel(connection.state)}
            </span>
            {connection.essential && !connected && !connection.alternative_connected ? (
              <span className="text-[10px] text-warning">needed to run</span>
            ) : null}
          </div>
          <p className="mt-0.5 text-micro text-secondary">{connection.purpose}</p>
          <p className="mt-0.5 text-[10px] text-tertiary">
            {connection.free_tier}
            {connected && connection.last4 ? (
              <span className="num"> · {maskedKey(connection.last4)}</span>
            ) : null}
          </p>
          {connection.state === "invalid" && connection.problem ? (
            <p className="mt-1 text-micro text-danger">{connection.problem}</p>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {connected ? (
            <>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setOpen((value) => !value)}
                disabled={disabled}
              >
                Replace
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void onDisconnect()}
                disabled={disabled}
                aria-label={`Disconnect ${connection.name}`}
              >
                <Trash2 size={14} />
              </Button>
            </>
          ) : (
            <Button
              /* Accent is reserved for what actually matters here. Eight rows
                 of identical purple buttons makes the AI connection -- which
                 the product genuinely cannot run without -- look no more
                 urgent than translation, which most people never need. */
              variant={
                open
                  ? "secondary"
                  : connection.essential && !connection.alternative_connected
                    ? "primary"
                    : "secondary"
              }
              size="sm"
              onClick={() => (open ? close() : setOpen(true))}
              disabled={disabled}
            >
              {open ? "Cancel" : "Connect"}
            </Button>
          )}
        </div>
      </div>

      {open ? (
        <div className="animate-fade-in space-y-3 border-t border-border bg-surface-raised px-4 py-4">
          {connection.fields.map((field) => {
            const id = `${connection.id}-${field.name}`;
            const isSecret = field.kind === "secret";
            const shown = reveal[field.name] ?? false;
            return (
              <div key={field.name}>
                <Label htmlFor={id}>{field.label}</Label>
                <div className="mt-1.5 flex gap-2">
                  {field.multiline ? (
                    <Textarea
                      id={id}
                      rows={4}
                      className="num"
                      placeholder={field.placeholder}
                      value={values[field.name] ?? ""}
                      onChange={(event) =>
                        setValues({ ...values, [field.name]: event.target.value })
                      }
                      autoComplete="off"
                      spellCheck={false}
                    />
                  ) : (
                    <Input
                      id={id}
                      // A secret is masked while typing. Not because the value is
                      // secret from the person entering it, but because these get
                      // pasted while somebody is screen-sharing.
                      type={isSecret && !shown ? "password" : "text"}
                      className="num"
                      placeholder={field.placeholder}
                      value={values[field.name] ?? ""}
                      onChange={(event) =>
                        setValues({ ...values, [field.name]: event.target.value })
                      }
                      autoComplete="off"
                      spellCheck={false}
                    />
                  )}
                  {isSecret && !field.multiline ? (
                    <Button
                      variant="secondary"
                      size="icon"
                      onClick={() => setReveal({ ...reveal, [field.name]: !shown })}
                      aria-label={shown ? "Hide" : "Show"}
                    >
                      {shown ? <EyeOff size={14} /> : <Eye size={14} />}
                    </Button>
                  ) : null}
                </div>
                {field.hint ? (
                  <p className="mt-1 text-micro text-tertiary">{field.hint}</p>
                ) : null}
              </div>
            );
          })}

          {connection.help_url ? (
            <a
              href={connection.help_url}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-1 text-micro text-accent-text underline decoration-accent/40 hover:decoration-accent"
            >
              {connection.help_label}
              <ArrowUpRight size={11} />
            </a>
          ) : null}

          {result ? (
            <p
              className={cn(
                "flex items-start gap-2 rounded-control border px-3 py-2 text-micro",
                result.ok
                  ? "border-success/30 bg-success-muted text-success"
                  : "border-danger/30 bg-danger-muted text-danger",
              )}
            >
              {result.ok ? (
                <Check size={13} className="mt-0.5 shrink-0" />
              ) : (
                <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              )}
              <span>
                {result.message}
                {result.detail ? (
                  <span className="mt-0.5 block opacity-80">{result.detail}</span>
                ) : null}
              </span>
            </p>
          ) : null}

          {error ? <p className="text-micro text-danger">{error}</p> : null}

          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" onClick={test} disabled={!complete || testing || saving}>
              {testing ? (
                <>
                  <Loader2 size={15} className="animate-spin" />
                  Checking
                </>
              ) : (
                <>
                  <Zap size={15} />
                  Test connection
                </>
              )}
            </Button>
            <Button variant="primary" onClick={save} disabled={!passed || saving}>
              {saving ? (
                <>
                  <Loader2 size={15} className="animate-spin" />
                  Saving
                </>
              ) : (
                "Save"
              )}
            </Button>
            {!passed ? (
              <span className="text-micro text-tertiary">
                Test it first - nothing is saved until the check passes.
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </Card>
  );
}
