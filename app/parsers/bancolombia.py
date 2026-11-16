"""Completed transaction vocabulary for bancolombia."""

from .common import template

TEMPLATES = (
    template('compraste', 'withdrawal', 'compraste|compra(?:\\s+(?:aprobada|exitosa))?|realizaste\\s+(?:una\\s+)?compra', False),
    template('pagaste', 'withdrawal', 'pagaste|pago\\s+(?:aprobado|exitoso)', True),
    template('transferiste', 'withdrawal', 'transferiste|enviaste', True),
    template('recibiste', 'deposit', 'recibiste|recibio|te\\s+llegaron', True),
    template('retiro', 'withdrawal', 'retiraste|retiro\\s+exitoso', False),
    template('refund', 'refund', 'devolucion\\s+de\\s+compra|reembolso(?:\\s+de\\s+compra)?|anulaste\\s+la\\s+compra', False),
)
