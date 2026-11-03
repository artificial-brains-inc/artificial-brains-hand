#!/usr/bin/env python3
"""Check project files, Python syntax, neural exports, and local Markdown links.

Run from any directory with the example's Python environment. This command uses
only local files and does not connect to a hand, a device, or the network.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'logs',
             'backups', 'test-results', '.pio', '.vscode', '.idea', 'build', 'dist'}
REQUIRED = (
    'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'AGENTS.md', 'VERSION',
    '.env.example', '.gitignore', 'requirements.txt', 'config.json', 'poses.json',
    'run.py', 'make_brain.py', 'brain.abdesign.json', 'brain_source.py',
    'docs/neural-grip.md', 'docs/configuration.md', 'docs/sensors.md',
    'docs/testing.md', 'docs/troubleshooting.md', 'docs/safety.md',
)


def source_files(root: Path) -> list[Path]:
    """Select source files while leaving local environments and run data alone."""
    return sorted(p for p in root.rglob('*') if p.is_file()
                  and not any(part in SKIP_DIRS for part in p.relative_to(root).parts))


def heading_ids(text: str) -> set[str]:
    """Generate heading anchors for the plain Markdown headings in these guides."""
    ids: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line)
        if not match:
            continue
        heading = re.sub(r'<[^>]+>', '', match.group(1)).lower()
        heading = re.sub(r'[^\w\-\s]', '', heading)
        slug = heading.replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        ids.add(slug if count == 0 else f'{slug}-{count}')
    return ids


def check_markdown_links(root: Path, path: Path) -> list[str]:
    """Check relative file links and heading anchors without fetching websites."""
    errors = []
    text = path.read_text(encoding='utf-8')
    links = re.findall(r'!?\[[^\]\n]*\]\(([^\n)]+)\)', text)
    links += re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', text)
    for link in links:
        raw = link.strip().split(' "', 1)[0].strip('<>')
        uri = urlsplit(raw)
        if uri.scheme or uri.netloc:
            continue
        target = (path.parent / unquote(uri.path)).resolve() if uri.path else path.resolve()
        if not target.is_relative_to(root.resolve()):
            errors.append(f'{path.relative_to(root)}: link leaves the project: {raw}')
        elif not target.exists():
            errors.append(f'{path.relative_to(root)}: missing local link: {raw}')
        elif uri.fragment and target.suffix == '.md':
            if unquote(uri.fragment) not in heading_ids(target.read_text(encoding='utf-8')):
                errors.append(f'{path.relative_to(root)}: missing heading: {raw}')
    return errors


def check(root: Path = ROOT) -> dict[str, int]:
    root = root.resolve()
    errors = [f'Missing project file: {name}' for name in REQUIRED if not (root/name).is_file()]
    paths = source_files(root)
    counts = {'python_files': 0, 'markdown_files': 0}
    for path in paths:
        if path.suffix == '.py':
            counts['python_files'] += 1
            try:
                ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            except (SyntaxError, UnicodeError) as exc:
                errors.append(f'{path.relative_to(root)}: {exc}')
        elif path.suffix == '.md':
            counts['markdown_files'] += 1
            errors.extend(check_markdown_links(root, path))
    if errors:
        raise ValueError('\n'.join(errors))

    sys.path.insert(0, str(root))
    from aerohand_control.constants import VERSION
    from aerohand_control.design import audit, design_dict, export_design
    data = json.loads((root/'brain.abdesign.json').read_text(encoding='utf-8'))
    audit(data)
    if data != design_dict():
        errors.append('brain.abdesign.json differs from the generator; run python make_brain.py.')
    with tempfile.TemporaryDirectory() as tmp:
        export_design(tmp)
        expected = (Path(tmp)/'brain_source.py').read_text(encoding='utf-8')
        if expected != (root/'brain_source.py').read_text(encoding='utf-8'):
            errors.append('brain_source.py differs from the generator; run python make_brain.py.')
    if (root/'VERSION').read_text(encoding='utf-8').strip() != VERSION:
        errors.append('VERSION and aerohand_control.constants.VERSION differ.')
    if errors:
        raise ValueError('\n'.join(errors))
    return counts


if __name__ == '__main__':
    try:
        result = check()
    except (ValueError, OSError, UnicodeError, SyntaxError, ImportError) as exc:
        print(f'Project check failed:\n{exc}', file=sys.stderr)
        raise SystemExit(1)
    print(f"Project checks passed: {result['python_files']} Python files, "
          f"{result['markdown_files']} Markdown files, and both neural exports.")
