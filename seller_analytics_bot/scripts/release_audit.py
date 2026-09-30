"""Dependency-light release audit for Seller Analytics Bot.

Does not contact Telegram or marketplace APIs. Intended for CI/release packaging.
"""
from __future__ import annotations
import ast
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from app.storage import Database, LATEST_SCHEMA_VERSION


def _commands_and_buttons():
    kb=ast.parse((ROOT/'app/bot/keyboards.py').read_text(encoding='utf-8'))
    constants={}
    buttons={}
    for node in kb.body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            if isinstance(node.value,ast.Constant): constants[node.targets[0].id]=node.value.value
        if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name) and node.target.id=='COMMAND_BUTTONS':
            for k,v in zip(node.value.keys,node.value.values):
                key=ast.literal_eval(k)
                buttons[key]=v.value if isinstance(v,ast.Constant) else constants[v.id]
    handlers=ast.parse((ROOT/'app/bot/handlers.py').read_text(encoding='utf-8'))
    commands=[]; labels=[]
    for node in ast.walk(handlers):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='Command':
            commands.append(ast.literal_eval(node.args[0]))
        if isinstance(node,ast.Compare) and len(node.ops)==1 and isinstance(node.ops[0],ast.Eq):
            left,right=node.left,node.comparators[0]
            if not (isinstance(left,ast.Attribute) and left.attr=='text'): continue
            if isinstance(right,ast.Constant) and isinstance(right.value,str): labels.append(right.value)
            elif isinstance(right,ast.Name) and right.id in constants: labels.append(constants[right.id])
            elif isinstance(right,ast.Subscript) and isinstance(right.value,ast.Name) and right.value.id=='COMMAND_BUTTONS':
                labels.append(buttons[ast.literal_eval(right.slice)])
    return commands,buttons,labels


def main() -> int:
    checks={}; errors=[]; warnings=[]
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    checks['version']=version
    checks['schema_version']=LATEST_SCHEMA_VERSION

    readme=(ROOT/'README.md').read_text(encoding='utf-8')
    changelog=(ROOT/'CHANGELOG.md').read_text(encoding='utf-8')
    checks['version_in_readme']=version in readme
    checks['version_in_changelog']=bool(re.search(rf'^##\s+{re.escape(version)}\b',changelog,re.M))
    if not checks['version_in_readme']: errors.append('VERSION is not documented in README.md')
    if not checks['version_in_changelog']: errors.append('VERSION is not the documented changelog release')

    commands,buttons,labels=_commands_and_buttons()
    checks['slash_commands']=len(set(commands))
    checks['canonical_buttons']=len(buttons)
    missing_mapping=sorted(set(commands)-set(buttons))
    missing_handlers=sorted(k for k,v in buttons.items() if v not in labels)
    duplicate_labels=sorted({x for x in labels if labels.count(x)>1})
    checks['commands_without_button_mapping']=missing_mapping
    checks['buttons_without_handler']=missing_handlers
    checks['duplicate_exact_button_handlers']=duplicate_labels
    if missing_mapping or missing_handlers or duplicate_labels:
        errors.append('Telegram command/button invariant failed')

    with tempfile.TemporaryDirectory(prefix='sellerbot-release-') as td:
        db=Database(Path(td)/'audit.sqlite3')
        checks['fresh_schema']=db.initialize_safely(Path(td)/'backups')
        checks['sqlite_quick_check']=db.quick_check()
        if checks['fresh_schema']!=LATEST_SCHEMA_VERSION or not checks['sqlite_quick_check']:
            errors.append('Fresh database bootstrap/integrity failed')

    required=['main.py','requirements.txt','.env.example','README.md','CHANGELOG.md','SECURITY.md','RELEASE_CHECKLIST.md']
    missing_files=[x for x in required if not (ROOT/x).exists()]
    checks['missing_release_files']=missing_files
    if missing_files: errors.append('Required release files are missing')

    env=(ROOT/'.env.example').read_text(encoding='utf-8')
    leaked=[]
    for key in ('TELEGRAM_BOT_TOKEN','OZON_API_KEY','WB_API_TOKEN','OZON_PERF_CLIENT_SECRET'):
        m=re.search(rf'(?m)^{re.escape(key)}=(.+)$',env)
        if m and m.group(1).strip(): leaked.append(key)
    checks['example_secrets_with_values']=leaked
    if leaked: errors.append('.env.example contains secret values')

    hsrc=(ROOT/'app/bot/handlers.py').read_text(encoding='utf-8')
    global_funcs=('cmd_backup','cmd_backups','cmd_restore','restore_file','cmd_shop_add','cmd_shop_profile','cmd_profiles')
    htree=ast.parse(hsrc); funcs={n.name:n for n in ast.walk(htree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    unguarded=[]
    for fn in global_funcs:
        node=funcs.get(fn)
        if node is None: unguarded.append(fn); continue
        if not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='is_system_owner' for n in ast.walk(node)):
            unguarded.append(fn)
    checks['unguarded_global_handlers']=unguarded
    if unguarded: errors.append('Global administrative handlers lack system-owner guard')

    checks['ok']=not errors
    print(json.dumps({'ok':not errors,'checks':checks,'warnings':warnings,'errors':errors},ensure_ascii=False,indent=2))
    return 0 if not errors else 1

if __name__=='__main__':
    raise SystemExit(main())
