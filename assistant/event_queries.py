"""Date-only events use calendar dates; timed events use actual instants."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q


def event_overlap(start, end, timezone_name):
    zone = ZoneInfo(timezone_name)
    dated = Q(all_day=True, start_date__isnull=False, end_date__isnull=False)
    timed = Q(all_day=False) | Q(start_date__isnull=True) | Q(end_date__isnull=True)
    if start is not None:
        dated &= Q(end_date__gt=start.astimezone(zone).date())
        timed &= Q(ends_at__gt=start)
    if end is not None:
        after_last_day = (end - timedelta(microseconds=1)).astimezone(zone).date() + timedelta(days=1)
        dated &= Q(start_date__lt=after_last_day)
        timed &= Q(starts_at__lt=end)
    return dated | timed
