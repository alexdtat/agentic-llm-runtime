"""Группировка заказов для отправки."""


def plan_orders(orders, batch_size):
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    full_length = len(orders) - len(orders) % batch_size
    return [
        orders[start:start + batch_size]
        for start in range(0, full_length, batch_size)
    ]
