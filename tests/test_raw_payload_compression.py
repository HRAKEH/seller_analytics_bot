import json
import sqlite3
import pytest
from app.storage import Database, Repository
from app.storage.raw_payloads import pack_raw_json, unpack_raw_json
from scripts.compact_raw_payloads import compact_copy

def test_roundtrip_and_corruption():
    body=json.dumps({'items':['данные']*10000},ensure_ascii=False)
    packed=pack_raw_json(body)
    assert isinstance(packed,bytes)
    assert unpack_raw_json(packed)==body
    assert unpack_raw_json('{}')=='{}'
    with pytest.raises(ValueError):
        unpack_raw_json(packed[:-1])

def test_repository_compression_and_legacy_copy(tmp_path):
    path=tmp_path/'old.sqlite'
    db=Database(path);db.initialize();repo=Repository(db)
    seller=repo.ensure_seller(1,'Seller');shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    data={'items':['data']*10000}
    rid=repo.record_success(conn.id,'probe','2026-11-26',data,[])
    assert json.loads(repo.raw_payload_for_run(rid))==data
    assert json.loads(repo.latest_raw_source(conn.id,'2026-11-26',('probe',))['payload_json'])==data
    with db.session() as c:
        assert c.execute('SELECT typeof(payload_json) FROM raw_payloads WHERE source_run_id=?',(rid,)).fetchone()[0]=='blob'
        c.execute('UPDATE raw_payloads SET payload_json=? WHERE source_run_id=?',(json.dumps(data),rid))
    assert json.loads(repo.raw_payload_for_run(rid))==data
    output=tmp_path/'compact.sqlite'
    assert compact_copy(path,output)[0]==1
    assert json.loads(Repository(Database(output)).raw_payload_for_run(rid))==data
    with db.session() as c:
        assert c.execute('SELECT typeof(payload_json) FROM raw_payloads WHERE source_run_id=?',(rid,)).fetchone()[0]=='text'
    with pytest.raises(FileExistsError):
        compact_copy(path,output)
