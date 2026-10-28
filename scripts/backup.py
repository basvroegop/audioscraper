"""Create a consistent SQLite backup without stopping the dashboard.

Run inside the container: python /app/scripts/backup.py
The script is also usable from a one-off container mounting this project.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

root=Path(os.getenv('DATA_DIR','./data'))
folder=root/'backups'
folder.mkdir(parents=True,exist_ok=True)
path=folder/('audioscraper-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')
with sqlite3.connect(root/'audioscraper.sqlite3') as source, sqlite3.connect(path) as target:
    source.backup(target)
print(path)
