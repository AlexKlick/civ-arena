"""The M12 graph lane: deterministic projection of the event log into a
Graphiti-compatible graph — artifacts are the portable truth, any DB loaded
from them is a derived, rebuildable index.

NB: the projection ENTRY POINT is ``civ_arena.graph.projection.project`` and
the CLI module is ``civ_arena.graph.project`` — the function is deliberately
NOT re-exported here so the submodule name stays unambiguous.
"""

from civ_arena.graph.projection import Projection

__all__ = ["Projection"]
