from pathlib import Path, PurePosixPath
import ast
import json
import re
import subprocess
import sys
ROOT = Path(__file__).resolve().parent
ROOT_FILES = {'README.md', 'PUBLICATION_REVIEW.md', 'requirements.txt', '.gitignore', '.gitattributes', 'AGENTS.md', 'labnote-deployment.example.json', 'check_public.py'}
EXTRA_FILES = {'.github/workflows/ci.yml', '.github/dependabot.yml', '.githooks/pre-commit'}
PATTERNS = [r'\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b', r'\b10(?:\.\d{1,3}){3}\b', r'\b192\.168(?:\.\d{1,3}){2}\b', r'(?i)\b(?:ac|mb)\d{5}\b', r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', r'gh[pousr]_[A-Za-z0-9]{20,}', r'github_pat_[A-Za-z0-9_]{20,}', r'AIza[A-Za-z0-9_-]{30,}', r'\bya29\.[A-Za-z0-9_-]{20,}']

def inspect_file(name, text):
    path = PurePosixPath(name)
    allowed = name in ROOT_FILES or name in EXTRA_FILES or (path.parts[0] in {'fileclient', 'tests'} and path.suffix == '.py') or name == 'fileclient/font-requirements.json'
    if not allowed: return [f'{name}: unexpected or private distribution path']
    if path.name in {'personal.py', 'updates.py'}: return [f'{name}: excluded implementation']
    errors = []
    if any(re.search(pattern, text) for pattern in PATTERNS): errors.append(f'{name}: possible private content')
    addresses = re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text)
    if any(not a.endswith('@example.com') for a in addresses): errors.append(f'{name}: non-example email requires review')
    try:
        if path.suffix == '.py':
            tree = ast.parse(text, filename=name)
            if path.parts[0] == 'fileclient':
                excluded = {'django', 'labnote', 'records', 'desktop', 'personal', 'updates'}
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import) and any(a.name.split('.')[0] in excluded for a in node.names): errors.append(f'{name}: excluded dependency')
                    if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in excluded: errors.append(f'{name}: excluded dependency')
        elif path.suffix == '.json': json.loads(text)
    except (SyntaxError, ValueError): errors.append(f'{name}: invalid source syntax')
    return errors

def check():
    staged = '--staged' in sys.argv
    git = (ROOT / '.git').exists()
    if git:
        args = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'] if staged else ['git', 'ls-files', '-z']
        names = subprocess.check_output(args, cwd=ROOT).decode('utf-8').split('\0')
    else:
        ignored = {'.git', '.venv', '__pycache__', 'var', 'work', 'outputs'}
        names = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and not any(part in ignored for part in p.relative_to(ROOT).parts) and p.name != 'labnote-deployment.json']
    errors, count = [], 0
    for name in filter(None, names):
        try:
            raw = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT) if staged and git else (ROOT / name).read_bytes()
            errors.extend(inspect_file(name, raw.decode('utf-8')))
            count += 1
        except (UnicodeError, OSError): errors.append(f'{name}: cannot inspect as text')
    if errors:
        print('\n'.join(sorted(set(errors))))
        return 1
    print(f'Public source checks passed: {count} text files. This is a limited pattern check.')
    return 0
if __name__ == '__main__': sys.exit(check())
