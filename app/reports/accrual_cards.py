"""Compact finance summaries with explicit report periods and signed corrections."""
from html import escape
from datetime import date

from app.services.money import money_sum
from .cards import rubles
from .dates import readable_text
from .text import paginate_report_html

LABELS = {
    'financial_sales':'Продажи по финансовому источнику', 'commission':'Комиссия',
    'logistics':'Логистика', 'services':'Услуги и прочие удержания',
    'ads_already_in_services':'Из услуг — реклама', 'other_components':'Другие составляющие итога',
    'marketplace_net':'Начислено после удержаний', 'goods_payable':'К перечислению за товар',
    'storage':'Хранение', 'acceptance':'Приёмка', 'penalties':'Штрафы',
    'compensation':'Компенсации и доплаты', 'bank_payment':'К оплате по отчёту',
}


def accrual_totals(ledger, marketplace):
    keys = ('financial_sales','commission','logistics','services','ads_already_in_services','other_components','marketplace_net') if marketplace=='ozon' else (
        'financial_sales','goods_payable','logistics','storage','acceptance','services','penalties','compensation','bank_payment')
    return {key:money_sum(row[key] for row in ledger.rows if row.get(key) is not None)
            if (marketplace=='ozon' and ledger.loaded_dates) or any(row.get(key) is not None for row in ledger.rows) else None for key in keys}


def accrual_warnings(ledger, marketplace):
    expected=(date.fromisoformat(ledger.end)-date.fromisoformat(ledger.start)).days+1
    warnings=[]
    if len(ledger.loaded_dates)<expected:
        warnings.append('Период загружен не полностью. Показаны только сохранённые данные.' if marketplace=='ozon' else
                        'Не все выбранные даты имеют опубликованный и загруженный отчёт WB.')
    if marketplace=='wb' and ledger.rows:
        if any(not row['report_from'] or not row['report_to'] for row in ledger.rows):
            warnings.append('Период части отчётов WB не указан в сохранённых данных.')
        if any(row['report_from']!=row['report_to'] for row in ledger.rows):
            warnings.append('Сумма относится ко всему периоду отчёта WB.')
        totals=accrual_totals(ledger,marketplace)
        if any(any(row.get(key) is None for row in ledger.rows) for key in totals):
            warnings.append('Часть сумм есть не во всех отчётах; показана доступная часть. Пропуски не считаются нулём.')
    return warnings


def build_accrual_card(ledger, marketplace, shop_name, *, timezone='Europe/Moscow'):
    label='Ozon' if marketplace=='ozon' else 'WB'
    title=f'🧾 <b>Начисления {label}</b>'
    summary=[title,'🏪 '+escape(shop_name),
             ('Период: ' if marketplace=='ozon' else 'Даты сохранения отчётов: ')+ledger.start+' — '+ledger.end]
    totals=accrual_totals(ledger,marketplace)
    available=bool(ledger.loaded_dates) if marketplace=='ozon' else bool(ledger.rows)
    periods=sorted({(row['report_from'],row['report_to']) for row in ledger.rows}) if marketplace=='wb' else []
    if periods:
        summary += ['Периоды самих отчётов WB: '+escape('; '.join((a or '—')+' — '+(b or '—') for a,b in periods[:2]))]
        if len(periods)>2:summary.append(f'Ещё периодов: {len(periods)-2}. Смотрите «Подробнее».')
    if not available:
        summary += ['', '📭 Нет сохранённых начислений за этот период. Обновите финансы и откройте отчёт заново.']
    else:
        key='marketplace_net' if marketplace=='ozon' else 'bank_payment'
        text=rubles(totals[key]) if totals[key] is not None else 'значение не передано'
        summary += ['', LABELS[key]+': <b>'+text+'</b>']
        if marketplace=='ozon':
            summary += ['Продажи: <b>'+rubles(totals['financial_sales'])+'</b>',
                        'Удержания: <b>'+rubles(money_sum(totals[k] for k in ('commission','logistics','services')))+'</b>']
            if totals['other_components']:
                summary.append('Другие составляющие: <b>'+rubles(totals['other_components'])+'</b>')
            expected=(date.fromisoformat(ledger.end)-date.fromisoformat(ledger.start)).days+1
            summary.append(f'Загружено дней: {len(ledger.loaded_dates)}/{expected} · операций: {len(ledger.rows)}')
        else:
            summary.append('К перечислению за товар: <b>'+(rubles(totals['goods_payable']) if totals['goods_payable'] is not None else 'значение не передано')+'</b>')
            summary.append(f'Финансовых отчётов: {len(ledger.rows)}')
    summary.extend('⚠️ '+warning for warning in accrual_warnings(ledger,marketplace))
    summary.append('Поступление денег проверяется в банке. Это расчёт площадки.')
    details=['🔎 <b>Подробнее · '+label+'</b>']
    for key,value in totals.items():
        details.append(LABELS[key]+': <b>'+(rubles(value) if value is not None else 'значение не передано')+'</b>')
    details += ['', 'Реклама в услугах уже включена в удержания; повторно не вычитается.' if marketplace=='ozon' else
                    'Удержания из суммы за товар повторно не вычитаются.',
                'Начисления, новые заказы и чистая прибыль — разные показатели.']
    if marketplace=='wb':
        details += ['', '<b>Номера и периоды отчётов WB</b>']
        for row in ledger.rows:
            details += ['Отчёт <code>'+escape(str(row['report_id'] or '—'))+'</code> · '+escape((row['report_from'] or '—')+' — '+(row['report_to'] or '—'))]
    details += ['', 'Источник: сохранённые финансовые операции Ozon.' if marketplace=='ozon' else 'Источник: сохранённые финансовые отчёты WB.']
    verified=max((row['verified_at'] for row in ledger.rows),default='')
    if verified:details.append('Источник проверен: '+verified)
    details.append('CSV сохраняет исходные коды полей. В Excel есть русские названия и лист «Описание полей».')
    pages=paginate_report_html(readable_text('\n'.join(details),tz=timezone),limit=1100,line_limit=18)
    return {'summary':readable_text('\n'.join(summary),tz=timezone),'pages':pages,'available':available}
