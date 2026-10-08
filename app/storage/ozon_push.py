"""Durable, account-scoped Ozon inbox and idempotent cancellation records."""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import re
import secrets

from app.services.ozon_push_payload import PushError, canonical, digest


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class OzonPushStore:
    def __init__(self, database):
        self.db = database

    def connection(self, connection_id: int):
        with self.db.connect() as c:
            row = c.execute('''SELECT mc.*,s.active,s.credential_profile FROM marketplace_connections mc
                JOIN shops s ON s.id=mc.shop_id WHERE mc.id=?''', (connection_id,)).fetchone()
        return dict(row) if row else None

    def observe_account(self, connection_id: int, client_id: str, profile: str) -> None:
        """A changed cabinet must be explicitly connected with a new URL."""
        with self.db.connect() as c:
            c.execute('''UPDATE ozon_push_bindings SET active_client=
                CASE WHEN client_id_hash=? AND credential_profile=? THEN 1 ELSE 0 END
                WHERE connection_id=?''', (digest(client_id.strip()), profile, connection_id))

    def issue(self, connection_id: int, shop_id: int, client_id: str, profile: str,
              *, actor_id: int, system_owners: tuple[int, ...], replace_hash: str | None = None) -> str:
        if actor_id not in system_owners or not client_id.strip():
            raise PushError('Подключение доступно владельцу бота.', 403)
        token = secrets.token_urlsafe(32)
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            allowed = c.execute('''SELECT 1 FROM marketplace_connections mc JOIN shops s ON s.id=mc.shop_id
                JOIN user_shop_access a ON a.shop_id=s.id JOIN bot_users u ON u.telegram_user_id=a.telegram_user_id
                WHERE mc.id=? AND s.id=? AND mc.marketplace='ozon' AND mc.enabled=1 AND s.active=1
                  AND s.credential_profile=? AND a.telegram_user_id=? AND a.role='owner' AND u.active=1''',
                (connection_id, shop_id, profile, actor_id)).fetchone()
            if not allowed:
                raise PushError('Магазин или права изменились. Откройте подключение заново.', 403)
            old = c.execute('SELECT * FROM ozon_push_bindings WHERE connection_id=?', (connection_id,)).fetchone()
            if old and (replace_hash is None or not hmac.compare_digest(old['token_hash'], replace_hash)):
                raise PushError('Адрес уже создан или изменился. Для замены откройте подключение заново.', 409)
            if not old and replace_hash is not None:
                raise PushError('Подключение изменилось. Откройте раздел заново.', 409)
            account_hash = digest(client_id.strip())
            same_account = old and old['client_id_hash'] == account_hash
            time = now()
            c.execute('''INSERT INTO ozon_push_bindings
                (connection_id,token_hash,client_id_hash,credential_profile,created_at,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET
                token_hash=excluded.token_hash,client_id_hash=excluded.client_id_hash,
                credential_profile=excluded.credential_profile,active_client=1,updated_at=excluded.updated_at,
                seller_id=?,last_ping_at=NULL,last_received_at=NULL,last_error=NULL''',
                (connection_id,digest(token),account_hash,profile,time,time,old['seller_id'] if same_account else None))
        return token

    def status(self, connection_id: int):
        with self.db.connect() as c:
            binding = c.execute('SELECT * FROM ozon_push_bindings WHERE connection_id=?', (connection_id,)).fetchone()
            if not binding:
                return None
            result = dict(binding)
            counts = c.execute('''SELECT status,COUNT(*) n FROM ozon_push_inbox
                WHERE connection_id=? AND client_id_hash=? GROUP BY status''',
                (connection_id,binding['client_id_hash'])).fetchall()
            result['counts'] = {r['status']: r['n'] for r in counts}
            repeated = c.execute('''SELECT COALESCE(SUM(delivery_count-1),0) FROM ozon_push_inbox
                WHERE connection_id=? AND client_id_hash=?''', (connection_id,binding['client_id_hash'])).fetchone()[0]
            result['repeated_deliveries'] = repeated + result['counts'].get('duplicate',0)
            result['confirmed_postings'] = c.execute('''SELECT COUNT(*) FROM ozon_push_cancellations
                WHERE connection_id=? AND client_id_hash=? AND conflicted=0''',
                (connection_id,binding['client_id_hash'])).fetchone()[0]
            # token_hash is only used internally for a compare-and-swap rotation.
            return result

    def receive(self, connection_id: int, token: str, client_id: str, profile: str,
                payload: dict, parsed: dict | None, *, validation_error: str | None = None,
                payload_text: str | None = None) -> str:
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise PushError('Адрес подключения не найден.', 404)
        text = canonical(payload)
        payload_hash = digest(text)
        account_hash = digest(client_id.strip())
        time = now()
        with self.db.connect() as c:
            # Fail fast so Ozon can retry instead of holding a request for 30s.
            c.execute('PRAGMA busy_timeout=1000')
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT b.*,mc.marketplace,mc.enabled,s.active,s.credential_profile current_profile
                FROM ozon_push_bindings b JOIN marketplace_connections mc ON mc.id=b.connection_id
                JOIN shops s ON s.id=mc.shop_id WHERE b.connection_id=?''', (connection_id,)).fetchone()
            if (not row or not hmac.compare_digest(row['token_hash'],digest(token)) or
                    row['marketplace'] != 'ozon' or not row['enabled'] or not row['active'] or
                    not row['active_client'] or row['client_id_hash'] != account_hash or
                    row['credential_profile'] != profile or row['current_profile'] != profile):
                raise PushError('Адрес подключения не найден или кабинет изменился.', 404)
            if parsed and parsed.get('seller_id') and row['seller_id'] not in (None, parsed['seller_id']):
                c.execute('UPDATE ozon_push_bindings SET last_error=? WHERE connection_id=?',
                          ('seller_id не совпадает с подключённым кабинетом.',connection_id))
                # Commit this diagnostic without accepting or counting the event.
                c.commit()
                raise PushError('seller_id не совпадает с подключённым кабинетом.', 409)
            if parsed and parsed['message_type'] == 'TYPE_PING':
                c.execute('UPDATE ozon_push_bindings SET last_ping_at=? WHERE connection_id=?', (time,connection_id))
                return 'ping'
            existing = c.execute('''SELECT status FROM ozon_push_inbox WHERE connection_id=?
                AND client_id_hash=? AND payload_hash=?''', (connection_id,account_hash,payload_hash)).fetchone()
            if existing:
                c.execute('''UPDATE ozon_push_inbox SET delivery_count=delivery_count+1
                    WHERE connection_id=? AND client_id_hash=? AND payload_hash=?''',
                    (connection_id,account_hash,payload_hash))
                c.execute('UPDATE ozon_push_bindings SET last_received_at=? WHERE connection_id=?', (time,connection_id))
                return existing['status']
            kind = str(payload.get('message_type',''))[:80] if isinstance(payload,dict) else ''
            status = 'invalid' if validation_error else 'accepted' if parsed and 'scheme' in parsed else 'ignored'
            cur = c.execute('''INSERT INTO ozon_push_inbox
                (connection_id,client_id_hash,payload_hash,message_type,event_uuid,received_at,payload_json,status,error)
                VALUES(?,?,?,?,?,?,?,?,?)''', (connection_id,account_hash,payload_hash,kind,
                parsed.get('event_uuid') if parsed else None,time,payload_text if payload_text is not None else text,status,validation_error))
            inbox_id = cur.lastrowid
            if status == 'accepted':
                matches = c.execute('''SELECT * FROM ozon_push_cancellations WHERE connection_id=?
                    AND client_id_hash=? AND ((scheme=? AND posting_number=?) OR (? IS NOT NULL AND event_uuid=?))''',
                    (connection_id,account_hash,parsed['scheme'],parsed['posting_number'],
                     parsed['event_uuid'],parsed['event_uuid'])).fetchall()
                if matches:
                    status = 'duplicate' if all(r['semantic_hash']==parsed['semantic_hash'] and not r['conflicted']
                                               for r in matches) else 'conflict'
                    if status == 'conflict':
                        for match in matches:
                            c.execute('UPDATE ozon_push_cancellations SET conflicted=1 WHERE id=?', (match['id'],))
                        validation_error = 'Получены разные версии отмены; спорные количества исключены из итога.'
                    c.execute('UPDATE ozon_push_inbox SET status=?,error=? WHERE id=?', (status,validation_error,inbox_id))
                else:
                    c.execute('''INSERT INTO ozon_push_cancellations
                        (connection_id,client_id_hash,scheme,posting_number,event_uuid,cancelled_at,event_day,
                         units,products_json,semantic_hash,inbox_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (connection_id,account_hash,parsed['scheme'],parsed['posting_number'],parsed['event_uuid'],
                         parsed['cancelled_at'],parsed['event_day'],parsed['units'],parsed['products_json'],
                         parsed['semantic_hash'],inbox_id))
                if row['seller_id'] is None:
                    c.execute('UPDATE ozon_push_bindings SET seller_id=? WHERE connection_id=?', (parsed['seller_id'],connection_id))
            c.execute('UPDATE ozon_push_bindings SET last_received_at=?,last_error=? WHERE connection_id=?',
                      (time,validation_error,connection_id))
        return status

    def day(self, connection_id: int, day: str):
        with self.db.connect() as c:
            binding = c.execute('''SELECT b.* FROM ozon_push_bindings b
                JOIN marketplace_connections mc ON mc.id=b.connection_id JOIN shops s ON s.id=mc.shop_id
                WHERE b.connection_id=? AND b.active_client=1 AND mc.enabled=1 AND s.active=1
                  AND b.credential_profile=s.credential_profile''', (connection_id,)).fetchone()
            if not binding:
                return None
            result = c.execute('''SELECT COALESCE(SUM(e.units),0) units,MAX(i.received_at) freshness
                FROM ozon_push_cancellations e LEFT JOIN ozon_push_inbox i ON i.id=e.inbox_id
                WHERE e.connection_id=? AND e.client_id_hash=? AND e.event_day=? AND e.conflicted=0''',
                (connection_id,binding['client_id_hash'],day)).fetchone()
            issues = c.execute('''SELECT COUNT(*) FROM ozon_push_inbox WHERE connection_id=?
                AND client_id_hash=? AND status IN ('conflict','invalid')''', (connection_id,binding['client_id_hash'])).fetchone()[0]
        return {**dict(result),'issues':issues,'last_ping_at':binding['last_ping_at']}
