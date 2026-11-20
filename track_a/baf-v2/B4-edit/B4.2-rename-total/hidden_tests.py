import json
import sys
sys.dont_write_bytecode = True
from billing.pricing import compute_total
from invoice import invoice_total
cases = [([], 0), (['0.10', '0.20'], 0), (['1.25', '2.50'], '0.20'), (['-1.00', '3.00'], 0)]
actual = [str(compute_total(prices, tax)) for prices, tax in cases]
actual.append(str(invoice_total(['10.00'])))
print(json.dumps(actual))
