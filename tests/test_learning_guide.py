"""Check the runnable lesson, rendered docs, and clone-oriented instructions."""
from __future__ import annotations

import ast
from collections import Counter
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import types
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


class RecordedBrain:
    """Declaration recorder, not an SDK or neural engine simulation."""
    def __init__(self, name):
        self.name = name
        self.nodes = {}
        self.edges = []

    def input(self, name, neurons):
        assert name not in self.nodes
        self.nodes[name] = dict(name=name, kind='input', neurons=neurons)

    def output(self, name, neurons=1):
        assert name not in self.nodes
        self.nodes[name] = dict(name=name, kind='output', neurons=neurons)

    def opu(self, name, rhythm, tnorm):
        assert name not in self.nodes
        self.nodes[name] = dict(name=name, kind='opu', rhythm=rhythm, tnorm=tnorm)

    def connect(self, source, target, kind='exc', **options):
        assert source in self.nodes, f'Missing source: {source}'
        assert target in self.nodes, f'Missing target: {target}'
        self.edges.append(dict({'from': source, 'to': target, 'type': kind}, **options))

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict()))

    def validate(self):
        return []

    def to_dict(self):
        return dict(schema='artificial-brains/design/v1', name=self.name,
                    nodes=list(self.nodes.values()), connections=self.edges)


def normalized(design):
    return dict(nodes={n['name']:{k:v for k,v in n.items() if k not in ('x','y')}
                       for n in design['nodes']},
                connections=sorted(design['connections'], key=lambda e:(e['from'],e['to'])))


def expected_index():
    full=json.loads((ROOT/'brain.abdesign.json').read_text())
    names={n['name'] for n in full['nodes'] if 'index' in n['name']}
    return dict(nodes=[n for n in full['nodes'] if n['name'] in names],
                connections=[e for e in full['connections'] if e['from'] in names and e['to'] in names])


def test_walkthrough_python_blocks_form_a_complete_design(monkeypatch, tmp_path):
    # Validate declarations and connectivity; this does not run neural dynamics.
    module=types.ModuleType('ab'); module.Brain=RecordedBrain
    monkeypatch.setitem(sys.modules,'ab',module)
    monkeypatch.chdir(tmp_path)
    text=(ROOT/'docs/neural-grip.md').read_text()
    blocks=re.findall(r'```python\n(.*?)```',text,re.S)
    assert len(blocks)==4
    ns={}
    for block in blocks:
        exec(compile(block,'docs/neural-grip.md','exec'),ns)
    assert normalized(ns['brain'].to_dict())==normalized(expected_index())
    assert (tmp_path/'examples/index-grip.abdesign.json').is_file()


def test_complete_example_matches_the_document_and_full_hand(monkeypatch):
    module=types.ModuleType('ab');module.Brain=RecordedBrain
    monkeypatch.setitem(sys.modules,'ab',module)
    spec=importlib.util.spec_from_file_location('index_grip_lesson',ROOT/'examples/index_grip.py')
    example=importlib.util.module_from_spec(spec);spec.loader.exec_module(example)
    actual=example.build_brain().to_dict()
    assert normalized(actual)==normalized(expected_index())
    assert Counter(n['kind'] for n in actual['nodes'])=={'input':3,'opu':17,'output':3}


def test_saved_teaching_design_matches_full_hand():
    assert normalized(json.loads((ROOT/'examples/index-grip.abdesign.json').read_text()))==normalized(expected_index())


def test_readme_starts_with_one_plain_title():
    text=(ROOT/'README.md').read_text()
    assert text.startswith('# Artificial Brains Tactile Hand\n')
    assert '<p align=' not in text and '<details>' not in text
    assert len(re.findall(r'^# ',text,re.M))==1
    assert text.rsplit('\n## ',1)[1].startswith('License\n')


def test_bundle_instructions_use_a_clean_clone_and_environment():
    text=(ROOT/'README.md').read_text()
    for command in ('git clone "$HOME/Downloads/artificial-brains-hand-v0.0.0.bundle" artificial-brains-hand',
                    'python3 -m venv .venv', 'python -m ensurepip --upgrade',
                    'python -m pip install -r requirements.txt', 'python -m pip check',
                    'python run.py --check', 'ab design --brain brain.abdesign.json',
                    'ab build brain --brain brain.abdesign.json --env .env'):
        assert command in text
    assert '--system-site-packages' not in text


class HTMLLinks(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.ids=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if attrs.get('id'): self.ids.append(attrs['id'])
        if tag in ('a','link') and attrs.get('href'): self.links.append(attrs['href'])
        if tag in ('img','script') and attrs.get('src'): self.links.append(attrs['src'])


@pytest.mark.parametrize('page', sorted((ROOT/'docs/site').glob('*.html')),ids=lambda p:p.name)
def test_browser_docs_links_and_anchors(page):
    parsed=HTMLLinks();parsed.feed(page.read_text())
    assert len(parsed.ids)==len(set(parsed.ids))
    for link in parsed.links:
        url=urlsplit(link)
        if url.scheme or url.netloc:continue
        target=(page.parent/unquote(url.path)).resolve() if url.path else page
        assert target.is_relative_to(ROOT)
        assert target.is_file(), f'{page.name}: {link}'
        if url.fragment and target.suffix=='.html':
            other=HTMLLinks();other.feed(target.read_text())
            assert unquote(url.fragment) in other.ids,f'{page.name}: {link}'


def test_new_files_use_python_310_syntax():
    for relative in ('examples/index_grip.py','tools/render_docs.py'):
        ast.parse((ROOT/relative).read_text(),feature_version=(3,10))


def test_installed_sdks_in_a_separate_process(tmp_path):
    # Isolate the real SDK check from unit-test fixtures that replace SDK modules.
    probe=subprocess.run([sys.executable,'-c',
        'import importlib.metadata as m; '
        'print(m.version("artificialbrains_sdk")); '
        'print(m.version("aero-open-sdk")); print(m.version("pyserial"))'],
        capture_output=True,text=True)
    if probe.returncode:
        pytest.skip('Runtime SDKs are not installed; install requirements.txt for this check.')
    result=subprocess.run([sys.executable,str(ROOT/'run.py'),'--check'],cwd=ROOT,
                          capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    result=subprocess.run([sys.executable,str(ROOT/'examples/index_grip.py'),
                          '--output',str(tmp_path/'index.abdesign.json')],cwd=ROOT,
                          capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert normalized(json.loads((tmp_path/'index.abdesign.json').read_text()))==normalized(expected_index())
