"""Conservative card monitoring. Complete snapshots only; no stock-based inference."""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from app.services.alerts import AlertNotification, marketplace_label

CARD_RULES = {'card_blocked', 'card_errors', 'card_source'}


def rows_at(payload, key):
    if not isinstance(payload, dict) or payload.get('error'):
        raise ValueError('Некорректный ответ API карточек')
    rows = payload.get(key)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('API карточек не вернул ожидаемый список')
    return rows


def wb_blocked(payload):
    result = {}
    for r in rows_at(payload, 'report'):
        if not isinstance(r.get('nmId'), int) or r['nmId'] <= 0:
            raise ValueError('Нет артикула WB в отчёте блокировок')
        subject = f'wildberries:{r["nmId"]}'
        result[subject] = f'🟣 WB · артикул {r["nmId"]}: карточка заблокирована\nТовар: {r.get("title") or "Название не получено"}\nПричина: {r.get("reason") or "Не указана площадкой"}'
    return result


def wb_errors(payload):
    problems = {}
    for batch in rows_at(payload, 'items'):
        errors = batch.get('errors')
        if not isinstance(errors, dict):
            raise ValueError('Нет списка ошибок черновиков WB')
        for vendor, reasons in errors.items():
            if not isinstance(vendor,str) or not vendor or not isinstance(reasons,list) or any(not isinstance(v,str) for v in reasons):
                raise ValueError('Некорректные ошибки черновика WB')
            if reasons:
                problems.setdefault(vendor,set()).update(reasons)
    return {f'wildberries:vendor:{v}':f'🟣 WB · артикул продавца {v}: ошибка создания / редактирования карточки\nПричина: '+ '; '.join(sorted(reasons)) for v,reasons in problems.items()}


def ozon_cards(rows):
    """Only explicit failure/approval. Missing/unknown status never clears an issue."""
    result, healthy = {}, set()
    for r in rows:
        pid = r.get('id')
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError('Нет product_id Ozon')
        subject = f'ozon:{pid}'
        s = r.get('statuses')
        if not isinstance(s, dict):
            raise ValueError('Нет статусов карточки Ozon')
        errors = r.get('errors')
        if not isinstance(errors, list):
            raise ValueError('Нет списка ошибок карточки Ozon')
        state = str(s.get('state', '')).lower()
        moderation = str(s.get('moderate_status', '')).lower()
        validation = str(s.get('validation_status', '')).lower()
        name = str(s.get('state_name') or '')
        # No inference from visibility, archived flag, inventory or selling status.
        blocked = state == 'blocked' or name.casefold() == 'заблокирован'
        failed = s.get('is_failed') is True or moderation == 'declined' or validation == 'fail'
        details = []
        for e in errors:
            if not isinstance(e, dict):
                raise ValueError('Некорректная ошибка Ozon')
            text = e.get('texts') or {}
            if not isinstance(text, dict):
                raise ValueError('Некорректное описание ошибки Ozon')
            details.append(str(text.get('description') or e.get('message') or e.get('code') or 'Ошибка без описания'))
        if blocked or failed or errors:
            rule = 'card_blocked' if blocked else 'card_errors'
            label = 'карточка заблокирована' if blocked else 'карточке нужна проверка / доработка'
            reason = '; '.join(sorted(set(details))) or str(s.get('state_description') or s.get('state_failed') or name or 'Причина не указана площадкой')
            result[subject] = (rule, f'🔵 Ozon · артикул {r.get("offer_id") or pid} · product_id {pid}: {label}\nТовар: {r.get("name") or "Название не получено"}\nПричина: {reason}')
        elif s.get('is_failed') is False and moderation == 'approved' and validation == 'success':
            healthy.add(subject)
    return result, healthy


def apply_snapshot(repo, shop_id, rule, messages, *, resolved_subjects=None, recovery='Проблема больше не указана в отчёте площадки'):
    """Persist state and event atomically. Same state never repeats, including restart."""
    notes = []
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with repo.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = {r['subject_key']: dict(r) for r in c.execute(
            'SELECT * FROM alert_state WHERE shop_id=? AND rule_key=?', (shop_id, rule))}
        for subject, message in sorted(messages.items()):
            fp = hashlib.sha256(message.encode()).hexdigest()
            previous = old.get(subject, {})
            changed = not previous.get('active') or previous.get('fingerprint') != fp
            c.execute('''INSERT INTO alert_state(shop_id,rule_key,subject_key,active,fingerprint,first_triggered_at,last_triggered_at,last_notified_at,updated_at)
                VALUES(?,?,?,1,?,?,?,?,?) ON CONFLICT(shop_id,rule_key,subject_key) DO UPDATE SET
                active=1,fingerprint=excluded.fingerprint,last_triggered_at=excluded.last_triggered_at,
                last_notified_at=CASE WHEN ? THEN excluded.last_notified_at ELSE alert_state.last_notified_at END,updated_at=excluded.updated_at''',
                (shop_id,rule,subject,fp,now,now,now,now,changed))
            if changed:
                notes.append(AlertNotification(rule,subject,'critical' if rule=='card_blocked' else 'warning',message))
        for subject, previous in old.items():
            if not previous['active'] or subject in messages:
                continue
            if resolved_subjects is not None and subject not in resolved_subjects:
                continue
            c.execute('UPDATE alert_state SET active=0,last_resolved_at=?,updated_at=? WHERE shop_id=? AND rule_key=? AND subject_key=?',
                (now,now,shop_id,rule,subject))
            market, _, sku = subject.partition(':')
            notes.append(AlertNotification(rule,subject,'resolved',f'✅ {marketplace_label(market)} · {sku}: {recovery}'))
        for n in notes:
            c.execute('INSERT INTO alert_events(shop_id,rule_key,subject_key,severity,message,created_at) VALUES(?,?,?,?,?,?)',
                (shop_id,n.rule_key,n.subject_key,n.severity,n.message,now))
    return notes


async def check_product_status(ctx):
    if ctx.demo_mode():
        return []
    notes = []
    repo, shop = ctx.repository, ctx.shop_id
    async with ctx.operation_lock('product_status'):
        sources = []
        if ctx.wb_connection_id is not None and ctx.collector.wb:
            sources.append(('wildberries:blocked',ctx.wb_connection_id,ctx.collector.wb.blocked_cards))
            sources.append(('wildberries:errors',ctx.wb_connection_id,ctx.collector.wb.card_errors))
        if ctx.ozon_connection_id is not None and ctx.collector.ozon:
            sources.append(('ozon:cards',ctx.ozon_connection_id,ctx.collector.ozon.card_status_snapshot))
        for source, connection_id, fetch in sources:
            key = 'card_check:' + source
            now = datetime.now(timezone.utc)
            last = repo.get_job_state(shop,key)
            if last:
                try:
                    if (now-datetime.fromisoformat(last)).total_seconds() < 3600:
                        continue
                except ValueError:
                    pass
            # Persist attempts as well: denied/429 responses must not create request storms.
            repo.set_job_state(shop,key,now.isoformat(timespec='seconds'))
            try:
                response = await fetch()
                if not response.ok:
                    raise ValueError(f'API HTTP {response.status_code or "недоступен"}. Проверьте доступ к товарам и права ключа.')
                if source == 'wildberries:blocked':
                    messages = wb_blocked(response.data)
                    repo.record_success(connection_id,'card_status/blocked',now.date().isoformat(),response.data,[])
                    notes += apply_snapshot(repo,shop,'card_blocked',messages,
                        resolved_subjects={r['subject_key'] for r in repo.active_alert_states(shop) if r['subject_key'].startswith('wildberries:')},
                        recovery='Карточка больше не числится в отчёте блокировок WB. Это не подтверждает наличие в продаже.')
                elif source == 'wildberries:errors':
                    messages = wb_errors(response.data)
                    repo.record_success(connection_id,'card_status/errors',now.date().isoformat(),response.data,[])
                    notes += apply_snapshot(repo,shop,'card_errors',messages,
                        resolved_subjects={r['subject_key'] for r in repo.active_alert_states(shop) if r['subject_key'].startswith('wildberries:vendor:')},
                        recovery='Ошибка больше не указана в списке черновиков WB. Публикация карточки не подтверждена.')
                else:
                    items = rows_at(response.data,'items')
                    issues, healthy = ozon_cards(items)
                    repo.record_success(connection_id,'card_status/ozon',now.date().isoformat(),response.data,[])
                    for rule in ('card_blocked','card_errors'):
                        messages = {s:m for s,(r,m) in issues.items() if r==rule}
                        # A changed problem replaces the old category without claiming recovery.
                        opposite = {s for s,(r,m) in issues.items() if r!=rule}
                        notes += apply_snapshot(repo,shop,rule,messages,resolved_subjects=healthy,
                            recovery='Проверки карточки Ozon пройдены, ошибок нет. Наличие в продаже проверяется отдельно.')
                        with repo.db.connect() as c:
                            for subject in opposite:
                                c.execute('UPDATE alert_state SET active=0,last_resolved_at=?,updated_at=? WHERE shop_id=? AND rule_key=? AND subject_key=?',
                                    (now.isoformat(),now.isoformat(),shop,rule,subject))
                notes += apply_snapshot(repo,shop,'card_source',{},resolved_subjects={source},recovery='Проверка карточек снова доступна')
            except Exception as exc:
                # Do not echo arbitrary API payloads or exception strings (may contain credentials).
                reason = str(exc) if isinstance(exc,ValueError) else 'Не удалось получить полный ответ API карточек'
                message = f'{marketplace_label(source.split(":")[0])}: проверка карточек не завершена. {reason} Прежние проблемы сохранены.'
                notes += apply_snapshot(repo,shop,'card_source',{source:message},resolved_subjects=set())
    return notes
