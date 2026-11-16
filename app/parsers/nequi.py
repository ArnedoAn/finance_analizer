"""Completed transaction vocabulary for nequi."""

from .common import template

TEMPLATES = (
    template('pagaste', 'withdrawal', 'pagaste', False),
    template('enviaste', 'withdrawal', 'enviaste|transferiste|sacaste', True),
    template('recibiste', 'deposit', 'recibiste|metiste', True),
)
