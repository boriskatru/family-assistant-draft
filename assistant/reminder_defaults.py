"""Default lead times for Telegram event reminders, in minutes."""
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


_CALL_TITLE = re.compile(r'\b(?:созвон|видеозвонок|видеоконференция|онлайн[- ]встреча)\b', re.I)
_CALL_LOCATION = re.compile(
    r'\bhttps?://(?:meet\.google\.com/|(?:[\w-]+\.)?zoom\.us/(?:j|my|w)/|'
    r'teams\.microsoft\.com/(?:l/meetup-join|meet)/|teams\.live\.com/meet/|'
    r'telemost\.yandex\.ru/j/)', re.I,
)
_TRAVEL_TITLE = re.compile(r'^поездка\s+(?:в|во|на|из|до|к|за)\s+', re.I)


def default_event_reminder_minutes(*, kind='meeting', title='', location='', meeting_url='', dated_trip=False):
    if kind == 'call' or meeting_url:
        return 60
    if kind == 'travel':
        return 15 if dated_trip else 1440
    if kind in ('meeting', 'activity'):
        if _CALL_TITLE.search(title) or _CALL_LOCATION.search(location):
            return 60
        if not dated_trip and _TRAVEL_TITLE.match(title):
            return 1440
    return 15


def event_reminder_minutes(event):
    if event.all_day:
        return 0
    return default_event_reminder_minutes(
        kind=event.kind, title=event.title, location=event.location, meeting_url=event.meeting_url,
        dated_trip=bool(event.trip_id and event.trip.starts_on),
    )


def event_reminder_due(event, offset_minutes, kind='reminder'):
    """All-day defaults arrive at 09:00; custom offsets remain from midnight."""
    if kind.startswith('at:'):
        zone = ZoneInfo(event.timezone)
        day = event.start_date or event.starts_at.astimezone(zone).date()
        return datetime.combine(day, time.fromisoformat(kind[3:]), zone) - timedelta(minutes=offset_minutes)
    if event.all_day and kind == 'reminder':
        zone = ZoneInfo(event.timezone)
        day = event.start_date or event.starts_at.astimezone(zone).date()
        return datetime.combine(day, time(9), zone)
    return event.starts_at.astimezone(timezone.utc) - timedelta(minutes=offset_minutes)


def task_reminder_due(task, offset_minutes, kind='reminder'):
    if task.due_at is None:
        return None
    if kind.startswith('at:'):
        zone = ZoneInfo(task.owner.timezone)
        return datetime.combine(task.due_at.astimezone(zone).date(), time.fromisoformat(kind[3:]), zone) - timedelta(minutes=offset_minutes)
    return task.due_at - timedelta(minutes=offset_minutes)
