"""TODO(P3): Explain what a "parent fork" is, how it relates to dominating forks, and why we need this.

In particular, explain the relationship to avoiding deadlocks, and that for most typical graphs such a
dominating fork does exist. Also explain how when there are multiple subsequent forks the preferred choice
could be ambiguous, and that in some cases it should/must be specified by the control flow graph designer.
"""

from __future__ import annotations

from collections.abc import Hashable
from copy import deepcopy
from dataclasses import dataclass
from functools import cached_property
from typing import Generic

from typing_extensions import TypeVar

T = TypeVar('T', bound=Hashable, infer_variance=True)


@dataclass
class ParentFork(Generic[T]):
    """A parent fork."""

    fork_id: T
    intermediate_nodes: set[T]
    """The set of node IDs of nodes upstream of the join and downstream of the parent fork.

    If there are no graph walkers in these nodes that were a part of a previous fork, it is safe to proceed downstream
    of the join.
    """


@dataclass
class ParentForkFinder(Generic[T]):
    """A parent fork finder."""

    nodes: set[T]
    start_ids: set[T]
    fork_ids: set[T]
    edges: dict[T, list[T]]  # source_id to list of destination_ids

    def find_parent_fork(self, join_id: T) -> ParentFork[T] | None:
        """Return the most ancestral parent fork of the join along with the that lie strictly between the fork and join.

        If every dominating fork of J lets J participate in a cycle that avoids the
        fork, return `None`, since that means no "parent fork" exists.
        """
        visited: set[str] = set()
        cur = join_id  # start at J and walk up the immediate dominator chain

        # TODO(P2): Make it a node-configuration option to choose the closest _or_ the farthest. Or manually specified(?)
        parent_fork: ParentFork[T] | None = None
        while True:
            cur = self._immediate_dominator(cur)
            if cur is None:  # reached the root
                break

            # The visited-tracking shouldn't be necessary, but I included it to prevent infinite loops if there are bugs
            assert cur not in visited, f'Cycle detected in dominator tree: {join_id} → {cur} → {visited}'
            visited.add(cur)

            if cur not in self.fork_ids:
                continue  # not a fork, so keep climbing

            upstream_nodes = self._get_upstream_nodes_if_parent(join_id, cur)
            if upstream_nodes is not None:  # found upstream nodes without a cycle
                parent_fork = ParentFork[T](cur, upstream_nodes)
            elif parent_fork is not None:
                # We reached a fork that is an ancestor of a parent fork but is not itself a parent fork.
                # This means there is a cycle to J that is downstream of `cur`, and so any node further upstream
                # will fail to be a parent fork for the same reason. So we can stop here and just return `parent_fork`.
                return parent_fork

        # No dominating fork passed the cycle test to be a "parent" fork
        return parent_fork

    @cached_property
    def _predecessors(self) -> dict[T, list[T]]:
        predecessors: dict[T, list[T]] = {n: [] for n in self.nodes}
        for source_id in self.nodes:
            for destination_id in self.edges.get(source_id, []):
                predecessors[destination_id].append(source_id)
        return predecessors

    @cached_property
    def _dominators(self) -> dict[T, set[T]]:
        node_ids = set(self.nodes)
        start_ids = self.start_ids

        dom: dict[T, set[T]] = {n: set(node_ids) for n in node_ids}
        for s in start_ids:
            dom[s] = {s}

        changed = True
        while changed:
            changed = False
            for n in node_ids - start_ids:
                preds = self._predecessors[n]
                if not preds:  # unreachable from any start
                    continue
                intersection = set[T].intersection(*(dom[p] for p in preds)) if preds else set[T]()
                new_dom = {n} | intersection
                if new_dom != dom[n]:
                    dom[n] = new_dom
                    changed = True
        return dom

    def _immediate_dominator(self, node_id: T) -> T | None:
        """Return the immediate dominator of node_id (if any)."""
        dom = self._dominators
        candidates = dom[node_id] - {node_id}
        for c in candidates:
            if all((c == d) or (c not in dom[d]) for d in candidates):
                return c
        return None

    def _get_upstream_nodes_if_parent(self, join_id: T, fork_id: T) -> set[T] | None:
        """Return the set of node‑ids that can reach the join (J) in the graph where the node `fork_id` is removed.

        If, in that pruned graph, a path exists that starts and ends at J
        (i.e. J is on a cycle that avoids the provided node) we return `None` instead,
        because the fork would not be a valid "parent fork".
        """
        upstream: set[T] = set()
        stack = [join_id]
        while stack:
            v = stack.pop()
            for p in self._predecessors[v]:
                if p == fork_id:
                    continue
                if p == join_id:
                    return None  # J sits on a cycle w/out the specified node
                if p not in upstream:
                    upstream.add(p)
                    stack.append(p)
        return upstream


def main_test():
    """Basic smoke test of the functionality."""
    join_id = 'J'
    nodes = {'start', 'A', 'B', 'C', 'F', 'F2', 'I', 'J', 'end'}
    start_ids = {'start'}
    fork_ids = {'F', 'F2'}
    valid_edges = {
        'start': ['F2'],
        'F2': ['I'],
        'I': ['F'],
        'F': ['A', 'B'],
        'A': ['J'],
        'B': ['J'],
        'J': ['C'],
        'C': ['end', 'I'],
    }
    invalid_edges = deepcopy(valid_edges)
    invalid_edges['C'].append('A')

    print(ParentForkFinder(nodes, start_ids, fork_ids, valid_edges).find_parent_fork(join_id))
    # > DominatingFork(fork_id='F', intermediate_nodes={'A', 'B'})
    print(ParentForkFinder(nodes, start_ids, fork_ids, invalid_edges).find_parent_fork(join_id))
    # > None


if __name__ == '__main__':
    main_test()
