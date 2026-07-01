"""Build import dependency graph from source files."""

import ast
import re
from pathlib import Path


def extract_imports_python(path: Path, root: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
    return imports


def extract_imports_ts_js(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # import ... from '...' or require('...')
    patterns = [
        r'from\s+[\'"]([^\'"\s]+)[\'"]',
        r'require\s*\(\s*[\'"]([^\'"\s]+)[\'"]\s*\)',
        r'import\s*\(\s*[\'"]([^\'"\s]+)[\'"]\s*\)',
    ]
    imports = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            imp = match.group(1)
            if imp.startswith("."):
                # relative — resolve to a sibling file/dir name
                resolved = (path.parent / imp).resolve()
                try:
                    imp = str(resolved.relative_to(root)).split("/")[0]
                except ValueError:
                    continue
            else:
                imp = imp.split("/")[0].lstrip("@")
            imports.append(imp)
    return imports


def extract_imports_rust(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    imports = []
    for match in re.finditer(r'\buse\s+([\w:]+)', text):
        crate = match.group(1).split("::")[0]
        imports.append(crate)
    for match in re.finditer(r'\bextern\s+crate\s+(\w+)', text):
        imports.append(match.group(1))
    return imports


def extract_imports_go(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    imports = []
    for match in re.finditer(r'"([^"]+)"', text):
        pkg = match.group(1)
        parts = pkg.split("/")
        # local package = last path component
        imports.append(parts[-1])
    return imports


EXTRACTORS = {
    ".py": extract_imports_python,
    ".ts": extract_imports_ts_js,
    ".tsx": extract_imports_ts_js,
    ".js": extract_imports_ts_js,
    ".jsx": extract_imports_ts_js,
    ".rs": extract_imports_rust,
    ".go": extract_imports_go,
}


def build_dependency_graph(files: list[Path], root: Path) -> dict[str, set[str]]:
    """
    Returns adjacency dict: {file_key: {imported_file_key, ...}}
    file_key = path relative to root, using '/' separator
    Only edges between files in the project (not external packages).
    """
    file_keys = {f: str(f.relative_to(root)) for f in files}
    # index by stem and by relative path for resolution
    stem_index: dict[str, str] = {}
    for f, key in file_keys.items():
        stem_index[f.stem] = key
        stem_index[str(f.relative_to(root))] = key

    graph: dict[str, set[str]] = {key: set() for key in file_keys.values()}

    for f, key in file_keys.items():
        extractor = EXTRACTORS.get(f.suffix)
        if not extractor:
            continue
        imports = extractor(f, root)
        for imp in imports:
            # resolve to a known file key
            target = stem_index.get(imp) or stem_index.get(imp + ".py") or \
                     stem_index.get(imp + ".ts") or stem_index.get(imp + ".js")
            if target and target != key:
                graph[key].add(target)

    return graph
