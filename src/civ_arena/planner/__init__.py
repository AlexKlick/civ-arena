"""The planner lane (M15+): belief-state modeling and option search.

Everything under this package operates on PROJECTED observations only —
the same docs an agent receives through the tool facade. Nothing here may
import adapters, the arena, or sessions; fairness is structural, then
test-pinned (tests/test_belief.py).
"""
