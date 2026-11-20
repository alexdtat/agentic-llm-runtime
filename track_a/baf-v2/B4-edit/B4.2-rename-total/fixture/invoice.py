from billing.pricing import calc_total

def invoice_total(prices):
    return calc_total(prices, tax_rate="0.20")
