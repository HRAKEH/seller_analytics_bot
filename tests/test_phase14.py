import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _menu_model():
    tree = ast.parse((ROOT / 'app/bot/keyboards.py').read_text(encoding='utf-8'))
    const = {}
    mapping = {}
    categories = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Constant):
                const[name] = node.value.value
                if name.startswith('MENU_'):
                    categories[name] = node.value.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'COMMAND_BUTTONS':
            assert isinstance(node.value, ast.Dict)
            for key_node, value_node in zip(node.value.keys, node.value.values):
                key = ast.literal_eval(key_node)
                if isinstance(value_node, ast.Constant):
                    value = value_node.value
                elif isinstance(value_node, ast.Name):
                    value = const[value_node.id]
                else:
                    raise AssertionError(ast.dump(value_node))
                mapping[key] = value
    return const, mapping, categories


def _handler_commands_and_buttons():
    const, mapping, _ = _menu_model()
    tree = ast.parse((ROOT / 'app/bot/handlers.py').read_text(encoding='utf-8'))
    commands = []
    labels = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Command':
            commands.append(ast.literal_eval(node.args[0]))
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            left, right = node.left, node.comparators[0]
            if not (isinstance(left, ast.Attribute) and left.attr == 'text'):
                continue
            if isinstance(right, ast.Constant) and isinstance(right.value, str):
                labels.append(right.value)
            elif isinstance(right, ast.Name) and right.id in const:
                labels.append(const[right.id])
            elif isinstance(right, ast.Subscript) and isinstance(right.value, ast.Name) and right.value.id == 'COMMAND_BUTTONS':
                labels.append(mapping[ast.literal_eval(right.slice)])
    return commands, labels


def test_every_slash_command_has_a_canonical_button_and_handler():
    _, mapping, _ = _menu_model()
    commands, labels = _handler_commands_and_buttons()
    assert set(commands) == set(mapping), 'Every slash command must be represented in COMMAND_BUTTONS'
    missing = {cmd: mapping[cmd] for cmd in commands if mapping[cmd] not in labels}
    assert missing == {}, f'Commands without Telegram button handlers: {missing}'


def test_button_handlers_have_no_exact_text_collisions():
    _, labels = _handler_commands_and_buttons()
    duplicates = {label: count for label, count in Counter(labels).items() if count > 1}
    assert duplicates == {}, f'Duplicate exact Telegram button handlers: {duplicates}'


def test_main_menu_is_logically_grouped_and_emoji_first():
    _, mapping, categories = _menu_model()
    expected = {
        '📊 Отчёты', '📦 Товары', '💰 Деньги и реклама', '🚚 Поставки',
        '🚨 Проблемы', '🏪 Магазин', '🛠 Ещё', '🧰 Техническое',
    }
    assert set(categories.values()) == expected
    assert len(set(mapping.values())) == len(mapping)
    assert all(label and ord(label[0]) > 127 for label in [*expected, *mapping.values()])



def test_primary_workflows_are_rendered_by_keyboard_builders():
    _, mapping, _ = _menu_model()
    source = (ROOT / 'app/bot/keyboards.py').read_text(encoding='utf-8')
    builders = source[source.index('def main_keyboard'):]
    primary = {
        'shops','shop','settings','readiness','connect_check',
        'backfill','products','stocks','finance','ads','management','sku_finance','reconcile',
        'alerts','actions','action_history','supply','inbound','promotions','forecast_quality',
        'export','health','jobs','diagnostics',
    }
    aliases={
        'backfill':'📥 Догрузить данные',
        'export':'📤 Экспорт',
    }
    missing=[]
    for cmd in primary:
        canonical=f"COMMAND_BUTTONS['{cmd}']" in builders
        alias=aliases.get(cmd)
        if not canonical and not (alias and repr(alias) in builders):
            missing.append(cmd)
    assert missing == [], f'Primary workflows absent from button menus: {missing}'
