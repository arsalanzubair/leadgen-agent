"""
graph.py -- StateGraph construction and edge wiring.

Section 5, wired literally:

    START -> N0 -> N1 -> N2
    N2   --unreachable?-->                   N9 | N3
    N3   --fit_score vs tenant threshold-->  N3.5 | N9(archive)
    N3.5 -> N4 (the draft is parameterised by `channel`)
    N4   -> N5
    N5   --approval_status-->                N5.5 | N9(archive)
    N5.5 --suppression_status-->             N6a | N6b | N9(archive)
    N6a  --channel == both?-->               N6b | N7
    N6b  -> N7
    N7   --reply_category-->  interested: manual handoff + N9
                              otherwise:  N8
    N8   -> N4 (next touch, when actually due) | N9
    N9   -> END

Compiled with a PER-TENANT SqliteSaver whose path is derived from tenant_id.
Checkpoint databases are never shared across tenants (Section 7).

TWO WIRING DECISIONS WORTH STATING
----------------------------------
1. N9 is the single exit. Every terminal path -- archived, rejected,
   suppressed, unreachable, replied, cadence exhausted, manual review -- goes
   through the CRM node before END, so no lead can leave the system without a
   row explaining what happened to it.
2. The N8 -> N4 loop is only taken when the next touch is genuinely due.
   Looping unconditionally would run a three-touch cadence inside a single
   invocation and send every touch in the same minute; the day-gaps are the
   point. A future touch ends the run at N9, and the next scheduled run resumes
   the checkpointed thread and takes the loop then.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.nodes.n0_config_load import n0_config_load
from src.nodes.n1_discovery import n1_discovery
from src.nodes.n2_enrichment import n2_enrichment
from src.nodes.n3_5_channel_selection import n3_5_channel_selection
from src.nodes.n3_qualification import n3_qualification, route_after_qualification
from src.nodes.n4_personalization import n4_personalization
from src.nodes.n5_5_suppression_gate import n5_5_suppression_gate, route_after_suppression
from src.nodes.n5_human_approval import n5_human_approval, route_after_approval
from src.nodes.n6a_email_outreach import n6a_email_outreach
from src.nodes.n6b_linkedin_outreach import n6b_linkedin_outreach
from src.nodes.n7_reply_monitoring import n7_reply_monitoring, route_after_reply
from src.nodes.n8_followup_sequencer import n8_followup_sequencer, route_after_sequencer
from src.nodes.n9_crm_analytics import n9_crm_analytics
from src.reliability import log
from src.settings import checkpoint_path
from src.state import LeadState, wants_linkedin

#: Graph node names. Kept as constants so an edge and a node cannot disagree
#: about a name through a typo that only shows up at runtime.
N0, N1, N2 = "config_load", "discovery", "enrichment"
N3, N3_5 = "qualification", "channel_selection"
N4, N5, N5_5 = "personalization", "human_approval", "suppression_gate"
N6A, N6B = "email_outreach", "linkedin_outreach"
N7, N8, N9 = "reply_monitoring", "sequencer", "crm"


# --------------------------------------------------------------------------- #
# Checkpointing (per tenant)
# --------------------------------------------------------------------------- #

@contextmanager
def checkpointer_for(tenant_id: str) -> Iterator[SqliteSaver]:
    """
    Open the SqliteSaver for ONE tenant.

    Section 7: the database path is derived from tenant_id inside
    settings.checkpoint_path and there is no override parameter, so two tenants
    cannot be pointed at one file by a caller passing the wrong argument.
    """
    path = checkpoint_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    log.debug("opening checkpointer tenant=%s path=%s", tenant_id, path)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def thread_config(tenant_id: str, thread_id: str, **extra: Any) -> dict[str, Any]:
    """
    The LangGraph config for one lead's thread.

    `thread_id` is the lead_id, which is already tenant-scoped (make_lead_id
    hashes tenant_id into it), so two tenants' leads can never collide on a
    thread even when the same company is discovered for both.
    """
    configurable = {"thread_id": thread_id, "tenant_id": tenant_id}
    configurable.update(extra)
    return {"configurable": configurable, "recursion_limit": 50}


# --------------------------------------------------------------------------- #
# Conditional edges
# --------------------------------------------------------------------------- #

def route_after_enrichment(state: LeadState) -> str:
    """A lead with no contact route is excluded from every downstream node."""
    if state.get("unreachable") or state.get("archived"):
        return N9
    if state.get("needs_manual_review"):
        return N9
    return N3


def route_after_channel_selection(state: LeadState) -> str:
    if state.get("archived") or state.get("needs_manual_review"):
        return N9
    return N4


def route_after_personalization(state: LeadState) -> str:
    """A draft that failed its self-check goes to a human, not to a send."""
    if state.get("needs_manual_review") or state.get("archived"):
        return N9
    return N5


def route_after_email(state: LeadState) -> str:
    """
    A `both` lead still owes a LinkedIn touch after the email goes out.
    Everything else moves on to reply monitoring.
    """
    if state.get("needs_manual_review"):
        return N9
    if wants_linkedin(state.get("channel", "")) and state.get("linkedin_url"):
        return N6B
    return N7


def route_after_linkedin(state: LeadState) -> str:
    if state.get("needs_manual_review"):
        return N9
    return N7


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def build_graph(tenant_id: str, *, checkpointer: SqliteSaver | None = None):
    """
    Build and compile the lead graph for one tenant.

    `tenant_id` is taken even though the nodes resolve their own config from
    state, because a caller that has to name the tenant to get a graph cannot
    accidentally run tenant A's leads through a graph compiled against tenant
    B's checkpointer.
    """
    builder = StateGraph(LeadState)

    builder.add_node(N0, n0_config_load)
    builder.add_node(N1, n1_discovery)
    builder.add_node(N2, n2_enrichment)
    builder.add_node(N3, n3_qualification)
    builder.add_node(N3_5, n3_5_channel_selection)
    builder.add_node(N4, n4_personalization)
    builder.add_node(N5, n5_human_approval)
    builder.add_node(N5_5, n5_5_suppression_gate)
    builder.add_node(N6A, n6a_email_outreach)
    builder.add_node(N6B, n6b_linkedin_outreach)
    builder.add_node(N7, n7_reply_monitoring)
    builder.add_node(N8, n8_followup_sequencer)
    builder.add_node(N9, n9_crm_analytics)

    # -- the straight run in --------------------------------------------- #
    builder.add_edge(START, N0)
    builder.add_edge(N0, N1)
    builder.add_edge(N1, N2)

    builder.add_conditional_edges(N2, route_after_enrichment, {N3: N3, N9: N9})

    # -- qualification ---------------------------------------------------- #
    builder.add_conditional_edges(
        N3,
        route_after_qualification,
        {"channel_selection": N3_5, "archive": N9, "manual_review": N9},
    )

    builder.add_conditional_edges(
        N3_5, route_after_channel_selection, {N4: N4, N9: N9}
    )

    # -- drafting and approval -------------------------------------------- #
    builder.add_conditional_edges(
        N4, route_after_personalization, {N5: N5, N9: N9}
    )
    builder.add_conditional_edges(
        N5,
        route_after_approval,
        {"suppression_gate": N5_5, "archive": N9, "manual_review": N9},
    )

    # -- the gate --------------------------------------------------------- #
    builder.add_conditional_edges(
        N5_5,
        route_after_suppression,
        {"email_outreach": N6A, "linkedin_outreach": N6B, "archive": N9},
    )

    # -- sending ---------------------------------------------------------- #
    builder.add_conditional_edges(N6A, route_after_email, {N6B: N6B, N7: N7, N9: N9})
    builder.add_conditional_edges(N6B, route_after_linkedin, {N7: N7, N9: N9})

    # -- replies and cadence ---------------------------------------------- #
    builder.add_conditional_edges(
        N7,
        route_after_reply,
        {"manual_handoff": N9, "sequencer": N8, "crm": N9},
    )
    builder.add_conditional_edges(
        N8, route_after_sequencer, {"personalization": N4, "crm": N9}
    )

    builder.add_edge(N9, END)

    graph = builder.compile(checkpointer=checkpointer)
    log.debug("compiled graph for tenant=%s", tenant_id)
    return graph


@contextmanager
def graph_for(tenant_id: str):
    """
    Convenience: open the tenant's checkpointer and compile a graph against it.

        with graph_for("example_tenant") as graph:
            graph.invoke(lead, config=thread_config("example_tenant", lead["lead_id"]))
    """
    with checkpointer_for(tenant_id) as saver:
        yield build_graph(tenant_id, checkpointer=saver)


def render_mermaid(tenant_id: str = "example_tenant") -> str:
    """The graph as a Mermaid diagram, for the README and for eyeballing edges."""
    return build_graph(tenant_id).get_graph().draw_mermaid()
