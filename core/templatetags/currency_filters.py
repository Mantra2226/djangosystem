"""
CURRENCY TEMPLATE FILTERS (core/templatetags/currency_filters.py)

Centralized currency formatting filter for Kenyan Shillings (KSh / KES).
Supports custom symbols from django.conf.settings.CURRENCY_SYMBOL.
"""

from decimal import Decimal, InvalidOperation
from django import template
from django.conf import settings

register = template.Library()


@register.filter(name='currency')
def currency(value, arg=None):
    """
    Formats a numeric value into a currency string with thousands separators
    and 2 decimal places, using settings.CURRENCY_SYMBOL (default 'KSh').

    Examples:
        1250.5               -> "KSh 1,250.50"
        -137.55              -> "-KSh 137.55"
        0                    -> "KSh 0.00"
        None                 -> "-"
        ""                   -> "-"
        100, arg="neg"       -> "-KSh 100.00"
    """
    if value is None or value == '':
        return '-'

    symbol = getattr(settings, 'CURRENCY_SYMBOL', 'KSh')

    try:
        if isinstance(value, str):
            cleaned = value.replace(symbol, '').replace('$', '').replace(',', '').strip()
            num = Decimal(cleaned)
        elif isinstance(value, (int, float, Decimal)):
            num = Decimal(str(value))
        else:
            return '-'
    except (InvalidOperation, ValueError, TypeError):
        return '-'

    # If the caller requests explicit negation (e.g., for outflows or cost line items)
    if arg in ('neg', 'negative', True) and num > 0:
        num = -num

    if num < 0:
        return f"-{symbol} {abs(num):,.2f}"
    else:
        return f"{symbol} {num:,.2f}"
