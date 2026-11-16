"""Completed transaction vocabulary for daviplata."""

from .common import template

TEMPLATES = (
    template('pagaste', 'withdrawal', 'pagaste|compra\\s+(?:aprobada|exitosa)|compraste', False),
    template('enviaste', 'withdrawal', 'enviaste|transferiste', True),
    template('recibiste', 'deposit', 'recibiste|recibio', True),
)
