#!/usr/bin/env python3
"""Generate local deployment secrets without printing credentials or committing them."""
from pathlib import Path
import os,secrets,shutil
root=Path(__file__).resolve().parents[1];d=root/'.secrets';d.mkdir(mode=0o700,exist_ok=True)
for name in ['admin_password.txt','ingest_token.txt']:
 p=d/name
 if not p.exists():
  p.write_text(secrets.token_urlsafe(36)+'\n')
  # Compose file-backed secrets are readable by the non-root container UID.
  # The containing directory is mode 0700, keeping host users out.
  os.chmod(p,0o644)
if not (root/'.env').exists():shutil.copy2(root/'.env.example',root/'.env')
print('Created missing secrets and .env. Existing files were preserved.')
print('Read .secrets/admin_password.txt locally to get the dashboard password. Do not commit secrets.')
