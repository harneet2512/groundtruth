"""Fixture module 'db' — the SECOND def site of late_probe, making the symbol
ambiguous (pkg/io.py + pkg/db.py) so grep late_probe emits a def/ref
localization partition (the S6 late-drop probe). 2-char stem: see pkg/io.py."""


def late_probe(x):
    return x - 1
