"""Shop permissions and shared visibility rules for employee roles."""
from __future__ import annotations


ROLE_LABELS = {'owner': 'Владелец', 'accountant': 'Бухгалтер', 'manager': 'Менеджер',
               'viewer': 'Наблюдатель (старый доступ)'}
ASSIGNABLE_ROLES = ('owner', 'accountant', 'manager')
PERMISSIONS = frozenset({'view', 'operate', 'manage', 'finance', 'costs', 'settings', 'technical'})
_ROLE_PERMISSIONS = {
    'owner': PERMISSIONS,
    'accountant': frozenset({'view', 'operate', 'finance', 'costs', 'settings'}),
    'manager': frozenset({'view', 'operate'}),
    # Keep existing read-only accounts read-only; do not grant update rights
    # merely because new employee roles became available.
    'viewer': frozenset({'view', 'finance'}),
}


def normalize_role(role: str) -> str:
    value = str(role or '').strip().lower()
    value = {'analyst': 'accountant', 'владелец': 'owner', 'бухгалтер': 'accountant',
             'менеджер': 'manager'}.get(value, value)
    if value not in ROLE_LABELS:
        raise ValueError('Выберите роль: Владелец, Бухгалтер или Менеджер.')
    return value


def can_role(role: str | None, permission: str = 'view') -> bool:
    if permission not in PERMISSIONS:
        raise ValueError('unknown permission')
    return permission in _ROLE_PERMISSIONS.get('accountant' if role == 'analyst' else role, ())


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get('accountant' if role == 'analyst' else role, 'Нет доступа')


FINANCE_ACTIONS = frozenset({'finance', 'finance_update', 'management', 'sku_finance',
                            'reconcile', 'accruals', 'wb_accruals', 'sources', 'readiness'})
TECHNICAL_ACTIONS = frozenset({'shop_add', 'shop_profile', 'shop_archive', 'shop_archived',
    'shop_restore', 'shop_delete', 'profiles', 'backup', 'backups', 'restore', 'connect_check',
    'health', 'jobs', 'job_retry', 'diagnostics', 'demo_on', 'demo_off', 'help', 'technical_menu'})


def action_permission(action: str) -> str:
    if action in TECHNICAL_ACTIONS:
        return 'technical'
    if action in FINANCE_ACTIONS:
        return 'finance'
    if action in {'cost', 'import_costs', 'link'}:
        return 'costs'
    if action in {'setup', 'supply_defaults', 'supply_set'}:
        return 'settings'
    if action in {'users', 'user_add', 'user_remove'}:
        return 'manage'
    return 'view'


def item_visible(item, *, finance: bool, technical: bool) -> bool:
    """Apply the same task filtering to lists, old callbacks and exports."""
    get = item.get if isinstance(item, dict) else lambda key, default='': getattr(item, key, default)
    key = str(get('action_key', ''))
    category = str(get('category', ''))
    if category == 'finance' or key.startswith(('finance:', 'management:')):
        return finance
    if category == 'system' or key.startswith('system:'):
        return key.startswith('alert:api_stale:') or technical
    return True
