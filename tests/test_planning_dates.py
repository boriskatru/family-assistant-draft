"""Date-only calendar notes and future backlog do not exhaust today's plan."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.agent.retrieval import available_slots
from assistant.models import Event, Task
from assistant.planning import accept_plan, build_daily_plan, daily_plan_message, render_daily_plan


pytestmark = pytest.mark.django_db
DAY = date(2030, 1, 7)


def moment(hour=0, day=DAY, zone='UTC'):
    return datetime.combine(day, datetime.min.time(), ZoneInfo(zone)).replace(hour=hour)


@pytest.fixture
def users(people, monkeypatch):
    from assistant import planning
    monkeypatch.setattr(planning.timezone, 'now', lambda: moment(day=DAY-timedelta(days=1)))
    for person in people:
        person.timezone = 'UTC'
        person.save()
    return people


def event(owner, start=None, end=None, **kwargs):
    obj = Event.objects.create(owner=owner, title=kwargs.pop('title', 'Событие'),
        starts_at=start or moment(), ends_at=end or moment(day=DAY+timedelta(days=1)),
        timezone=owner.timezone, **kwargs)
    obj.participants.add(owner)
    return obj


def task(owner, title='Задача', **kwargs):
    obj = Task.objects.create(owner=owner, assignee=owner, title=title, **kwargs)
    obj.participants.add(owner)
    return obj


def proposed(plan):
    return [item for item in plan.items if item['kind'] == 'task']


def test_informational_birthday_remains_visible_without_busy_time_load_or_conflict(users):
    owner, _ = users
    birthday = event(owner, title='День рождения Коли', all_day=True, blocks_time=False,
        start_date=DAY, end_date=DAY+timedelta(days=1))
    owner.profile = {'max_daily_minutes': 60}
    owner.save()
    task(owner)
    plan = build_daily_plan(owner, DAY)
    assert len(proposed(plan)) == 1
    assert not [item for item in plan.items if item['kind'] in {'problem', 'unscheduled'}]
    result = daily_plan_message(owner, DAY)
    assert 'Без времени День рождения Коли' in result['text']
    assert '00:00' not in result['text']
    assert {'kind': 'event', 'id': str(birthday.pk)} in result['permission_refs']
    windows = available_slots(owner, DAY)['windows']
    assert datetime.fromisoformat(windows[0]['starts_at']) == moment(7)
    created = accept_plan(owner, DAY)
    assert len(created) == 1 and created[0].starts_at == moment(7)


def test_nonblocking_timed_entry_does_not_conflict_or_hide_a_real_busy_meeting(users):
    owner, _ = users
    event(owner, moment(7), moment(23), blocks_time=False, title='Справочная запись')
    event(owner, moment(7), moment(8), title='Созвон')
    task(owner)
    plan = build_daily_plan(owner, DAY)
    assert not [item for item in plan.items if item['kind'] == 'problem']
    assert datetime.fromisoformat(proposed(plan)[0]['starts_at']) == moment(8)
    assert datetime.fromisoformat(available_slots(owner, DAY)['windows'][0]['starts_at']) == moment(8)


def test_explicit_blocking_all_day_entry_still_reserves_the_day(users):
    owner, _ = users
    event(owner, all_day=True, blocks_time=True, start_date=DAY, end_date=DAY+timedelta(days=1), title='Отпуск')
    task(owner)
    plan = build_daily_plan(owner, DAY)
    assert proposed(plan) == []
    assert available_slots(owner, DAY)['windows'] == []
    assert 'Весь день Отпуск' in render_daily_plan(owner, DAY)


def test_date_only_event_uses_calendar_date_in_each_participants_timezone(users):
    owner, spouse = users
    owner.timezone = 'Europe/Moscow'
    owner.save()
    spouse.timezone = 'America/Los_Angeles'
    spouse.save()
    birthday = event(owner, moment(zone=owner.timezone), moment(day=DAY+timedelta(days=1), zone=owner.timezone),
        all_day=True, blocks_time=False, start_date=DAY, end_date=DAY+timedelta(days=1),
        title='День рождения', visibility='shared')
    birthday.participants.add(spouse)
    assert 'День рождения' not in render_daily_plan(spouse, DAY-timedelta(days=1))
    assert 'Без времени День рождения' in render_daily_plan(spouse, DAY)
    assert 'День рождения' not in render_daily_plan(spouse, DAY+timedelta(days=1))
    birthday.blocks_time = True
    birthday.save()
    assert available_slots(spouse, DAY)['windows'] == []


def test_future_and_undated_backlog_is_deferred_without_false_urgent_warning(users):
    owner, _ = users
    event(owner, moment(7), moment(23))
    future = task(owner, 'Подготовить подарки', due_at=moment(12, day=DAY+timedelta(days=30)))
    undated = task(owner, 'Перевезти вещи')
    result = daily_plan_message(owner, DAY)
    assert 'Требует внимания' not in result['text']
    assert 'Недостаточно свободного времени' not in result['text']
    assert 'На другие дни осталось задач: 2' in result['text']
    assert '/tasks' in result['text']
    pairs = {(ref['kind'], ref['id']) for ref in result['permission_refs']}
    assert {('task', str(future.pk)), ('task', str(undated.pk))} <= pairs
    plan = build_daily_plan(owner, DAY)
    assert {item['task_id'] for item in plan.items if item['kind'] == 'deferred'} == {str(future.pk), str(undated.pk)}


def test_due_today_and_overdue_tasks_keep_actionable_warning(users):
    owner, _ = users
    event(owner, moment(7), moment(23))
    today = task(owner, 'Отправить документы', due_at=moment(12))
    overdue = task(owner, 'Оплатить заказ', due_at=moment(12, day=DAY-timedelta(days=2)))
    task(owner, 'На следующую неделю', due_at=moment(12, day=DAY+timedelta(days=7)))
    result = daily_plan_message(owner, DAY)
    assert 'Требует внимания:' in result['text']
    assert today.title in result['text'] and overdue.title in result['text']
    assert 'Дедлайн уже прошёл' in result['text']
    assert 'На следующую неделю' not in result['text']


def test_future_tasks_can_still_use_spare_time_before_their_deadline(users):
    owner, _ = users
    future = task(owner, due_at=moment(12, day=DAY+timedelta(days=30)))
    plan = build_daily_plan(owner, DAY)
    assert [item['task_id'] for item in proposed(plan)] == [str(future.pk)]


def test_partial_future_task_and_blocked_dependency_use_neutral_deferral(users):
    owner, _ = users
    owner.profile = {'max_daily_minutes': 30}
    owner.save()
    first = task(owner, 'Большая задача', estimated_minutes=120, remaining_minutes=120,
        allow_split=True, due_at=moment(12, day=DAY+timedelta(days=30)))
    later = task(owner, 'После неё')
    later.dependencies.add(first)
    plan = build_daily_plan(owner, DAY)
    assert len(proposed(plan)) == 1
    assert {item['task_id'] for item in plan.items if item['kind'] == 'deferred'} == {str(first.pk), str(later.pk)}
    assert not [item for item in plan.items if item['kind'] == 'unscheduled']


def test_multiday_timed_event_includes_both_dates_instead_of_ambiguous_times(users):
    owner, _ = users
    event(owner, moment(16), moment(14, day=DAY+timedelta(days=2)), title='Поездка в Клён')
    assert '07.01.2030 16:00–09.01.2030 14:00 Поездка в Клён' in render_daily_plan(owner, DAY)
    assert '07.01.2030 16:00–09.01.2030 14:00 Поездка в Клён' in render_daily_plan(owner, DAY+timedelta(days=1))


def test_multiday_date_only_event_uses_inclusive_visible_end_date(users):
    owner, _ = users
    event(owner, end=moment(day=DAY+timedelta(days=3)), title='Выставка', all_day=True,
        blocks_time=False, start_date=DAY, end_date=DAY+timedelta(days=3))
    assert 'Без времени (07.01.2030–09.01.2030) Выставка' in render_daily_plan(owner, DAY)


def test_date_only_private_details_are_not_leaked_through_joint_plan(users):
    owner, spouse = users
    birthday = event(owner, title='Личное название', location='Личный адрес', all_day=True,
        blocks_time=True, start_date=DAY, end_date=DAY+timedelta(days=1), visibility='busy_only')
    birthday.participants.add(spouse)
    text = render_daily_plan(spouse, DAY)
    assert 'Занят' in text and 'Личное' not in text and 'Личный' not in text
