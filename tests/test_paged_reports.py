"""Report pagination through actual handlers, without external requests."""
from datetime import date
from html.parser import HTMLParser
import json
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
import pytest

from app.bot.report_cards import send_daily_card
from app.reports.cards import DailyCardText
from app.reports.products import ProductRank, ProductReport, StockRisk
from app.reports.text import paginate_report_html, utf16_length
from app.storage import Database

from test_navigation import ui, press, callback, inline_data
from test_report_cards import tap, stored


class Parsed(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]
        self.codes=[]
        self.text=[]

    def handle_starttag(self,tag,attrs):
        self.stack.append(tag)
        if tag=='code':self.codes.append('')

    def handle_endtag(self,tag):
        assert self.stack.pop()==tag

    def handle_data(self,value):
        self.text.append(value)
        if self.stack and self.stack[-1]=='code':self.codes[-1]+=value


def parsed(text):
    parser=Parsed();parser.feed(text);assert not parser.stack
    return parser


def stock_report(count=36):
    rows=[StockRisk('wildberries' if index%2 else 'ozon', 'Полное название товара 😀 '+str(index),
        f'00{index} & WB.',index+3,0,1,index+3,14,'2026-10-04T06:09:00+00:00') for index in range(count)]
    return ProductReport(date(2026,10,3),date(2026,10,3),1,stock_risks=rows,
                         marketplaces=('wildberries','ozon'))


def reports(ui):
    return [call for call in ui.telegram.methods if isinstance(call,SendMessage) and 'Остатки и запас' in call.text]


def snapshot(ui,message_id):
    return ui.repo.paged_report(ui.bot.id,101,message_id)


def check_buttons(markup):
    assert not any(button.copy_text for row in markup.inline_keyboard for button in row)


def test_html_pages_preserve_full_names_and_exact_code_values_with_multiline_tags():
    source='<b>Отчёт</b>\n<blockquote>\n'+ '\n'.join(
        f'• Полное название №{index} 😀 &amp; склад\n  Артикул <code>00{index} &amp; WB.</code>\n  Остаток: 3 шт.' for index in range(30))+'\n</blockquote>'
    pages=paginate_report_html(source)
    assert len(pages)>1 and all(utf16_length(page)<=3500 for page in pages)
    contents=[parsed(page) for page in pages]
    assert [code for item in contents for code in item.codes]==[f'00{index} & WB.' for index in range(30)]
    combined=''.join(''.join(item.text) for item in contents)
    assert all(f'Полное название №{index} 😀 & склад' in combined for index in range(30))


def test_oversized_single_html_line_preserves_entities_formatting_and_all_text():
    source='<b>'+('Название 😀 &amp; склад '*1200)+'</b>'
    pages=paginate_report_html(source)
    assert len(pages)>1 and all(utf16_length(page)<=3500 for page in pages)
    assert ''.join(''.join(parsed(page).text) for page in pages)==''.join(parsed(source).text)


@pytest.mark.asyncio
async def test_stock_pages_are_one_message_without_copy_buttons_and_keep_all_rows(ui,monkeypatch):
    report=stock_report()
    monkeypatch.setattr('app.bot.handlers.build_product_report',lambda *a,**kw: report)
    ui.ctx.collect_inventory=AsyncMock(return_value=[])
    await press(ui,'/stocks')
    ui.ctx.collect_inventory.reset_mock()
    assert len(reports(ui))==1
    message=ui.telegram.messages[(101,ui.telegram.sequence)]
    saved=snapshot(ui,message.message_id);pages=json.loads(saved['pages_json'])
    assert len(pages)>1
    values=[code for page in pages for code in parsed(page).codes]
    assert sorted(values)==sorted(row.sku for row in report.stock_risks)
    assert '04.10.2026 09:09' in '\n'.join(pages)
    check_buttons(message.reply_markup)
    first=message.text
    await callback(ui,message.message_id,inline_data(ui,message.message_id,'▶️'))
    changed=ui.telegram.messages[(101,message.message_id)]
    assert changed.text!=first and 'Страница 2/' in changed.text
    await callback(ui,message.message_id,inline_data(ui,message.message_id,'◀️'))
    assert ui.telegram.messages[(101,message.message_id)].text==first
    assert len(reports(ui))==1
    ui.ctx.collect_inventory.assert_not_called()


@pytest.mark.asyncio
async def test_restart_and_shop_switch_keep_original_snapshot_without_rebuilding(ui,monkeypatch):
    report=stock_report()
    monkeypatch.setattr('app.bot.handlers.build_product_report',lambda *a,**kw: report)
    await press(ui,'/stocks');mid=ui.telegram.sequence
    second=ui.repo.ensure_shop(ui.shop.seller_id,'Second')
    ui.repo.grant_shop_access(101,second.id,'owner');ui.repo.ensure_shop_preferences(second.id)
    ui.ctx.shop_id=second.id
    ui.dp=ui.new_dispatcher()
    monkeypatch.setattr('app.bot.handlers.build_product_report',lambda *a,**kw: pytest.fail('Snapshot must not be rebuilt'))
    await callback(ui,mid,'report_page:1')
    assert snapshot(ui,mid)['shop_id']==ui.shop.id and snapshot(ui,mid)['current_page']==1
    assert 'Страница 2/' in ui.telegram.messages[(101,mid)].text
    count=len([m for m in ui.telegram.methods if isinstance(m,EditMessageText)])
    ui.repo.revoke_shop_access(101,ui.shop.id)
    await callback(ui,mid,'report_page:0')
    assert len([m for m in ui.telegram.methods if isinstance(m,EditMessageText)])==count
    assert 'Нет доступа' in ui.telegram.methods[-1].text


@pytest.mark.asyncio
async def test_paging_rechecks_initial_permission_and_does_not_send_on_edit_failure(ui,monkeypatch):
    report=stock_report()
    monkeypatch.setattr('app.bot.handlers.build_product_report',lambda *a,**kw: report)
    await press(ui,'/stocks');mid=ui.telegram.sequence
    with ui.repo.db.connect() as conn:
        conn.execute("UPDATE telegram_paged_reports SET permission='manage' WHERE message_id=?",(mid,))
    ui.repo.grant_shop_access(101,ui.shop.id,'viewer')
    await callback(ui,mid,'report_page:1')
    assert snapshot(ui,mid)['current_page']==0
    ui.repo.grant_shop_access(101,ui.shop.id,'owner')
    original=ui.telegram.request
    async def fail_edit(bot,method,**kwargs):
        if isinstance(method,EditMessageText):raise TelegramBadRequest(method,'message to edit not found')
        return await original(bot,method,**kwargs)
    ui.bot.session.make_request=AsyncMock(side_effect=fail_edit)
    await callback(ui,mid,'report_page:1')
    assert snapshot(ui,mid)['current_page']==0 and len(reports(ui))==1


@pytest.mark.asyncio
async def test_page_bounds_and_stale_or_malformed_callbacks_do_not_create_messages(ui,monkeypatch):
    monkeypatch.setattr('app.bot.handlers.build_product_report',lambda *a,**kw:stock_report())
    await press(ui,'/stocks');mid=ui.telegram.sequence
    pages=json.loads(snapshot(ui,mid)['pages_json'])
    await callback(ui,mid,'report_page:999')
    assert snapshot(ui,mid)['current_page']==len(pages)-1
    await callback(ui,mid,'report_page:-1')
    assert snapshot(ui,mid)['current_page']==0
    await callback(ui,mid,'report_page:bad')
    assert isinstance(ui.telegram.methods[-1],AnswerCallbackQuery) and ui.telegram.methods[-1].show_alert
    with ui.repo.db.connect() as conn:conn.execute('DELETE FROM telegram_paged_reports')
    await callback(ui,mid,'report_page:1')
    assert 'Откройте раздел' in ui.telegram.methods[-1].text and len(reports(ui))==1


@pytest.mark.asyncio
async def test_daily_details_pages_keep_summary_and_resume_after_restart(ui):
    details='🔎 <b>Подробнее</b>\n'+'\n'.join(f'• WB · артикул <code>00{index}</code> · товар {index}' for index in range(80))
    text=DailyCardText('📊 <b>Сводка: 157 шт.</b>',details,details)
    message=await send_daily_card(ui.bot,ui.ctx,101,date(2026,10,3),user_id=101,text=text)
    await tap(ui,message,'details')
    first=ui.telegram.messages[(101,message.message_id)]
    assert 'Страница 1/' in first.text
    ui.dp=ui.new_dispatcher()
    await tap(ui,message,'page:1')
    changed=ui.telegram.messages[(101,message.message_id)]
    assert changed.text.startswith(text.summary_html+'\n\n') and 'Страница 2/' in changed.text
    assert stored(ui,message)['page']==1
    check_buttons(changed.reply_markup)
    await tap(ui,message,'collapse')
    assert stored(ui,message)['page']==0 and ui.telegram.messages[(101,message.message_id)].text==text.summary_html
    assert len([call for call in ui.telegram.methods if isinstance(call,SendMessage)])==1


@pytest.mark.asyncio
async def test_long_top_report_codes_all_articles_and_does_not_shorten_names(ui,monkeypatch):
    report=stock_report(0)
    object.__setattr__(report,'top',{'ozon':[ProductRank('ozon',i,'Полное длинное название '+str(i)+' 😀'*10,
        f'SKU:{i} & test',1,10) for i in range(18)]})
    monkeypatch.setattr('app.bot.handlers.build_product_report',lambda *a,**kw:report)
    await press(ui,'/products')
    saved=snapshot(ui,ui.telegram.sequence)
    pages=json.loads(saved['pages_json'])
    assert len(pages)>1
    assert [code for page in pages for code in parsed(page).codes]==[row.sku for row in report.top['ozon']]
    assert all(row.name in ''.join(''.join(parsed(page).text) for page in pages) for row in report.top['ozon'])


def test_schema18_upgrade_adds_pages_without_changing_daily_snapshots(tmp_path):
    from app.storage.database import MIGRATIONS
    db=Database(tmp_path/'old.sqlite3')
    with db.connect() as conn:
        conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version,migration in MIGRATIONS.items():
            if version==19:break
            migration(conn);conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
        assert 'page' not in [row[1] for row in conn.execute('PRAGMA table_info(telegram_report_cards)')]
    from app.storage import LATEST_SCHEMA_VERSION
    assert db.initialize_safely()==LATEST_SCHEMA_VERSION
    assert list((tmp_path/'backups').glob('pre_migration_v18_*.sqlite3'))
    with db.connect() as conn:
        assert 'page' in [row[1] for row in conn.execute('PRAGMA table_info(telegram_report_cards)')]
