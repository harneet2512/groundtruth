"""Fixture module 'io' — one def site of the ambiguous late_probe symbol (S6).
The 2-char stem is DELIBERATE: it is too short to form a dotted path-symbol, so
the def/ref partition surfaces ONLY the bare 'late_probe' code identifier (the
late-drop subject a passing `pytest -k late_probe` can seed into the pass-tokens)."""


def late_probe(x):
    return x + 1
