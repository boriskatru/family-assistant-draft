"""Validate public date/time inputs and keep the stored event interval coherent."""
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.tz import datetime_ambiguous, datetime_exists, resolve_imaginary
from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_date, parse_datetime

UTC = dt_timezone.utc
TIMING_FIELDS = frozenset({
    'starts_at', 'ends_at', 'timezone', 'end_timezone', 'all_day',
    'start_date', 'end_date', 'blocks_time',
})


def _date(value, name):
    try:
        parsed = value if isinstance(value, date) and not isinstance(value, datetime) else parse_date(str(value))
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        raise ValidationError(f'{name}: укажите дату YYYY-MM-DD.')
    return parsed


def _zone(value):
    try:
        return ZoneInfo(value)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ValidationError('Неизвестный часовой пояс IANA.') from None


def _datetime(value, zone):
    try:
        result = value if isinstance(value, datetime) else parse_datetime(str(value))
    except (TypeError, ValueError):
        result = None
    if result is None:
        raise ValidationError('Для события со временем нужны starts_at и ends_at в формате ISO.')
    if result.tzinfo is None or result.utcoffset() is None:
        result = result.replace(tzinfo=zone)
        if not datetime_exists(result) or datetime_ambiguous(result):
            raise ValidationError('Неоднозначное местное время: укажите UTC-смещение.')
    return result


def _midnight(day, zone):
    result = datetime.combine(day, time.min, zone)
    return resolve_imaginary(result) if not datetime_exists(result) else result


def normalise_event_timing(user, data, event=None):
    """Return complete timing fields for creates and timing edits, else {}.

    All-day end_date is exclusive; omission on create means a single day.
    Updating only start_date preserves an existing all-day event's day count.
    Newly created date-only events are informational unless blocks_time=True.
    Existing rows keep their blocking setting during unrelated edits.
    """
    if event is not None and not TIMING_FIELDS.intersection(data):
        return {}
    values = {k: v for k, v in data.items() if v is not None}
    previous_all_day = bool(event and event.all_day)
    all_day = values.get('all_day', previous_all_day if event else bool(values.get('start_date')))
    if type(all_day) is not bool:
        raise ValidationError('all_day должен быть логическим значением.')
    zone_name = values.get('timezone') or (event.timezone if event else user.timezone)
    zone = _zone(zone_name)
    end_zone_name = values.get('end_timezone') or (zone_name if all_day or 'timezone' in values or not event else event.end_timezone)
    end_zone = _zone(end_zone_name)
    blocks_time = values.get('blocks_time', event.blocks_time if event and all_day == previous_all_day else not all_day)
    if type(blocks_time) is not bool:
        raise ValidationError('blocks_time должен быть логическим значением.')

    if all_day:
        if end_zone_name != zone_name:
            raise ValidationError('Событие без времени использует один часовой пояс.')
        start_value = values.get('start_date', event.start_date if previous_all_day else None)
        start_day = _date(start_value, 'start_date')
        previous_days = ((event.end_date - event.start_date).days
                         if previous_all_day and event.start_date and event.end_date else 1)
        end_value = values.get('end_date')
        if end_value is None:
            end_day = start_day + timedelta(days=max(1, previous_days))
        else:
            end_day = _date(end_value, 'end_date')
        if end_day <= start_day:
            raise ValidationError('end_date не включается в событие и должна быть позже start_date.')
        start, end = _midnight(start_day, zone), _midnight(end_day, zone)
        # Keep compatibility with trusted callers supplying the canonical bounds,
        # but reject a contradictory clock time instead of silently discarding it.
        for key, canonical in (('starts_at', start), ('ends_at', end)):
            if key in values and _datetime(values[key], zone).astimezone(UTC) != canonical.astimezone(UTC):
                raise ValidationError('Событие без времени задаётся датами; переданное время противоречит датам.')
    else:
        if values.get('start_date') is not None or values.get('end_date') is not None:
            raise ValidationError('Для дат без времени укажите all_day=true.')
        if previous_all_day and (not values.get('starts_at') or not values.get('ends_at')):
            raise ValidationError('Для перехода к событию со временем укажите starts_at и ends_at.')
        start = _datetime(values.get('starts_at', event.starts_at if event else None), zone)
        end = _datetime(values.get('ends_at', event.ends_at if event else None), end_zone)
        start_day = end_day = None
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise ValidationError('Конец должен быть позже начала.')
    return {'starts_at': start, 'ends_at': end, 'timezone': zone_name, 'end_timezone': end_zone_name,
            'all_day': all_day, 'start_date': start_day, 'end_date': end_day, 'blocks_time': blocks_time}
