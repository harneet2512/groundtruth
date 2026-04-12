"""Language configuration for tree-sitter structural checking.

Migrated from groundtruth_v2/lang_config.py. This is a DATA TABLE —
adding a language = adding one dict entry. No behavioral logic here.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LangParamConfig:
    """Tree-sitter node type config for parameter extraction."""

    grammar_loader: str
    """Module name, e.g. 'tree_sitter_python'."""

    func_def_types: tuple[str, ...]
    """Node types for function/method definitions."""

    param_list_types: tuple[str, ...]
    """Node type for the parameter list container."""

    param_types: tuple[str, ...]
    """Node types that represent individual parameters."""

    default_param_types: tuple[str, ...]
    """Node types for parameters with default values (optional)."""

    self_param: str | None
    """Self/this parameter to skip (None if language doesn't have one)."""

    import_node_types: tuple[str, ...] = ()
    """Import statement node types."""

    call_node_types: tuple[str, ...] = ()
    """Call expression node types."""


LANGUAGE_CONFIGS: dict[str, LangParamConfig] = {
    "python": LangParamConfig(
        grammar_loader="tree_sitter_python",
        func_def_types=("function_definition",),
        param_list_types=("parameters",),
        param_types=(
            "identifier", "typed_parameter",
            "default_parameter", "typed_default_parameter",
        ),
        default_param_types=("default_parameter", "typed_default_parameter"),
        self_param="self",
        import_node_types=("import_statement", "import_from_statement"),
        call_node_types=("call",),
    ),
    "javascript": LangParamConfig(
        grammar_loader="tree_sitter_javascript",
        func_def_types=(
            "function_declaration", "method_definition", "arrow_function",
        ),
        param_list_types=("formal_parameters",),
        param_types=(
            "identifier", "assignment_pattern",
            "rest_parameter", "object_pattern", "array_pattern",
        ),
        default_param_types=("assignment_pattern",),
        self_param=None,
        import_node_types=("import_statement",),
        call_node_types=("call_expression",),
    ),
    "typescript": LangParamConfig(
        grammar_loader="tree_sitter_javascript",
        func_def_types=(
            "function_declaration", "method_definition", "arrow_function",
        ),
        param_list_types=("formal_parameters",),
        param_types=(
            "identifier", "assignment_pattern", "rest_parameter",
            "required_parameter", "optional_parameter",
        ),
        default_param_types=("assignment_pattern", "optional_parameter"),
        self_param=None,
        import_node_types=("import_statement",),
        call_node_types=("call_expression",),
    ),
    "go": LangParamConfig(
        grammar_loader="tree_sitter_go",
        func_def_types=("function_declaration", "method_declaration"),
        param_list_types=("parameter_list",),
        param_types=("parameter_declaration",),
        default_param_types=(),
        self_param=None,
        import_node_types=("import_declaration",),
        call_node_types=("call_expression",),
    ),
    "java": LangParamConfig(
        grammar_loader="tree_sitter_java",
        func_def_types=("method_declaration", "constructor_declaration"),
        param_list_types=("formal_parameters",),
        param_types=("formal_parameter", "spread_parameter"),
        default_param_types=(),
        self_param=None,
        import_node_types=("import_declaration",),
        call_node_types=("method_invocation",),
    ),
    "rust": LangParamConfig(
        grammar_loader="tree_sitter_rust",
        func_def_types=("function_item",),
        param_list_types=("parameters",),
        param_types=("parameter", "self_parameter"),
        default_param_types=(),
        self_param="self_parameter",
        import_node_types=("use_declaration",),
        call_node_types=("call_expression",),
    ),
    "ruby": LangParamConfig(
        grammar_loader="tree_sitter_ruby",
        func_def_types=("method", "singleton_method"),
        param_list_types=("method_parameters",),
        param_types=(
            "identifier", "optional_parameter",
            "splat_parameter", "keyword_parameter",
        ),
        default_param_types=("optional_parameter",),
        self_param=None,
        import_node_types=(),
        call_node_types=("call", "method_call"),
    ),
    "c": LangParamConfig(
        grammar_loader="tree_sitter_c",
        func_def_types=("function_definition",),
        param_list_types=("parameter_list",),
        param_types=("parameter_declaration",),
        default_param_types=(),
        self_param=None,
        import_node_types=("preproc_include",),
        call_node_types=("call_expression",),
    ),
    "cpp": LangParamConfig(
        grammar_loader="tree_sitter_cpp",
        func_def_types=("function_definition",),
        param_list_types=("parameter_list",),
        param_types=("parameter_declaration", "optional_parameter_declaration"),
        default_param_types=("optional_parameter_declaration",),
        self_param=None,
        import_node_types=("preproc_include", "using_declaration"),
        call_node_types=("call_expression",),
    ),
    "php": LangParamConfig(
        grammar_loader="tree_sitter_php",
        func_def_types=("function_definition", "method_declaration"),
        param_list_types=("formal_parameters",),
        param_types=(
            "simple_parameter", "variadic_parameter",
            "property_promotion_parameter",
        ),
        default_param_types=(),
        self_param=None,
        import_node_types=("namespace_use_declaration",),
        call_node_types=(
            "function_call_expression", "member_call_expression",
            "scoped_call_expression",
        ),
    ),
}

# Extension → language mapping
EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".rake": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".php": "php",
}


def load_grammar(lang: str) -> Any:
    """Load and return the tree-sitter Language pointer for a given language."""
    config = LANGUAGE_CONFIGS.get(lang)
    if not config:
        return None
    try:
        mod = importlib.import_module(config.grammar_loader)
        return mod.language()
    except (ImportError, AttributeError):
        return None
