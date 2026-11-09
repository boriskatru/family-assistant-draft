"""Validated, finite task-edit arguments for both agent and legacy actions."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskChanges(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    not_before: datetime | None = Field(default=None, description='Do not schedule work before this aware datetime.')
    clear_not_before: bool = False
    assignee_id: UUID | None = None
    participants: list[UUID] | None = None
    project_id: UUID | None = None
    trip_id: UUID | None = None
    parent_id: UUID | None = None
    clear_project: bool = False
    clear_trip: bool = False
    clear_parent: bool = False
    dependencies: list[UUID] | None = None
    visibility: Literal['private', 'shared'] | None = None
    domain: Literal['personal', 'work'] | None = None
    due_at: datetime | None = Field(default=None, description='A deadline with an explicit UTC offset; it is not a scheduled work slot.')
    clear_due_at: bool = Field(default=False, description='Explicitly remove the deadline. Do not also supply due_at.')
    priority: int | None = Field(default=None, ge=1, le=5)
    estimated_minutes: int | None = Field(default=None, ge=1, le=10000)
    remaining_minutes: int | None = Field(default=None, ge=0, le=10000)
    allow_split: bool | None = None
    category: str | None = Field(default=None, max_length=64)
    status: Literal['todo', 'in_progress', 'cancelled'] | None = Field(default=None,
        description='Use cancelled for an obsolete or mistakenly created task; completion uses complete_task.')

    @field_validator('due_at', 'not_before')
    @classmethod
    def aware_due(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError('An explicit time-zone offset is required')
        return value

    @model_validator(mode='after')
    def valid_changes(self):
        if self.clear_due_at and self.due_at is not None:
            raise ValueError('Cannot set and remove the deadline together')
        for field,flag in [('not_before','clear_not_before'),('project_id','clear_project'),('trip_id','clear_trip'),('parent_id','clear_parent')]:
            if getattr(self,flag) and getattr(self,field) is not None:
                raise ValueError('Cannot set and clear one field together')
        if self.not_before and self.due_at and self.not_before > self.due_at:
            raise ValueError('not_before must not be later than due_at')
        changes = self.model_dump(exclude_none=True)
        clear = False
        for flag in ('clear_due_at','clear_not_before','clear_project','clear_trip','clear_parent'):
            clear = changes.pop(flag,False) or clear
        if not changes and not clear:
            raise ValueError('No changes supplied')
        return self


class UpdateTaskArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: UUID
    expected_version: int = Field(ge=1)
    changes: TaskChanges
