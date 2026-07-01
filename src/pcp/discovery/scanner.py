"""Stack detection and file inventory for brownfield import."""

import re
from pathlib import Path


STACK_SIGNALS = {
    "python": ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"],
    "typescript": ["tsconfig.json", "package.json"],
    "javascript": ["package.json"],
    "rust": ["Cargo.toml"],
    "swift": ["Package.swift", "*.xcodeproj"],
    "go": ["go.mod"],
    "java": ["pom.xml", "build.gradle"],
    "ruby": ["Gemfile"],
}

SOURCE_EXTENSIONS = {
    "python": [".py"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx"],
    "rust": [".rs"],
    "swift": [".swift"],
    "go": [".go"],
    "java": [".java"],
    "ruby": [".rb"],
}

IGNORE_DIRS = {
    ".git", ".pcp", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".build", "target", ".next", ".nuxt", "coverage",
    ".tox", "eggs", "*.egg-info",
}


def detect_stack(root: Path) -> list[str]:
    detected = []
    for lang, signals in STACK_SIGNALS.items():
        for signal in signals:
            if signal.startswith("*"):
                if list(root.glob(f"**/{signal}")):
                    detected.append(lang)
                    break
            else:
                if (root / signal).exists():
                    detected.append(lang)
                    break
    # deduplicate: ts implies js, prefer ts
    if "typescript" in detected and "javascript" in detected:
        detected.remove("javascript")
    return detected or ["unknown"]


def collect_source_files(root: Path, stack: list[str]) -> list[Path]:
    extensions = set()
    for lang in stack:
        extensions.update(SOURCE_EXTENSIONS.get(lang, []))
    if not extensions:
        extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".swift", ".go"}

    files = []
    for path in root.rglob("*"):
        if any(part in IGNORE_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix in extensions and path.is_file():
            files.append(path)
    return sorted(files)


def detect_entry_points(root: Path, stack: list[str]) -> list[str]:
    entries = []
    candidates = [
        "main.py", "app.py", "server.py", "wsgi.py", "asgi.py",
        "index.ts", "index.js", "main.ts", "server.ts", "app.ts",
        "src/main.rs", "src/lib.rs",
        "main.go", "cmd/main.go",
        "Sources/*/main.swift",
    ]
    for c in candidates:
        matches = list(root.glob(c))
        entries.extend(str(m.relative_to(root)) for m in matches)
    return entries


def read_manifest_deps(root: Path) -> dict[str, list[str]]:
    deps = {}
    pkg = root / "package.json"
    if pkg.exists():
        import json
        try:
            data = json.loads(pkg.read_text())
            deps["dependencies"] = list(data.get("dependencies", {}).keys())
            deps["devDependencies"] = list(data.get("devDependencies", {}).keys())
        except Exception:
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        matches = re.findall(r'^\s*"([a-zA-Z0-9_\-]+)[>=<!\[]', text, re.MULTILINE)
        deps["python"] = matches

    cargo = root / "Cargo.toml"
    if cargo.exists():
        text = cargo.read_text()
        matches = re.findall(r'^([a-zA-Z0-9_\-]+)\s*=', text, re.MULTILINE)
        deps["rust"] = [m for m in matches if m not in ("package", "lib", "bin", "dependencies", "features")]

    return deps
