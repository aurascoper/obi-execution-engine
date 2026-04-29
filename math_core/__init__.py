"""math_core — pure mathematical primitives.

Modules here contain ONLY numerical / mathematical implementations with no
dependency on engine state, order paths, or live config. Imported freely by
analysis scripts; deliberately kept independent of `strategy/` so trajectory
sandbox tests can run without risking accidental wiring into the live
order-decision path.
"""
