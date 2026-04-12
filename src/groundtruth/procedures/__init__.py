"""Procedure Priors Engine — structured repair know-how.

Distills repair knowledge into structured procedures without injecting
noisy raw memory. Only verified, clustered procedures with empirical
support may enter the runtime prompt path.
"""

from groundtruth.procedures.models import AntiPattern, ProcedureCard, ValidationPlan

__all__ = ["AntiPattern", "ProcedureCard", "ValidationPlan"]
