"""
Language server configuration for GT edge resolution.

The ONLY place language-specific information lives.
Adding a new language = adding one entry to LSP_SERVERS.
The LSP client code never changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path


# Extension → server config. The LSP client reads this dict, nothing else.
LSP_SERVERS: dict[str, dict[str, object]] = {
    # Tier 1: Production quality, maintained by major organizations
    ".py":    {"cmd": "pyright-langserver", "args": ["--stdio"], "install": "pip install pyright"},
    ".ts":    {"cmd": "typescript-language-server", "args": ["--stdio"], "install": "npm i -g typescript-language-server typescript"},
    ".tsx":   {"cmd": "typescript-language-server", "args": ["--stdio"], "install": "npm i -g typescript-language-server typescript"},
    ".js":    {"cmd": "typescript-language-server", "args": ["--stdio"], "install": "npm i -g typescript-language-server typescript"},
    ".jsx":   {"cmd": "typescript-language-server", "args": ["--stdio"], "install": "npm i -g typescript-language-server typescript"},
    ".go":    {"cmd": "gopls", "args": ["serve"], "install": "go install golang.org/x/tools/gopls@latest"},
    ".rs":    {"cmd": "rust-analyzer", "args": [], "install": "rustup component add rust-analyzer"},
    ".java":  {"cmd": "jdtls", "args": [], "install": "see eclipse.org/jdtls"},
    ".cs":    {"cmd": "OmniSharp", "args": ["--languageserver"], "install": "dotnet tool install -g omnisharp"},
    ".c":     {"cmd": "clangd", "args": [], "install": "apt install clangd"},
    ".cpp":   {"cmd": "clangd", "args": [], "install": "apt install clangd"},
    ".cc":    {"cmd": "clangd", "args": [], "install": "apt install clangd"},
    ".h":     {"cmd": "clangd", "args": [], "install": "apt install clangd"},
    ".hpp":   {"cmd": "clangd", "args": [], "install": "apt install clangd"},
    # Tier 2: Good quality, community maintained
    ".rb":    {"cmd": "solargraph", "args": ["stdio"], "install": "gem install solargraph"},
    ".php":   {"cmd": "phpactor", "args": ["language-server"], "install": "composer global require phpactor"},
    ".kt":    {"cmd": "kotlin-language-server", "args": [], "install": "github.com/fwcd/kotlin-language-server/releases"},
    ".swift": {"cmd": "sourcekit-lsp", "args": [], "install": "ships with Swift toolchain"},
    ".lua":   {"cmd": "lua-language-server", "args": [], "install": "brew install lua-language-server"},
    ".scala": {"cmd": "metals", "args": [], "install": "cs install metals"},
    ".ex":    {"cmd": "elixir-ls", "args": [], "install": "mix compile"},
    ".exs":   {"cmd": "elixir-ls", "args": [], "install": "mix compile"},
    ".hs":    {"cmd": "haskell-language-server-wrapper", "args": ["--lsp"], "install": "ghcup install hls"},
    ".dart":  {"cmd": "dart", "args": ["language-server", "--protocol=lsp"], "install": "ships with Dart SDK"},
    ".r":     {"cmd": "R", "args": ["--slave", "-e", "languageserver::run()"], "install": "R -e 'install.packages(\"languageserver\")'"},
    ".R":     {"cmd": "R", "args": ["--slave", "-e", "languageserver::run()"], "install": "R -e 'install.packages(\"languageserver\")'"},
    ".jl":    {"cmd": "julia", "args": ["--startup-file=no", "-e", "using LanguageServer; runserver()"], "install": "julia -e 'using Pkg; Pkg.add(\"LanguageServer\")'"},
    ".zig":   {"cmd": "zls", "args": [], "install": "zig build or binary download"},
    ".ml":    {"cmd": "ocamllsp", "args": [], "install": "opam install ocaml-lsp-server"},
    ".sh":    {"cmd": "bash-language-server", "args": ["start"], "install": "npm i -g bash-language-server"},
    ".bash":  {"cmd": "bash-language-server", "args": ["start"], "install": "npm i -g bash-language-server"},
    # Tier 3: Available
    ".pl":    {"cmd": "perlnavigator", "args": ["--stdio"], "install": "npm i -g perlnavigator-server"},
    ".f90":   {"cmd": "fortls", "args": [], "install": "pip install fortls"},
    ".clj":   {"cmd": "clojure-lsp", "args": [], "install": "binary download from github"},
    ".erl":   {"cmd": "erlang_ls", "args": [], "install": "build from source"},
}

# Extension → LSP language identifier. Separate from server config
# so the client can use it without knowing which server handles the language.
LANG_IDS: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascriptreact",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".cs": "csharp", ".c": "c", ".cpp": "cpp", ".cc": "cpp",
    ".h": "c", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin",
    ".swift": "swift", ".lua": "lua", ".scala": "scala",
    ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell", ".dart": "dart",
    ".r": "r", ".R": "r", ".jl": "julia",
    ".zig": "zig", ".ml": "ocaml",
    ".sh": "shellscript", ".bash": "shellscript",
    ".pl": "perl", ".f90": "fortran",
    ".clj": "clojure", ".erl": "erlang",
}


def detect_installed_servers(extensions_in_repo: set[str] | None = None) -> dict[str, dict[str, object]]:
    """
    Detect which LSP servers are installed for the given extensions.

    Args:
        extensions_in_repo: Set of file extensions present in the repo.
            If None, checks all known extensions.

    Returns:
        Dict of extension → server config for extensions with an installed server.
    """
    to_check = extensions_in_repo if extensions_in_repo is not None else set(LSP_SERVERS.keys())
    available: dict[str, dict[str, object]] = {}
    for ext in to_check:
        config = LSP_SERVERS.get(ext)
        if config is None:
            continue
        cmd = str(config["cmd"])
        if shutil.which(cmd):
            available[ext] = config
    return available


def get_language_id(file_path: str) -> str:
    """Return the LSP language identifier for a file, or the bare extension."""
    ext = Path(file_path).suffix.lower()
    return LANG_IDS.get(ext, ext.lstrip("."))
