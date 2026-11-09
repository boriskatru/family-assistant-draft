"""Human-readable event dates without artificial midnight appointments."""
from datetime import timedelta
from zoneinfo import ZoneInfo


def event_span(event, timezone_name):
    if event.all_day and event.start_date and event.end_date:
        last_day = event.end_date - timedelta(days=1)
        label = f'{event.start_date:%d.%m.%Y}'
        if last_day != event.start_date:
            label += f' — {last_day:%d.%m.%Y}'
        return label + ' · без времени'
    zone = ZoneInfo(timezone_name)
    start, end = event.starts_at.astimezone(zone), event.ends_at.astimezone(zone)
    if start.date() == end.date():
        return f'{start:%d.%m.%Y %H:%M}–{end:%H:%M} ({timezone_name})'
    return f'{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M} ({timezone_name})'
