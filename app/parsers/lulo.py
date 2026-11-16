"""Completed transaction vocabulary for lulo."""

from .common import template

TEMPLATES = (
    template('compra', 'withdrawal', 'compra\\s+realizada(?:\\s*\\n\\s*)?(?:(?:monto|valor)\\s*:?)?|compraste|compra\\s+aprobada', False),
    template('pagaste', 'withdrawal', 'pagaste', False),
    template('enviaste', 'withdrawal', 'enviaste|transferiste', True),
    template('recibiste', 'deposit', 'recibiste', True),
)
