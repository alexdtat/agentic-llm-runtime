from datetime import datetime

def parse_date(value):
    """Parse a local calendar date."""
    return datetime.strptime(value, "%Y-%m-%d")
