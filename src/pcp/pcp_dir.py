"""Locate and navigate the .pcp/ directory for a project."""

from pathlib import Path


class NoPCPDir(Exception):
    pass


def find_pcp_dir(start: Path | None = None) -> Path:
    """Walk up from start (default cwd) to find .pcp/. Raises NoPCPDir if not found."""
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        candidate = parent / ".pcp"
        if candidate.is_dir():
            return candidate
    raise NoPCPDir(
        "No .pcp/ directory found. Run `pcp init` to initialise this project."
    )


def get_modules_dir(pcp_dir: Path) -> Path:
    return pcp_dir / "strategy" / "modules"


def get_objective(pcp_dir: Path) -> Path:
    return pcp_dir / "objective.md"


def get_decomposition(pcp_dir: Path) -> Path:
    return pcp_dir / "strategy" / "decomposition.md"


def get_ontology_state(pcp_dir: Path) -> Path:
    return pcp_dir / "ontology_state.yaml"
