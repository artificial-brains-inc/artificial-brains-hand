"""Keep the getting-started guides consistent with the repository."""
from pathlib import Path
import importlib.util
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_checker', ROOT/'tools/check_project.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_project_checks():
    result = checker.check(ROOT)
    assert result['python_files'] > 0
    assert result['markdown_files'] > 0


def test_readme_license_is_the_last_section():
    text = (ROOT/'README.md').read_text()
    headings = re.findall(r'^## (.+)$', text, re.M)
    assert headings[-1] == 'License'
    before, after = text.split('## License\n', 1)
    assert '[LICENSE]' not in before and '](LICENSE)' not in before
    assert 'Artificial Brains products' in after
    assert '](LICENSE)' in after


@pytest.mark.parametrize('command', [
    'git clone https://github.com/artificial-brains-inc/artificial-brains-hand.git',
    'python -m pip install -r requirements.txt',
    'ab design --brain brain.abdesign.json',
    'ab build brain --brain brain.abdesign.json --env .env',
    'AB_TELEMETRY=true python run.py',
])
def test_readme_contains_developer_workflow(command):
    assert command in (ROOT/'README.md').read_text()


def test_local_links_find_missing_file(tmp_path):
    p = tmp_path/'README.md'
    p.write_text('[Missing](missing.md)')
    assert 'missing local link' in checker.check_markdown_links(tmp_path, p)[0]


def test_local_links_find_missing_heading(tmp_path):
    p = tmp_path/'README.md'
    p.write_text('# Example\n[Missing](#different)')
    assert 'missing heading' in checker.check_markdown_links(tmp_path, p)[0]


def test_local_links_accept_heading_and_external_link(tmp_path):
    p = tmp_path/'README.md'
    p.write_text('# Example\n[Here](#example)\n[Elsewhere](https://example.com/)')
    assert checker.check_markdown_links(tmp_path, p) == []


def test_source_file_walk_ignores_environment(tmp_path):
    env = tmp_path/'.venv'; env.mkdir()
    (env/'dependency.py').write_text('')
    code = tmp_path/'run.py'; code.write_text('')
    assert checker.source_files(tmp_path) == [code]
