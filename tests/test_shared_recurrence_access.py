from datetime import datetime, timedelta, timezone

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from assistant import services
from assistant.models import Event, RecurrenceRule
from assistant.recurrence import create_rule_from_event, materialize_recurrences


pytestmark = pytest.mark.django_db
START = datetime(2030, 1, 1, 9, tzinfo=timezone.utc)


@pytest.mark.parametrize('owner_index', [0, 1])
def test_spouse_can_make_existing_shared_event_recurring_without_changing_owner(people, owner_index):
    owner, spouse = people[owner_index], people[1 - owner_index]
    original = services.create_event(
        owner, title='Совместный созвон', starts_at=START,
        ends_at=START + timedelta(hours=1), visibility='shared',
        participants=[owner.pk, spouse.pk], remind_minutes=[60],
    )
    assert not original.editors.exists()
    checklist = services.make_checklist(owner, 'event', original.pk, 'Паспорт каждому; Камера')
    services.set_checklist_item(owner, checklist.items.get(text='Камера').pk, True)

    rule = create_rule_from_event(spouse, original, rrule='FREQ=WEEKLY;COUNT=3')
    original.refresh_from_db()
    assert rule.owner_id == original.owner_id == owner.pk
    assert original.recurrence_id == rule.pk
    assert materialize_recurrences(START) == 2
    assert materialize_recurrences(START) == 0

    for occurrence in Event.objects.filter(recurrence=rule):
        assert occurrence.owner_id == owner.pk
        assert occurrence.visibility == 'shared'
        assert set(occurrence.participants.all()) == {owner, spouse}
        assert services.can_edit(occurrence, owner)
        assert services.can_edit(occurrence, spouse)
        assert set(occurrence.reminders.values_list('recipient_id', 'offset_minutes', 'status')) == {
            (owner.pk, 60, 'pending'), (spouse.pk, 60, 'pending'),
        }
        assert occurrence.checklists.get().items.count() == 3
        if occurrence.pk != original.pk:
            assert not occurrence.checklists.get().items.filter(checked_at__isnull=False).exists()
            assert set(occurrence.checklists.get().items.filter(text='Паспорт').values_list('assignee_id', flat=True)) == {
                owner.pk, spouse.pk,
            }
    assert checklist.items.get(text='Камера').checked_at is not None


@pytest.mark.parametrize('visibility', ['private', 'busy_only'])
@pytest.mark.parametrize('owner_index', [0, 1])
def test_spouse_cannot_make_private_or_busy_event_recurring(people, owner_index, visibility):
    owner, spouse = people[owner_index], people[1 - owner_index]
    original = services.create_event(
        owner, title='Личное событие', starts_at=START,
        ends_at=START + timedelta(hours=1), visibility=visibility,
    )
    with pytest.raises(PermissionDenied):
        create_rule_from_event(spouse, original, rrule='FREQ=WEEKLY;COUNT=3')
    original.refresh_from_db()
    assert original.recurrence_id is None
    assert not RecurrenceRule.objects.exists()


@pytest.mark.parametrize('attendee', ['owner', 'spouse'])
def test_nonowner_must_include_both_spouses_when_configuring_shared_series(people, attendee):
    owner, spouse = people
    original = services.create_event(
        owner, title='Общее событие', starts_at=START,
        ends_at=START + timedelta(hours=1), visibility='shared',
    )
    original.participants.set([owner if attendee == 'owner' else spouse])
    with pytest.raises(ValidationError, match='с участием обоих'):
        create_rule_from_event(spouse, original, rrule='FREQ=WEEKLY;COUNT=3')
    original.refresh_from_db()
    assert original.recurrence_id is None
    assert not RecurrenceRule.objects.exists()
