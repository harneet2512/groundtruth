"""Delivery-surface fact-filter policy — the SINGLE SOURCE for both the Product
brief (``pretask/v1r_brief.py``) and the Integration delivery hook
(``artifact_deepswe/gt_mini_patch.py``).

Two halves:
  * ``path_policy`` — vendored / minified / generated PATH classification.
  * ``name_policy`` — builtin/dunder-shadow + stdlib-shadow NAME classification.

The FACT gate (``DETERMINISTIC_RESOLUTION_METHODS``) and the cross-language
disqualifier (``_is_cross_language_pair``) live in ``pretask/curation_map.py``
(their existing Product home) and are imported directly from there by both
consumers — they are NOT re-homed here, to avoid creating a second source.

Import everything delivery-policy from this one package so a change reaches the
proof-time brief AND agent-time delivery from a single edit (the B1 invariant).
"""

from groundtruth.delivery.name_policy import (
    BUILTIN_CALLABLE_NAMES,
    STDLIB_MODULES,
    is_builtin_shadow_name,
    is_stdlib_shadow,
)
from groundtruth.delivery.path_policy import (
    is_delivery_excluded,
    is_generated,
    is_minified_file,
    is_vendored_path,
)

__all__ = [
    "is_vendored_path",
    "is_generated",
    "is_delivery_excluded",
    "is_minified_file",
    "BUILTIN_CALLABLE_NAMES",
    "STDLIB_MODULES",
    "is_builtin_shadow_name",
    "is_stdlib_shadow",
]
