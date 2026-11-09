"""Typed access to the speaker's settings for ordinary language requests."""
from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Person


class PreferenceChanges(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    timezone: str | None = None
    morning: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    quiet_start: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    quiet_end: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    work_start: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    work_end: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    sleep_start: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    sleep_end: str | None = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    max_daily_minutes: int | None = Field(default=None, ge=0, le=1440)
    min_block_minutes: int | None = Field(default=None, ge=5, le=1440)
    break_minutes: int | None = Field(default=None, ge=0, le=1440)
    work_days: list[int] | None = Field(default=None, min_length=1, max_length=7)
    transport_mode: Literal['walking','driving','transit','bicycling'] | None = None
    home: str | None = Field(default=None, max_length=500)
    work: str | None = Field(default=None, max_length=500)

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        if value is not None:
            try: ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError): raise ValueError('Unknown timezone') from None
        return value

    @field_validator('morning','quiet_start','quiet_end','work_start','work_end','sleep_start','sleep_end')
    @classmethod
    def clock(cls, value):
        if value is not None: time.fromisoformat(value)
        return value

    @field_validator('work_days')
    @classmethod
    def days(cls, value):
        if value is not None and any(v < 0 or v > 6 for v in value): raise ValueError('Weekdays are 0 to 6')
        return sorted(set(value)) if value is not None else None

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_dump(exclude_none=True): raise ValueError('No preferences supplied')
        return self


def read_preferences(user, shared_context=False):
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS: raise PermissionDenied('Нет доступа.')
    user.refresh_from_db()
    profile = {key: value for key,value in user.profile.items() if key in PreferenceChanges.model_fields or key == 'locations'}
    return {'version':user.version, 'timezone':user.timezone, 'morning':user.morning_time.strftime('%H:%M'),
            'quiet_start':user.quiet_start.strftime('%H:%M'), 'quiet_end':user.quiet_end.strftime('%H:%M'),
            'profile':profile}


@transaction.atomic
def update_preferences(user, changes, expected_version, shared_context=False):
    if user.telegram_id not in settings.ALLOWED_TELEGRAM_IDS: raise PermissionDenied('Нет доступа.')
    person=Person.objects.select_for_update().get(pk=user.pk)
    if person.version != expected_version: raise ValidationError('Настройки изменились; прочитайте их заново.')
    values=PreferenceChanges.model_validate(changes).model_dump(exclude_none=True)
    profile=dict(person.profile)
    for key,value in values.items():
        if key == 'timezone': person.timezone=value
        elif key in ('morning','quiet_start','quiet_end'):
            setattr(person,'morning_time' if key=='morning' else key,time.fromisoformat(value))
        elif key in ('home','work'):
            profile['locations']={**profile.get('locations',{}),key:value}
        else: profile[key]=value
    person.profile=profile; person.version+=1; person.full_clean(); person.save()
    from .services import _audit
    _audit(person,person,'update_preferences')
    user.refresh_from_db()
    return {'text':'Ваши настройки обновлены.', 'version':person.version, 'changed_fields':list(values)}
