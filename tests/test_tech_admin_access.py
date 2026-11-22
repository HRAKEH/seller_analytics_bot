"""Separately granted support role: no shop-owner escalation via /tech."""
from dataclasses import replace
from app.access import action_permission, is_tech_admin
from app.bot.keyboards import MENU_TECH, service_keyboard, shop_keyboard


def test_support_role_is_explicit():
    from types import SimpleNamespace
    s=SimpleNamespace(owner_ids=(101,102), tech_admin_ids=(101,))
    assert is_tech_admin(s,101)
    assert not is_tech_admin(s,102)
    assert not is_tech_admin(s,103)
    s.tech_admin_ids=(103,)
    assert not is_tech_admin(s,103)


def test_only_support_actions_are_technical():
    for action in ('tech','technical_menu','backup','backups','restore','health','jobs',
                   'job_retry','diagnostics','ozon_push','demo_on','demo_off'):
        assert action_permission(action)=='technical'
    for action in ('shop_add','shop_profile','profiles','connect_check'):
        assert action_permission(action)=='manage'
    assert action_permission('users')=='manage'


def test_client_keeps_shop_controls_while_tech_menu_is_hidden():
    tech=[b.text for row in service_keyboard('owner',system_owner=True).keyboard for b in row]
    customer=[b.text for row in service_keyboard('owner',system_owner=False).keyboard for b in row]
    assert MENU_TECH in tech and MENU_TECH not in customer
    owner_shop=[b.text for row in shop_keyboard('owner',system_owner=True).keyboard for b in row]
    assert '➕ Добавить магазин' in owner_shop
    assert '🔐 Профиль ключей' in owner_shop


import pytest
from test_navigation import press, ui


@pytest.mark.asyncio
async def test_tech_menu_silent_for_other_shop_owner(ui):
    ui.repo.grant_shop_access(103,ui.shop.id,'owner')
    ui.ctx.settings=replace(ui.ctx.settings,owner_ids=(101,103),tech_admin_ids=(101,))
    before=len(ui.telegram.methods)
    await press(ui,'/tech',user=103)
    assert len(ui.telegram.methods)==before
    await press(ui,MENU_TECH,user=103)
    assert len(ui.telegram.methods)==before
    await press(ui,'/tech',user=101)
    assert len(ui.telegram.methods)>before


@pytest.mark.asyncio
async def test_client_owner_cannot_read_backup_via_old_command(ui):
    ui.repo.grant_shop_access(103,ui.shop.id,'owner')
    ui.ctx.settings=replace(ui.ctx.settings,owner_ids=(101,103),tech_admin_ids=(101,))
    await press(ui,'/backups',user=103)
    assert not any(getattr(m,'document',None) for m in ui.telegram.methods)
