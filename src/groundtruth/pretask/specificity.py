"""Backwards-compat shim — the real implementation now lives in
``groundtruth.confidence`` (the centralized single source of the dynamic +
hybrid + confidence-gated primitives). This module's hand-rolled specificity was
superseded by the research-backed BM25/RSJ-IDF version in confidence.py
(BugLocator ICSE 2012 / BLUiR ASE 2013 / Robertson & Zaragoza FnTIR 2009).

Kept only as a re-export so any caller that imported ``pretask.specificity``
keeps working against the canonical implementation. DO NOT add logic here — add
it to confidence.py.
"""
from __future__ import annotations

from groundtruth.confidence import clear_cache, symbol_specificity

__all__ = ["symbol_specificity", "clear_cache"]
