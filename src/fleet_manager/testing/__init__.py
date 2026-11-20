"""Helpers for tests and simulations, not used by the router or the node.

Shipped in the package so the test suite and ``scripts/fake_fleet.py`` build
heartbeats with one definition: a fake node that drifted from the fixtures
would measure a router no test describes.  Nothing here imports pytest.
"""
