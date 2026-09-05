"""
email_adapters.py -- sending and reading, behind their interfaces.

These two capabilities already had an ABC and a factory before this layer
existed (`EmailSender`/`get_sender`, `EmailReader`/`get_reader`), and the
concrete classes already satisfy the matching `Protocol` structurally. So
there is nothing to wrap here and no adapter class to write: what was missing
was not an interface, it was who chooses.

That is the entire change. The old factories defaulted from `.env`
(`EMAIL_PROVIDER`, `EMAIL_READER`); these build functions are handed the
tenant's own choice instead, and the factories' credential-based degradation
to a dry-run sender or a null reader is left exactly as it was -- a batch must
not die at N6a because a mailbox was never connected.
"""

from __future__ import annotations

from src.integrations import email_reader as _reader_module
from src.integrations import email_sender as _sender_module
from src.integrations.email_reader import EmailReader, NullReader
from src.integrations.email_sender import DryRunSender, EmailSender
from src.providers.base import ProviderContext
from src.providers.registry import Capability, ProviderSpec


def _build_sender(spec: ProviderSpec, ctx: ProviderContext) -> EmailSender:
    """
    Resolve the sender for one workspace.

    `daily_limits` comes from the tenant's config so the durable per-day
    counter is keyed on the provider the tenant actually chose. Losing that
    would let a workspace that switched from Gmail to Brevo start the day with
    a fresh 40 sends on top of the 40 it already made.
    """
    limits = dict(ctx.option("daily_limits", {}) or {})

    if spec.id == "dry_run":
        return DryRunSender(ctx.tenant_id, limits.get("dry_run"))

    # Through the module, not a bound name: `get_sender` is patched by the
    # outreach tests, and a name bound at import time would not see that.
    return _sender_module.get_sender(
        ctx.tenant_id,
        provider=spec.id,
        daily_limits=limits,
        dry_run=ctx.dry_run,
    )


def _build_reader(spec: ProviderSpec, ctx: ProviderContext) -> EmailReader:
    """
    Resolve the reply reader.

    `none` is handled here rather than passed through: `get_reader` treats an
    unrecognised provider as "use IMAP if any mailbox credential exists", which
    is the right default but the wrong answer to somebody who deliberately
    chose not to watch for replies.
    """
    if spec.id == "none" or ctx.dry_run:
        return NullReader()
    return _reader_module.get_reader(provider=spec.id, dry_run=False)


def build(spec: ProviderSpec, ctx: ProviderContext):
    """Registry factory for both email capabilities."""
    if spec.capability is Capability.EMAIL_SENDER:
        return _build_sender(spec, ctx)
    return _build_reader(spec, ctx)
