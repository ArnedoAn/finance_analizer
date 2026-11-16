"""Completed transaction vocabulary for davivienda."""

from .common import template

TEMPLATES = (
    template('compra', 'withdrawal', 'compra(?:\\s+(?:aprobada|exitosa))?|compraste|realizaste\\s+(?:una\\s+)?compra', False),
    template('pago', 'withdrawal', 'pagaste|pago\\s+exitoso', True),
    template('transferencia', 'withdrawal', 'transferiste|enviaste', True),
    template('abono', 'deposit', 'recibiste|abono(?:\\s+recibido)?', True),
)
