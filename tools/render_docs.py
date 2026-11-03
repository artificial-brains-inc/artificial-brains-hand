#!/usr/bin/env python3
"""Build the browser-readable guides from the repository's Markdown files.

Install requirements-docs.txt, then run python tools/render_docs.py.
The generated docs/site/ files also open directly in a browser without a server.
"""
from __future__ import annotations

import html
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs/site'
CSS = '''
:root{color-scheme:dark;--bg:#0b0e14;--panel:#141923;--text:#e9e7ef;--muted:#b5b2c4;--accent:#c8b6f4;--border:#333042}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:1rem}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}
header{border-bottom:1px solid var(--border);padding:18px max(24px,calc((100vw - 960px)/2))}
header strong{display:block;letter-spacing:.09em;font-size:12px;text-transform:uppercase;color:var(--accent)}
nav{display:flex;flex-wrap:wrap;gap:8px 24px;font-size:14px;margin-top:7px}
main{max-width:1008px;padding:38px 24px 72px;margin:auto}h1{font-size:clamp(28px,4vw,40px);line-height:1.2;letter-spacing:-.035em;margin:0 0 22px}
h2{font-size:25px;line-height:1.3;margin:44px 0 18px;border-bottom:1px solid var(--border);padding-bottom:10px}
h3{font-size:19px;margin-top:32px}p{margin:0 0 18px}a{color:var(--accent);text-underline-offset:3px}a:hover{color:#fff}a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
strong{font-weight:650}code{font: .9em/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--panel);padding:2px 5px;border-radius:4px}
pre{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:18px;overflow:auto;max-width:100%}pre code{padding:0;background:none;white-space:pre}
.table-wrap{max-width:100%;overflow:auto;margin:18px 0 26px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:12px 14px;border:1px solid var(--border);text-align:left;vertical-align:top}th{background:var(--panel)}
img{max-width:100%;height:auto;border-radius:8px}blockquote{margin:20px 0;padding:10px 20px;border-left:3px solid var(--accent);background:var(--panel)}
footer{max-width:960px;margin:auto;padding:24px 0 40px;color:var(--muted);font-size:13px;border-top:1px solid var(--border)}
@media(max-width:600px){main{padding:26px 18px 48px}header{padding:14px 18px}nav{gap:6px 16px}h2{font-size:22px}footer{margin:0 18px}pre{padding:12px}th,td{padding:9px}}
'''


def sources(root: Path) -> dict[Path, str]:
    mapping = {root / 'README.md': 'index.html', root / 'LICENSE': 'license.html'}
    for path in sorted((root / 'docs').glob('*.md')):
        mapping[path] = path.stem + '.html'
    for name in ('AGENTS.md', 'CONTRIBUTING.md', 'BRANDING.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md'):
        mapping[root / name] = Path(name).stem.lower().replace('_','-') + '.html'
    mapping[root / 'sensor_firmware/README.md'] = 'sensor-firmware.html'
    mapping[root / 'examples/README.md'] = 'examples.html'
    return mapping


def remap(raw: str, source: Path, mapping: dict[Path, str], destination: Path) -> str:
    uri = urlsplit(raw)
    if uri.scheme or uri.netloc or not uri.path:
        return raw
    target = (source.parent / unquote(uri.path)).resolve()
    if target in mapping:
        result = mapping[target]
    else:
        result = Path(os.path.relpath(target, destination)).as_posix()
    if uri.query:
        result += '?' + uri.query
    if uri.fragment:
        result += '#' + uri.fragment
    return result


def render(root: Path = ROOT) -> list[Path]:
    root = root.resolve()
    output = root / 'docs/site'
    output.mkdir(parents=True, exist_ok=True)
    mapping = sources(root)
    md = MarkdownIt('commonmark', {'html': False}).enable('table')
    generated = []
    for source, name in mapping.items():
        text = source.read_text(encoding='utf-8')
        if source.name == 'LICENSE':
            title = 'License'
            body = '<h1>License</h1><pre><code>' + html.escape(text) + '</code></pre>'
        else:
            tokens = md.parse(text)
            title = next((tokens[i+1].content for i,t in enumerate(tokens) if t.type == 'heading_open'),source.stem)
            counts = {}
            for i, token in enumerate(tokens):
                if token.type == 'heading_open':
                    label = re.sub(r'<[^>]+>', '', tokens[i+1].content).lower()
                    slug = re.sub(r'[^\w\-\s]', '', label).replace(' ', '-')
                    count = counts.get(slug, 0)
                    counts[slug] = count + 1
                    token.attrSet('id', slug if count == 0 else f'{slug}-{count}')
                stack = [token]
                while stack:
                    child = stack.pop()
                    stack.extend(child.children or [])
                    for attr in ('href','src'):
                        raw = child.attrGet(attr)
                        if raw:
                            child.attrSet(attr, remap(raw,source,mapping,output))
            body = md.renderer.render(tokens,md.options,{})
            body = body.replace('<table>', '<div class="table-wrap"><table>').replace('</table>', '</table></div>')
        document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Artificial Brains</title><link rel="stylesheet" href="style.css"></head>
<body><header><strong>Artificial Brains · Tactile Hand</strong><nav aria-label="Documentation">
<a href="index.html">Get started</a><a href="neural-grip.html">Neural grip</a><a href="configuration.html">Configuration</a><a href="sensors.html">Sensors</a><a href="testing.html">Testing</a>
</nav></header><main>{body}</main><footer><a href="index.html">Getting started</a> · <a href="license.html">License</a></footer></body></html>
'''
        path = output / name
        path.write_text(document,encoding='utf-8')
        generated.append(path)
    (output / 'style.css').write_text(CSS.strip()+'\n',encoding='utf-8')
    return generated


if __name__ == '__main__':
    files = render()
    print(f'Built {len(files)} pages. Open docs/site/index.html in a browser.')
