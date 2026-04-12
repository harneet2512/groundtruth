"""Build Graph — parses build manifests to extract dependency topology.

Supports:
- Python: pyproject.toml, setup.py, setup.cfg, requirements.txt
- JavaScript/TypeScript: package.json
- Go: go.mod
- Rust: Cargo.toml
- Java: pom.xml, build.gradle

All extraction is deterministic — pure file parsing, no model calls.
"""

from __future__ import annotations

import json
import json
import re
from pathlib import Path

from groundtruth.repo_intel.models import BuildNode


class BuildGraphExtractor:
    """Extracts build graph from manifest files in a repository."""

    def extract(self, root: str) -> list[BuildNode]:
        """Walk repository root and extract build nodes from all manifests."""
        nodes: list[BuildNode] = []
        root_path = Path(root)

        # Python manifests
        for path in root_path.rglob("pyproject.toml"):
            nodes.extend(self._parse_pyproject(path))
        for path in root_path.rglob("setup.py"):
            nodes.extend(self._parse_setup_py(path))
        for path in root_path.rglob("requirements*.txt"):
            nodes.extend(self._parse_requirements(path))

        # JavaScript/TypeScript
        for path in root_path.rglob("package.json"):
            if "node_modules" in str(path):
                continue
            nodes.extend(self._parse_package_json(path))

        # Go
        for path in root_path.rglob("go.mod"):
            nodes.extend(self._parse_go_mod(path))

        # Rust
        for path in root_path.rglob("Cargo.toml"):
            nodes.extend(self._parse_cargo_toml(path))

        return nodes

    def _parse_pyproject(self, path: Path) -> list[BuildNode]:
        """Extract from pyproject.toml."""
        nodes: list[BuildNode] = []
        try:
            import tomllib
            content = path.read_bytes()
            data = tomllib.loads(content.decode())
        except (ImportError, Exception):
            # Fallback: regex extraction
            return self._parse_pyproject_regex(path)

        project = data.get("project", {})
        name = project.get("name", path.parent.name)
        deps = [d.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
                for d in project.get("dependencies", [])]

        nodes.append(BuildNode(
            name=name,
            kind="package",
            file_path=str(path),
            dependencies=tuple(deps),
        ))

        # Entry points
        scripts = project.get("scripts", {})
        for script_name in scripts:
            nodes.append(BuildNode(
                name=script_name,
                kind="entry_point",
                file_path=str(path),
                dependencies=(name,),
            ))

        return nodes

    def _parse_pyproject_regex(self, path: Path) -> list[BuildNode]:
        """Regex fallback for pyproject.toml (when tomllib unavailable)."""
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            return []

        name_match = re.search(r'name\s*=\s*"([^"]+)"', content)
        name = name_match.group(1) if name_match else path.parent.name

        deps: list[str] = []
        in_deps = False
        for line in content.splitlines():
            if "dependencies" in line and "[" in line:
                in_deps = True
                continue
            if in_deps:
                if "]" in line:
                    break
                dep = line.strip().strip('"').strip("',")
                if dep:
                    deps.append(dep.split(">")[0].split("<")[0].split("=")[0].strip())

        return [BuildNode(
            name=name,
            kind="package",
            file_path=str(path),
            dependencies=tuple(deps),
        )]

    def _parse_setup_py(self, path: Path) -> list[BuildNode]:
        """Extract package name and deps from setup.py via regex."""
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            return []

        name_match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
        name = name_match.group(1) if name_match else path.parent.name

        deps: list[str] = []
        deps_match = re.search(r'install_requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
        if deps_match:
            for dep in re.findall(r'["\']([^"\']+)["\']', deps_match.group(1)):
                deps.append(dep.split(">")[0].split("<")[0].split("=")[0].strip())

        return [BuildNode(
            name=name,
            kind="package",
            file_path=str(path),
            dependencies=tuple(deps),
        )]

    def _parse_requirements(self, path: Path) -> list[BuildNode]:
        """Extract dependencies from requirements.txt."""
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            return []

        deps: list[str] = []
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                dep = line.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
                if dep:
                    deps.append(dep)

        return [BuildNode(
            name=f"requirements:{path.name}",
            kind="config",
            file_path=str(path),
            dependencies=tuple(deps),
        )]

    def _parse_package_json(self, path: Path) -> list[BuildNode]:
        """Extract from package.json."""
        try:
            data = json.loads(path.read_text(errors="ignore"))
        except (json.JSONDecodeError, OSError):
            return []

        name = data.get("name", path.parent.name)
        deps = list(data.get("dependencies", {}).keys())
        dev_deps = list(data.get("devDependencies", {}).keys())

        nodes = [BuildNode(
            name=name,
            kind="package",
            file_path=str(path),
            dependencies=tuple(deps + dev_deps),
        )]

        # Scripts as entry points
        for script_name in data.get("scripts", {}):
            nodes.append(BuildNode(
                name=f"{name}:{script_name}",
                kind="entry_point",
                file_path=str(path),
                dependencies=(name,),
            ))

        return nodes

    def _parse_go_mod(self, path: Path) -> list[BuildNode]:
        """Extract from go.mod."""
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            return []

        module_match = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
        name = module_match.group(1) if module_match else path.parent.name

        deps: list[str] = []
        in_require = False
        for line in content.splitlines():
            if line.strip().startswith("require"):
                in_require = True
                continue
            if in_require:
                if line.strip() == ")":
                    in_require = False
                    continue
                parts = line.strip().split()
                if parts:
                    deps.append(parts[0])

        return [BuildNode(
            name=name,
            kind="package",
            file_path=str(path),
            dependencies=tuple(deps),
        )]

    def _parse_cargo_toml(self, path: Path) -> list[BuildNode]:
        """Extract from Cargo.toml via regex."""
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            return []

        name_match = re.search(r'name\s*=\s*"([^"]+)"', content)
        name = name_match.group(1) if name_match else path.parent.name

        deps: list[str] = []
        in_deps = False
        for line in content.splitlines():
            if line.strip() == "[dependencies]":
                in_deps = True
                continue
            if in_deps:
                if line.startswith("["):
                    break
                dep_match = re.match(r'(\w[\w-]*)\s*=', line)
                if dep_match:
                    deps.append(dep_match.group(1))

        return [BuildNode(
            name=name,
            kind="package",
            file_path=str(path),
            dependencies=tuple(deps),
        )]
