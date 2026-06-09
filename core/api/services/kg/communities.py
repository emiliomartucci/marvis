# v0.1.0 - 2026-06-04 - Track 2 #3c: deterministic KG community detection (flag MARVIS_COMMUNITY_SUMMARIES, default OFF)
"""Deterministic community detection over the KG edge list (Track 2 #3c).

GraphRAG global-search (arXiv:2506.05690) needs a *partition* of the graph into
communities before any per-community summary can be written. The plan pins two
hard constraints on that step:

* **No LLM for the clustering** — caro/non-deterministic. Clustering is pure
  topology over the already-materialized edges.
* **Deterministic** — the same edge set must always yield the same partition,
  byte-identical, regardless of input ordering. A flaky partition would make
  every downstream summary flaky too.

We implement **deterministic label propagation** in pure Python (stdlib only —
NO networkx / igraph / python-louvain; none is in ``pyproject``, and a heavy
graph dep is banned on the shared host). Leiden/Louvain are the textbook
references but carry a dependency and a non-trivial determinism story (random
tie-breaks); synchronous label propagation with a *fixed* node order and
*lexicographic* tie-breaks is fully deterministic and dependency-free, which is
what #3c actually needs to surface a stable "quadro d'insieme" in the brief.

Determinism argument (same edges in → same partition out, in ANY input order):

1. The adjacency is built from a ``set`` of undirected pairs, so duplicate /
   reversed / re-ordered edge rows collapse to one canonical neighbor set.
2. Every node starts in its own singleton community (``community_id == node_id``).
3. Each sweep visits nodes in a **fixed order** (``sorted`` by ``node_id``) and
   uses the *same* (previous-sweep) label snapshot for all nodes in the sweep
   (synchronous update) — so the result never depends on visitation interleaving.
4. A node adopts the **most frequent** community label among **itself and its
   neighbors**; **ties broken by the smallest community_id** (a total order on
   labels). Counting the node's own label breaks the two-node label-swap
   oscillation that pure synchronous propagation suffers on bipartite-like
   structures (P↔Q would otherwise swap labels every sweep forever) and pulls
   each clique to its smallest member id. No randomness, no seed-dependent
   shuffles.
5. Sweeps repeat until a sweep changes nothing (fixed point) or ``max_iter`` is
   hit. The fixed-point/label-monotone cap guarantees termination.

``seed`` is accepted for API symmetry with stochastic detectors but is unused —
the algorithm is seed-independent by construction. It is kept so a future
swap-in (e.g. a seeded Leiden, off-host) is signature-compatible.

This module is a pure library. Nothing here touches the DB, the brief, or a
model. The caller is responsible for fetching CURRENT edges only
(``valid_until IS NULL``) and passing them in; honoring temporal validity is a
query concern, not a clustering concern.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# A node id is the KG ``{prefix}:{kind}:{slug}`` string. We treat it as opaque.
NodeId = str
CommunityId = str

# Hard cap on label-propagation sweeps. Label propagation converges fast on
# real graphs; the cap only guards against a pathological non-convergent
# oscillation (impossible here given synchronous + tie-break-stable updates,
# but cheap insurance and an explicit termination proof).
DEFAULT_MAX_ITER = 100


def _normalize_edges(
    edges: Iterable[Sequence[str] | Mapping[str, str]],
) -> tuple[list[NodeId], dict[NodeId, frozenset[NodeId]]]:
    """Build the (sorted node list, undirected adjacency) from raw edge rows.

    Accepts either ``(source_id, target_id)`` pairs/sequences or mappings with
    ``source_id``/``target_id`` keys (the ``graph_edges`` row shape). Self-loops
    are dropped. The adjacency is symmetric and de-duplicated, so input order /
    direction / duplicates never affect the result.
    """
    adjacency: dict[NodeId, set[NodeId]] = defaultdict(set)
    nodes: set[NodeId] = set()

    for edge in edges:
        if isinstance(edge, Mapping):
            src = edge["source_id"]
            dst = edge["target_id"]
        else:
            src, dst = edge[0], edge[1]
        nodes.add(src)
        nodes.add(dst)
        if src == dst:
            continue  # self-loop carries no community signal
        adjacency[src].add(dst)
        adjacency[dst].add(src)

    frozen = {n: frozenset(adjacency.get(n, set())) for n in nodes}
    return sorted(nodes), frozen


def detect_communities(
    edges: Iterable[Sequence[str] | Mapping[str, str]],
    *,
    seed: int = 0,
) -> dict[NodeId, CommunityId]:
    """Partition the KG nodes into communities via deterministic label propagation.

    Returns a mapping ``{node_id: community_id}``. ``community_id`` is itself a
    node id (the label that "won" the community), so ids are stable and human-
    readable. A node with no edges stays in its own singleton community.

    ``seed`` is accepted for API parity with stochastic detectors and is
    intentionally unused — the partition is a pure function of ``edges``.
    """
    _ = seed  # explicitly unused: determinism does not depend on a seed
    node_order, adjacency = _normalize_edges(edges)

    # Each node starts as its own community.
    labels: dict[NodeId, CommunityId] = {n: n for n in node_order}
    if not node_order:
        return labels

    for _iteration in range(DEFAULT_MAX_ITER):
        # Synchronous update: every node reads the SAME snapshot, so the sweep
        # is order-independent within the sweep (the sorted order only fixes a
        # canonical write sequence and makes any future async variant stable).
        snapshot = dict(labels)
        changed = False

        for node in node_order:  # fixed lexicographic order
            neighbors = adjacency[node]
            if not neighbors:
                continue  # singleton: nothing to adopt, keep own label

            # Vote over self + neighbors. Including the node's own label is what
            # makes the update converge (no two-node label-swap oscillation) and
            # collapses each clique onto its smallest member id deterministically.
            counts: Counter[CommunityId] = Counter(
                snapshot[other] for other in (node, *neighbors)
            )
            best_count = max(counts.values())
            # Tie-break: among the most-frequent labels, smallest community_id.
            winner = min(
                label
                for label, count in counts.items()
                if count == best_count
            )
            if winner != labels[node]:
                labels[node] = winner
                changed = True

        if not changed:
            break  # fixed point reached

    return labels


def community_members(
    partition: Mapping[NodeId, CommunityId],
) -> dict[CommunityId, list[NodeId]]:
    """Invert a partition into ``{community_id: [member node ids, sorted]}``.

    Communities are returned in sorted ``community_id`` order; members within
    each community are sorted. Fully deterministic.
    """
    members: dict[CommunityId, list[NodeId]] = defaultdict(list)
    for node, community in partition.items():
        members[community].append(node)
    return {
        community: sorted(members[community])
        for community in sorted(members)
    }


@dataclass(frozen=True)
class Community:
    """A detected community: its id, its sorted members, and a cheap label.

    ``label`` is a deterministic, model-free human handle for the cluster: the
    most-connected member within the community (highest intra-community degree),
    ties broken by the sorted-first member. It is a structural descriptor, NOT a
    summary — the summary is a separate, verification-bearing step
    (see ``community_summary``).
    """

    id: CommunityId
    members: tuple[NodeId, ...]
    label: NodeId = field(default="")

    @property
    def size(self) -> int:
        return len(self.members)


def build_communities(
    edges: Iterable[Sequence[str] | Mapping[str, str]],
    *,
    seed: int = 0,
) -> list[Community]:
    """Detect communities and wrap them as ``Community`` objects (sorted by id).

    Convenience seam over ``detect_communities`` + ``community_members`` that
    also computes the deterministic ``label`` per community. Returned list is in
    sorted ``community_id`` order.
    """
    edge_list = list(edges)
    partition = detect_communities(edge_list, seed=seed)
    members_by_community = community_members(partition)

    # Re-derive adjacency once to score intra-community degree for the label.
    _, adjacency = _normalize_edges(edge_list)

    communities: list[Community] = []
    for community_id, members in members_by_community.items():
        member_set = set(members)
        # Most-connected member = highest count of neighbors that are ALSO in
        # this community; ties broken by sorted-first member id.
        label = min(
            members,
            key=lambda n: (
                -len(adjacency[n] & member_set),  # higher degree first
                n,  # then lexicographically smallest
            ),
        )
        communities.append(
            Community(
                id=community_id,
                members=tuple(members),
                label=label,
            )
        )
    return communities
