from app.bot.keyboards import main_keyboard, reports_keyboard, service_keyboard, MENU_SERVICE, MENU_TECH, COMMAND_BUTTONS


def labels(markup):
    return {button.text for row in markup.keyboard for button in row}


def test_service_menu_is_private_to_technical_admin():
    for role in ('owner','accountant','manager','viewer'):
        assert MENU_SERVICE not in labels(main_keyboard(role))
        assert MENU_TECH not in labels(service_keyboard(role))
    assert MENU_SERVICE in labels(main_keyboard('owner',system_owner=True))
    assert MENU_TECH in labels(service_keyboard('owner',system_owner=True))
    assert COMMAND_BUTTONS['export'] in labels(reports_keyboard('owner'))
