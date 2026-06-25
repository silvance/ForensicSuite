"""Pytest helpers shared across the suite's per-package test runs.

The three apps (and suite_common itself) each have a ``tests/``
directory with its own ``conftest.py``. Anything that has to be
configured exactly once per test session -- the Qt offscreen platform,
shared fixtures -- lives here so the four conftest.py files stay
trivial.
"""
