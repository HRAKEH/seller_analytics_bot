"""Create a separate verified compact database; never replace the input.
Run from the repository root: python -m scripts.compact_raw_payloads INPUT OUTPUT
Stop the bot before using a compacted copy as its live database.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import os
from app.storage.raw_payloads import pack_raw_json, unpack_raw_json

def compact_copy(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if output.exists() or source == output:
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Staging on the destination filesystem makes publication atomic.
    fd, name = tempfile.mkstemp(prefix='.sellerbot-compact-', suffix='.sqlite3', dir=output.parent)
    os.close(fd)
    tmp = Path(name)
    compressed = 0
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst, pages=256)
            dst.execute('PRAGMA journal_mode=DELETE')
            initial_count = dst.execute('SELECT COUNT(*) FROM raw_payloads').fetchone()[0]
            last = 0
            while True:
                rows = dst.execute('SELECT id,payload_json FROM raw_payloads WHERE id>? ORDER BY id LIMIT 16',(last,)).fetchall()
                if not rows:
                    break
                with dst:
                    for rid, value in rows:
                        if isinstance(value, str):
                            packed = pack_raw_json(value)
                            if isinstance(packed, bytes):
                                if unpack_raw_json(packed) != value:
                                    raise ValueError('Lossless roundtrip failed')
                                dst.execute('UPDATE raw_payloads SET payload_json=? WHERE id=?',(packed,rid))
                                compressed += 1
                        else:
                            unpack_raw_json(value)
                        last = rid
            dst.execute('VACUUM')
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity check failed')
            if dst.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('SQLite foreign key check failed')
            if dst.execute('SELECT COUNT(*) FROM raw_payloads').fetchone()[0] != initial_count:
                raise ValueError('Snapshot count changed')
        # Atomic no-overwrite publication. Never replace source or an existing output.
        os.link(tmp, output)
        return compressed, output.stat().st_size
    finally:
        tmp.unlink(missing_ok=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    count,size=compact_copy(args.source,args.output)
    print(f'Compressed {count} snapshots; output: {size} bytes')

if __name__=='__main__':
    main()
