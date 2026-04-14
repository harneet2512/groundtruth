"""DispatchRegistrationExtractor -- mines live behavior routing contracts.

This family captures the obligation that when a symbol is registered in one
routing structure (registry dict, dispatch map, decorator, __all__, plugin
entry point), it must remain consistently reachable through that routing
surface.

This is distinct from RegistryCouplingExtractor which detects file-level
coupling to registry files. This extractor focuses on live behavior routing:
the symbol must remain callable through the dispatch path that references it.

Detection signals (language-agnostic)
--------------------------------------
1. Callers that look like dispatch/registry functions
   - function names containing: register, dispatch, lookup, resolve, route,
     handler, adapter, plugin, add_*, install_*
   - properties with kind "registry_key" or "dispatch_key"

2. Graph edges from __all__-like exports or entry-point files
   - caller file is an __init__.py, setup.py, or entry_points file
   - caller function is named: register, add_handler, install, setup, plugin

3. Sibling symbols in the same dispatch map
   - if >= 2 siblings share a common caller that dispatches by key,
     all siblings inherit the dispatch coupling obligation

Support kinds and promotion
---------------------------
  callers (dispatch callers) + structure (same-scope siblings) → likely or verified
  callers only                                                  → likely
  No naming-only heuristics.
"""

from __future__ import annotations

import os
import re

from groundtruth.substrate.promotion import promote_tier
from groundtruth.substrate.types import ContractRecord, SupportKind

_DISPATCH_NAME_PATTERNS = re.compile(
    r"(register|dispatch|lookup|resolve|route|handler|adapter|plugin|"
    r"add_\w+|install_\w+|setup_\w+|entry_point)",
    re.IGNORECASE,
)

_REGISTRY_FILE_PATTERNS = re.compile(
    r"(__init__|setup|entry_points?|registry|plugin|dispatcher|router|handler)",
    re.IGNORECASE,
)


class DispatchRegistrationExtractor:
    """Extract live dispatch/registration coupling contracts."""

    contract_type = "dispatch_registration"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        support_kinds: list[SupportKind] = []
        dispatch_callers: list[dict] = []   # strong: dispatch-named function callers
        registry_callers: list[dict] = []   # weak: registry-file callers only
        dispatch_sources: list[str] = []

        # 1. Detect dispatch callers — separate function-level (strong) from file-level (weak)
        for caller in reader.get_callers(node_id):
            source_file = caller.get("source_file", "")
            source_id = caller.get("source_id")

            is_dispatch_caller = False  # function name matches dispatch patterns
            has_registry_key = False    # explicit registry_key / dispatch_key property
            is_dispatch_file = bool(    # only file-level signal
                _REGISTRY_FILE_PATTERNS.search(os.path.basename(source_file))
            )

            if source_id is not None:
                caller_node = reader.get_node_by_id(source_id)
                if caller_node:
                    caller_name = caller_node.get("name", "")
                    is_dispatch_caller = bool(_DISPATCH_NAME_PATTERNS.search(caller_name))

                    # Check for registry_key / dispatch_key property — strong signal
                    for prop in reader.get_properties(source_id, kind=None):
                        if prop.get("kind") in {"registry_key", "dispatch_key"}:
                            has_registry_key = True
                            break

                    # 4b. String-key routing: target name appears as string literal in caller body
                    if not is_dispatch_caller and not has_registry_key:
                        for prop in reader.get_properties(source_id, kind="string_literal"):
                            if prop.get("value") == name:
                                is_dispatch_caller = True  # upgrade: string-key routing
                                break

            if is_dispatch_caller or has_registry_key:
                # Strong: function-level dispatch — counts toward "callers" support class
                dispatch_callers.append(caller)
                dispatch_sources.append(f"{source_file}:{caller.get('source_line', 0)}")
            elif is_dispatch_file:
                # Weak: file-level signal only — counts toward "structure" support class
                registry_callers.append(caller)

        if not dispatch_callers and not registry_callers:
            return []

        # Only function-level dispatch callers contribute to "callers" evidence class
        if dispatch_callers:
            support_kinds.append("callers")
            dispatch_sources.extend(
                f"{c.get('source_file', '')}:{c.get('source_line', 0)}"
                for c in registry_callers[:2]
            )
        elif registry_callers:
            # File-level only → structure class (lower weight)
            support_kinds.append("structure")
            dispatch_sources.extend(
                f"{c.get('source_file', '')}:{c.get('source_line', 0)}"
                for c in registry_callers
            )

        # 2. Check for sibling symbols registered through the SAME dispatch caller (not just any)
        same_dispatcher_siblings = self._count_dispatch_siblings(
            reader, node_id, dispatch_callers
        )
        if same_dispatcher_siblings >= 2:
            support_kinds.append("structure")

        # 3. Check if any function-level dispatch caller is in an entry-point/registry file
        if any(
            _REGISTRY_FILE_PATTERNS.search(os.path.basename(c.get("source_file", "")))
            for c in dispatch_callers
        ):
            support_kinds.append("docs_or_config")

        tier = promote_tier(support_kinds)
        support_count = len({s.split(":")[0] for s in dispatch_sources}) or len(dispatch_sources)

        # Build a representative dispatcher name for the predicate
        dispatcher_hint = _dispatcher_hint(dispatch_callers)

        return [
            ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=qualified,
                predicate=(
                    f"{name} must remain registered/reachable via dispatch path"
                    + (f" ({dispatcher_hint})" if dispatcher_hint else "")
                ),
                normalized_form=f"dispatch_registration:{qualified}:{dispatcher_hint}",
                support_sources=tuple(dispatch_sources[:5]),
                support_count=support_count,
                confidence=_confidence_from_tier(tier),
                tier=tier,
                support_kinds=tuple(dict.fromkeys(support_kinds)),
                scope_file=node.get("file_path"),
                checkable=True,  # presence-check is machine-checkable
                freshness_state="unknown",
            )
        ]

    def _count_dispatch_siblings(
        self, reader, node_id: int, dispatch_callers: list[dict]  # noqa: ANN001
    ) -> int:
        """Count siblings (same parent) that share the SAME dispatch caller node.

        Requires siblings to be called by the SAME caller node_id, not just any
        caller that happens to be in our dispatch caller set. This prevents
        over-counting when multiple unrelated dispatch functions exist.
        """
        # Build map: caller_node_id → set of sibling node_ids it calls
        dispatcher_ids: set[int] = {
            int(c["source_id"])
            for c in dispatch_callers
            if c.get("source_id") is not None
        }
        if not dispatcher_ids:
            return 0

        node = reader.get_node_by_id(node_id)
        if not node:
            return 0

        parent_id = node.get("parent_id")
        file_path = node.get("file_path", "")

        # For each sibling, check if it's called by the SAME dispatcher as this node
        sibling_count = 0
        for candidate in reader.get_nodes_in_file(file_path):
            cid = candidate.get("id")
            if cid is None or cid == node_id:
                continue
            if candidate.get("parent_id") != parent_id:
                continue
            # Only count if sibling is called by one of the SAME dispatcher node_ids
            sibling_dispatcher_ids = {
                caller.get("source_id")
                for caller in reader.get_callers(cid)
                if caller.get("source_id") is not None
            }
            if dispatcher_ids & sibling_dispatcher_ids:
                sibling_count += 1

        return sibling_count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dispatcher_hint(callers: list[dict]) -> str:
    """Build a short hint string from the first dispatch caller."""
    if not callers:
        return ""
    first = callers[0]
    src = first.get("source_file", "")
    line = first.get("source_line", 0)
    basename = os.path.basename(src)
    return f"{basename}:{line}" if basename else ""


def _confidence_from_tier(tier: str) -> float:
    if tier == "verified":
        return 0.90
    if tier == "likely":
        return 0.78
    return 0.55


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
