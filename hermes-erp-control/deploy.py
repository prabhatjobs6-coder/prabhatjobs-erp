#!/usr/bin/env python3
"""Install the fixed file worker without changing ERP roles or credentials."""
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

import yaml

SOURCE = Path(__file__).resolve().parent
TARGET = Path('/opt/prabhat-workbench')
DOCS = Path('/home/hermeslab/docs_mcp.py')
CONFIG = Path('/home/hermes/.hermes/config.yaml')
SOUL = Path('/home/hermes/.hermes/SOUL.md')
TAG = 'PRABHAT_WORKBENCH_ADAPTER_V1'
ROUTE_TAG = 'Prabhat ERP execution routing — workbench v1'

GUIDANCE = '''

## Prabhat ERP execution routing — workbench v1

- Use the PrabhatJobs MCP first for ERP actions under the current requester's delegated identity. Never substitute a saved owner credential for a staff token.
- A denied scope or permission is a blocker. Do not use browser, direct database access or another account to get around that denial.
- For a genuinely missing MCP action, use browser control only in an already-authorized session belonging to that requester, with normal ERP permissions and write approvals. If the browser is disconnected, say so plainly.
- For PDF split/merge, spreadsheets and PDF/DOCX documents, discover workbench tools in pwf_docs. Submit a fixed file job with an idempotency key; retain its job_id; poll status. The systemd worker continues while chat/browser is closed.
- Use existing pwf_media for images, logos and video. Fixed video jobs may be queued in workbench. Do not fabricate candidate details, experience, documents, vacancy facts or delivery status.
- A queued job or approval request is pending. Say done only after success is verified; deliver the returned MEDIA attachment. Do not claim an ERP upload or publication merely because a local file was created.
- Workbench processes local files only. Recurring ERP actions need the existing ERP/n8n scheduler and an explicit scoped grant. Do not persist a short-lived conversation token or silently turn old workflows back on.
- Keep a precise account of missing capabilities. Complete every independently authorized part, and state any remaining blocker with its actual error.
'''


def run(argv):
    subprocess.run(argv, check=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo on the existing Prabhat VPS')
    if not DOCS.is_file() or not CONFIG.is_file() or not SOUL.is_file():
        raise SystemExit('Expected existing Prabhat Hermes installation was not found')
    os.umask(0o077)
    user = pwd.getpwnam('hermeslab')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = TARGET / 'backups' / stamp
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for path in [DOCS, CONFIG, SOUL, Path('/etc/systemd/system/prabhat-workbench.service')]:
        if path.exists():
            copy = backup / path.name
            shutil.copy2(path, copy)
            manifest[str(path)] = str(copy)
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    os.chmod(TARGET, 0o755)
    for name in ['workbench.py','test_workbench.py','requirements.txt','README.md','prabhat-workbench.service','deploy.py']:
        if SOURCE / name != TARGET / name:
            shutil.copy2(SOURCE/name,TARGET/name)
        os.chmod(TARGET/name,0o644)
    for path in [Path('/home/hermeslab/files'),Path('/home/hermeslab/.local/state'),Path('/home/hermeslab/.local/state/prabhat-workbench')]:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path,user.pw_uid,user.pw_gid)
    venv = TARGET/'venv'
    candidates = [Path(p) for p in [shutil.which('uv'), '/home/hermeslab/.local/bin/uv'] if p]
    candidates += list(Path('/home/hermes/.hermes/tools').glob('uv-*-linux-x64/uv'))
    candidates += list(Path('/home/hermes/.hermes/tools').glob('uv-*-linux-x64/bin/uv'))
    uv = next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if not (venv/'bin/python').exists():
        if uv:
            run([uv,'venv','--python','/usr/bin/python3',str(venv)])
        else:
            run(['/usr/bin/python3','-m','venv',str(venv)])
    # The worker needs read/execute access to its environment, not ownership of code.
    if uv:
        run([uv,'pip','install','--python',str(venv/'bin/python'),'-r',str(TARGET/'requirements.txt')])
    else:
        run([str(venv/'bin/python'),'-m','pip','install','--disable-pip-version-check','-r',str(TARGET/'requirements.txt')])
    for root, directories, files in os.walk(venv):
        os.chmod(root,0o755)
        for name in files:
            p=Path(root)/name
            if not p.is_symlink():
                os.chmod(p,0o755 if os.access(p,os.X_OK) else 0o644)
    text = DOCS.read_text()
    if TAG not in text:
        adapter = '''\n# PRABHAT_WORKBENCH_ADAPTER_V1\nimport importlib.util as _pwf_import\n_pwf_spec = _pwf_import.spec_from_file_location('prabhat_workbench', '/opt/prabhat-workbench/workbench.py')\n_pwf_module = _pwf_import.module_from_spec(_pwf_spec)\n_pwf_spec.loader.exec_module(_pwf_module)\nTOOLS.extend(_pwf_module.TOOLS)\nFN.update(_pwf_module.FUNCTIONS)\n\n'''
        marker='def handle(msg):'
        if text.count(marker)!=1:
            raise SystemExit('Document MCP shape changed; review before modifying')
        DOCS.write_text(text.replace(marker,adapter+marker))
        os.chown(DOCS,user.pw_uid,user.pw_gid)
    config=yaml.safe_load(CONFIG.read_text())
    config.setdefault('tools',{}).setdefault('tool_search',{}).update({'enabled':'on','threshold_pct':1})
    CONFIG.write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True))
    owner=pwd.getpwnam('hermes')
    os.chown(CONFIG,owner.pw_uid,owner.pw_gid)
    if ROUTE_TAG not in SOUL.read_text():
        with SOUL.open('a') as f:
            f.write(GUIDANCE)
    shutil.copy2(TARGET/'prabhat-workbench.service','/etc/systemd/system/prabhat-workbench.service')
    os.chmod('/etc/systemd/system/prabhat-workbench.service',0o644)
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now','prabhat-workbench.service'])
    print('INSTALLED',json.dumps({'backup':str(backup),'worker':'prabhat-workbench.service','reload_hermes_required':True}))


if __name__=='__main__':
    main()
