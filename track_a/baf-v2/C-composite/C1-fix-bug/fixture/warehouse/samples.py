"""Полные контрольные группы для выборочной проверки."""


def full_samples(labels, sample_size):
    return [
        labels[start:start + sample_size]
        for start in range(0, len(labels) - sample_size + 1, sample_size)
    ]
