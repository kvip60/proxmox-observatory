"""Operator-only account recovery. Run interactively inside the dashboard container."""
import argparse,getpass,os
from pathlib import Path
from server import Store
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['reset-password']);p.add_argument('username');a=p.parse_args()
 store=Store(Path(os.environ.get('DATA_DIR','/data'))/'history.db');user=store.account(username=a.username)
 if not user or user['role']!='admin':raise SystemExit('Administrator account not found')
 password=getpass.getpass('New password (up to 256 characters): ')
 if password!=getpass.getpass('Confirm password: '):raise SystemExit('Passwords do not match')
 store.update_account(user['id'],user['username'],password)
 print('Administrator password updated. All previous sessions for this account were revoked.')
