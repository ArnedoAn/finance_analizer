"""Completed transaction vocabulary for nu."""

from .common import template

TEMPLATES = (
    template('compra', 'withdrawal', 'compra\\s+(?:aprobada|realizada)|compraste|pagaste', False),
    template('recibiste', 'deposit', 'recibiste|abono\\s+recibido', True),
    template('enviaste', 'withdrawal', 'enviaste|transferiste', True),
)
