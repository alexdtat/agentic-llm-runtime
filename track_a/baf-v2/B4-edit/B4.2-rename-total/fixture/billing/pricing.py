from decimal import Decimal

def calc_total(prices, tax_rate=0):
    subtotal = sum((Decimal(str(price)) for price in prices), Decimal("0"))
    return (subtotal * (Decimal("1") + Decimal(str(tax_rate)))).quantize(Decimal("0.01"))
