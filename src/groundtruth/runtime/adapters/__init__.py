"""Seam adapters for the GT Gateway (W2).

Each adapter wires ``groundtruth.runtime.gateway`` onto ONE agent harness with a
single observation-constructor call. The adapters are PURE and hold no harness
import; the harness-side seam does the observation splice and injects any
harness-native renderer it wants to reuse.
"""
