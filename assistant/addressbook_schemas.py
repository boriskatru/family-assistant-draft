"""Bounded create/patch schemas shared by agent tools and the address book."""
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BookArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)
    id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    visibility: Literal['private', 'shared'] | None = None
    domain: Literal['personal', 'work'] | None = None
    aliases: list[str] | None = Field(default=None, max_length=30)
    project_ids: list[UUID] | None = Field(default=None, max_length=30)

    @field_validator('aliases')
    @classmethod
    def clean_aliases(cls, values):
        if values is None:
            return None
        result, seen = [], set()
        for value in values:
            value = ' '.join(value.split())
            if not value or len(value) > 100:
                raise ValueError('Название места/контакта: от 1 до 100 символов.')
            key = value.casefold().replace('ё', 'е')
            if key not in seen:
                result.append(value)
                seen.add(key)
        return result

    @model_validator(mode='after')
    def valid_target(self):
        if self.id is None and self.title is None:
            raise ValueError('Для новой записи укажите название.')
        if self.id is not None and self.expected_version is None:
            raise ValueError('Для изменения нужна актуальная expected_version из карточки.')
        return self


class ContactArgs(BookArgs):
    kind: Literal['person', 'organization'] | None = None
    phone: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    relationship: str | None = Field(default=None, max_length=300)


class PlaceArgs(BookArgs):
    category: str | None = Field(default=None, min_length=1, max_length=64)
    address: str | None = Field(default=None, max_length=500)
    address_confirmed: bool | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    clear_coordinates: bool = False
    map_url: str | None = Field(default=None, max_length=2000)
    access_notes: str | None = Field(default=None, max_length=4000)
    parking_notes: str | None = Field(default=None, max_length=4000)
    contact_ids: list[UUID] | None = Field(default=None, max_length=30)

    @field_validator('map_url')
    @classmethod
    def safe_map_url(cls, value):
        if not value:
            return value
        try:
            parts = urlsplit(value)
            valid = parts.scheme in ('http', 'https') and parts.hostname and not parts.username and not parts.password
            parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Нужна HTTP(S)-ссылка на карту без логина и пароля.')
        return value

    @model_validator(mode='after')
    def valid_coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Широта и долгота указываются вместе.')
        if self.clear_coordinates and self.latitude is not None:
            raise ValueError('Нельзя одновременно удалить и задать координаты.')
        return self
