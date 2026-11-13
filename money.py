"""Internal USD uses integer cents; provider amounts are never floats."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

MAX_CENTS = 10**12


def rate_value(value):
    try:
        rate=Decimal(str(value))
        if not rate.is_finite() or not Decimal('0.01')<=rate<=Decimal('1000000'):
            raise ValueError
        return rate
    except (InvalidOperation,ValueError):
        raise ValueError('Курс: от 0.01 до 1000000 Stars за $1') from None


def usd_cents(value, positive=False):
    """Parse a dollar amount ('12.5', Decimal('3.10'), 7) into integer cents.

    Rejects negative values, exponent notation, more than two decimals and amounts
    above $10 000 000 000. Zero is accepted (balances and costs may be zero) unless
    ``positive`` is true, which callers use for payments and top-ups.
    """
    try:
        if isinstance(value,bool) or (isinstance(value,str) and ('e' in value.lower() or not value.strip())):
            raise ValueError
        number=Decimal(str(value).strip())
        if not number.is_finite():
            raise ValueError
        cents=number*100
        if cents!=cents.to_integral_value() or cents<0 or cents>MAX_CENTS or (positive and cents==0):
            raise ValueError
        return int(cents)
    except (ValueError,InvalidOperation):
        raise ValueError('Укажите сумму в долларах, не более двух знаков после точки') from None


def stars_to_cents(stars,rate,rounding=ROUND_HALF_UP):
    return int((Decimal(stars)*100/rate_value(rate)).quantize(Decimal(1),rounding=rounding))


def credit_cents(stars,rate):
    """Stars paid by a buyer converted to wallet credit: never round in the buyer's favour."""
    return stars_to_cents(stars,rate,ROUND_FLOOR)


def cents_to_stars(cents,rate):
    return int((Decimal(cents)*rate_value(rate)/100).to_integral_value(rounding=ROUND_CEILING))


def format_usd(cents):
    return f'${Decimal(cents)/100:.2f}'
