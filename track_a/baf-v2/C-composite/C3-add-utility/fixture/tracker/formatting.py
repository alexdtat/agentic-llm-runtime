def format_minutes(minutes):
    hours, remaining = divmod(minutes, 60)
    return f"{hours}:{remaining:02d}"
