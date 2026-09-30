"""Create/update local database schema without starting Telegram polling."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.storage import Database

if __name__ == '__main__':
    path=Path(sys.argv[1]) if len(sys.argv)>1 else settings.db_file
    db=Database(path)
    version=db.initialize()
    print(f'✅ Database ready: {path} | schema v{version} | integrity={db.integrity_check()}')
