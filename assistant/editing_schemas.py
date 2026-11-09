"""Explicit, versioned edits for records that previously only supported creation."""
from datetime import date
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EditModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ProjectChanges(EditModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    goal: str | None = Field(default=None, max_length=5000)
    starts_on: date | None = None
    ends_on: date | None = None
    clear_starts_on: bool = False
    clear_ends_on: bool = False
    status: Literal['active', 'completed', 'archived', 'cancelled'] | None = None
    visibility: Literal['private', 'shared'] | None = None
    domain: Literal['personal', 'work'] | None = None
    participants: list[UUID] | None = None


class TripChanges(EditModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    packing_notes: str | None = Field(default=None, max_length=10000)
    starts_on: date | None = None
    ends_on: date | None = None
    clear_starts_on: bool = False
    clear_ends_on: bool = False
    status: Literal['planned', 'booked', 'active', 'completed', 'archived', 'cancelled'] | None = None
    visibility: Literal['private', 'shared', 'busy_only'] | None = None
    domain: Literal['personal', 'work'] | None = None
    participants: list[UUID] | None = None
    project_id: UUID | None = None
    clear_project: bool = False


class ChecklistChanges(EditModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    source_text: str | None = Field(default=None, max_length=10000)


class ChecklistItemChanges(EditModel):
    text: str | None = Field(default=None, min_length=1, max_length=300)
    quantity: str | None = Field(default=None, max_length=50)
    assignee_id: UUID | None = None
    clear_assignee: bool = False
    position: int | None = Field(default=None, ge=0, le=10000)
    checked: bool | None = None


class NewChecklistItem(EditModel):
    text: str = Field(min_length=1, max_length=300)
    quantity: str = Field(default='', max_length=50)
    assignee_id: UUID | None = None
    position: int | None = Field(default=None, ge=0, le=10000)


class TripSegmentChanges(EditModel):
    position: int | None = Field(default=None, ge=0, le=10000)
    kind: Literal['transport', 'flight', 'train', 'car', 'bus', 'walk', 'hotel', 'activity'] | None = None
    origin: str | None = Field(default=None, max_length=500)
    destination: str | None = Field(default=None, max_length=500)
    origin_place_id: UUID | None = None
    destination_place_id: UUID | None = None
    clear_origin_place: bool = False
    clear_destination_place: bool = False
    booking_status: Literal['planned', 'booked', 'cancelled'] | None = None
    booking_reference: str | None = Field(default=None, max_length=200)
    carrier: str | None = Field(default=None, max_length=200)
    flight_number: str | None = Field(default=None, max_length=50)
    checkin_minutes: int | None = Field(default=None, ge=0, le=10000)
    baggage_minutes: int | None = Field(default=None, ge=0, le=10000)
    transfer_minutes: int | None = Field(default=None, ge=0, le=10000)
    travelers: list[str] | None = Field(default=None, max_length=20)


class UpdateProjectArgs(EditModel):
    project_id: UUID
    expected_version: int = Field(ge=1)
    changes: ProjectChanges


class UpdateTripArgs(EditModel):
    trip_id: UUID
    expected_version: int = Field(ge=1)
    changes: TripChanges
    shift_events: bool = Field(default=False, description='Explicitly move all linked events by the change in starts_on; preserve local wall-clock times.')


class UpdateChecklistArgs(EditModel):
    checklist_id: UUID
    expected_version: int = Field(ge=1)
    changes: ChecklistChanges


class AddChecklistItemArgs(EditModel):
    checklist_id: UUID
    expected_version: int = Field(ge=1, description='Current checklist version')
    data: NewChecklistItem


class UpdateChecklistItemArgs(EditModel):
    item_id: UUID
    expected_version: int = Field(ge=1, description='Current item version')
    changes: ChecklistItemChanges


class RemoveChecklistItemArgs(EditModel):
    item_id: UUID
    expected_version: int = Field(ge=1)


class UnlinkDocumentArgs(EditModel):
    document_id: UUID
    expected_version: int = Field(ge=1, description='Current document version')
    parent_type: Literal['event', 'trip', 'task']
    parent_id: UUID


class UpdateTripSegmentArgs(EditModel):
    segment_id: UUID
    expected_version: int = Field(ge=1)
    changes: TripSegmentChanges
