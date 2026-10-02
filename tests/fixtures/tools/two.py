"""A module without a main, used by one.py."""


def count(items):
    total = 0
    for item in items:
        if item:  # a real item
            total += 1
    return total
