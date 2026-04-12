"""contracts.py — Compute implicit contracts from caller usage patterns.

A contract is a behavioral rule mined from the call graph: if 80%+ of
callers share a usage pattern (destructure, iterate, boolean check, etc.),
that pattern IS the contract. Changing it breaks callers.

All computation reads graph.db via GraphReader. Language-blind.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundtruth_v2.graph import GraphReader


@dataclass(frozen=True, slots=True)
class ContractTerm:
    """A single contract term mined from caller usage."""

    pattern: str  # e.g., "destructure_tuple", "boolean_check"
    count: int  # how many callers exhibit this pattern
    total: int  # total callers with any classified usage
    fraction: float  # count / total


@dataclass(frozen=True, slots=True)
class Contract:
    """Implicit behavioral contract derived from caller usage patterns."""

    terms: tuple[ContractTerm, ...]
    total_callers: int
    classified_callers: int  # callers with usage classification
    unclassified_callers: int  # callers without usage classification


@dataclass(frozen=True, slots=True)
class Obligation:
    """A must-preserve requirement derived from the contract + caller graph."""

    description: str  # human-readable, e.g., "Return type must remain iterable"
    affected_callers: int  # how many callers break if violated
    evidence: str  # e.g., "7/8 callers iterate the return value"


@dataclass(frozen=True, slots=True)
class SiblingPattern:
    """Common patterns across sibling functions (same class/module)."""

    sibling_count: int
    common_return_type: str | None  # if >50% share a return type
    return_type_agreement: float  # fraction sharing the most common return type
    common_signatures: list[str]  # sample signatures for reference


# ── Contract computation ──────────────────────────────────────────────


_CONTRACT_THRESHOLD = 0.8  # 80%+ pattern = contract term


def compute_contract(reader: GraphReader, node_id: int) -> Contract:
    """Aggregate caller usage patterns into an implicit contract.

    Reads caller_usage properties for all callers of the target node.
    If >=80% of classified callers share a usage pattern, it becomes
    a contract term.
    """
    callers = reader.get_callers(node_id, deterministic_only=True)
    total = len(callers)

    # Count usage patterns
    usage_counts: dict[str, int] = {}
    classified = 0
    for c in callers:
        if c.usage:
            classified += 1
            usage_counts[c.usage] = usage_counts.get(c.usage, 0) + 1

    # Build contract terms from patterns meeting the threshold
    # v1.0.4: 2-caller minimum floor — skip patterns seen in only 1 caller
    terms: list[ContractTerm] = []
    if classified > 0:
        for pattern, count in sorted(
            usage_counts.items(), key=lambda x: -x[1]
        ):
            if count < 2:
                continue  # v1.0.4: require at least 2 callers for any pattern
            fraction = count / classified
            terms.append(
                ContractTerm(
                    pattern=pattern,
                    count=count,
                    total=classified,
                    fraction=fraction,
                )
            )

    return Contract(
        terms=tuple(terms),
        total_callers=total,
        classified_callers=classified,
        unclassified_callers=total - classified,
    )


def compute_obligations(
    reader: GraphReader, node_id: int
) -> list[Obligation]:
    """Derive must-preserve requirements from the contract + caller count.

    Obligations are things that WILL break callers if changed:
    - Signature arity (all callers pass N args)
    - Return type usage (callers destructure, iterate, etc.)
    - Exception contract (callers catch specific exceptions)
    """
    callers = reader.get_callers(node_id, deterministic_only=True)
    contract = compute_contract(reader, node_id)
    node = reader.get_node_by_id(node_id)
    obligations: list[Obligation] = []

    if not callers or not node:
        return obligations

    # Obligation 1: Signature arity
    # All callers pass the same number of args (from the signature)
    if node.signature:
        obligations.append(
            Obligation(
                description=(
                    f"Signature must remain compatible: {node.signature[:80]}"
                ),
                affected_callers=len(callers),
                evidence=f"{len(callers)} callers depend on current signature",
            )
        )

    # Obligation 2: Return type usage from contract terms
    for term in contract.terms:
        if term.fraction >= _CONTRACT_THRESHOLD:
            desc = _obligation_from_usage(term.pattern, term.count, term.total)
            if desc:
                obligations.append(
                    Obligation(
                        description=desc,
                        affected_callers=term.count,
                        evidence=(
                            f"{term.count}/{term.total} callers {term.pattern} "
                            "the return value"
                        ),
                    )
                )

    # Obligation 3: Return-shape contract from specific caller usage
    # v6: Check what callers do with the return value beyond generic patterns
    # If callers index, iterate, or pass to typed APIs, the return shape is contractual
    usage_details = []
    for c in callers:
        if c.usage:
            # Extract the specific callee from usage like "destructure_tuple:np.dot"
            parts = c.usage.split(":", 1)
            if len(parts) == 2 and parts[1] != node.name:
                usage_details.append((parts[0], parts[1], c.source_file))
    if usage_details:
        # Group by usage pattern + downstream callee
        from collections import Counter as _Counter
        downstream = _Counter((u[0], u[1]) for u in usage_details)
        for (pattern, callee), count in downstream.most_common(3):
            if count >= 2:
                obligations.append(
                    Obligation(
                        description=f"Return value passed to {callee}() by {count} callers — shape must be compatible",
                        affected_callers=count,
                        evidence=f"{count} callers use {pattern} then call {callee}()",
                    )
                )

    # Obligation 4: Exception contract from properties
    exception_props = reader._conn.execute(
        "SELECT value, COUNT(*) FROM properties "
        "WHERE node_id = ? AND kind = 'exception_type' "
        "GROUP BY value",
        (node_id,),
    ).fetchall()
    for exc_type, count in exception_props:
        # Check if callers have exception_guard usage
        guard_callers = sum(1 for c in callers if c.usage == "exception_guard")
        if guard_callers > 0:
            obligations.append(
                Obligation(
                    description=f"Must continue raising {exc_type}",
                    affected_callers=guard_callers,
                    evidence=(
                        f"{guard_callers} callers catch exceptions from this function"
                    ),
                )
            )

    return obligations


def _obligation_from_usage(pattern: str, count: int, total: int) -> str | None:
    """Convert a usage pattern into a human-readable obligation."""
    match pattern:
        case "destructure_tuple":
            return f"Return type must remain destructurable ({count}/{total} callers destructure)"
        case "iterated":
            return f"Return type must remain iterable ({count}/{total} callers iterate)"
        case "boolean_check":
            return f"Return value truthiness must be preserved ({count}/{total} callers use in conditionals)"
        case "exception_guard":
            return f"Exception behavior must be preserved ({count}/{total} callers guard with try/catch)"
        case _:
            return None


def compute_sibling_pattern(
    reader: GraphReader, node_id: int
) -> SiblingPattern | None:
    """Find common patterns across sibling functions (same parent).

    Useful for: "12/15 Field.clean() methods raise ValidationError on failure"
    """
    siblings = reader.get_siblings(node_id)
    if not siblings:
        return None

    # Analyze return types
    return_types: dict[str, int] = {}
    signatures: list[str] = []
    for s in siblings:
        if s.return_type:
            return_types[s.return_type] = return_types.get(s.return_type, 0) + 1
        if s.signature and len(signatures) < 3:
            signatures.append(s.signature[:80])

    common_rt = None
    rt_agreement = 0.0
    if return_types:
        most_common = max(return_types.items(), key=lambda x: x[1])
        common_rt = most_common[0]
        rt_agreement = most_common[1] / len(siblings)

    return SiblingPattern(
        sibling_count=len(siblings),
        common_return_type=common_rt,
        return_type_agreement=rt_agreement,
        common_signatures=signatures,
    )
