"""Malformed Telegram commands produce actionable replies without partial writes."""
import pytest

from assistant import bot, services
from assistant.models import Document, IncomingUpdate, Job


pytestmark = pytest.mark.django_db


def process(person, text='', caption=None, prepared=None):
    message = {
        'message_id': 1,
        'from': {'id': person.telegram_id, 'is_bot': False},
        'chat': {'id': person.telegram_id, 'type': 'private'},
        'text': text,
    }
    if caption is not None:
        message['caption'] = caption
    payload = {'update_id': 1, 'message': message}
    bot._finish_sync(payload, bot._identity(payload), person.pk, prepared)
    return payload


@pytest.mark.parametrize('command', ['/show', '/show event', '/show event id extra', '/show unknown id'])
def test_malformed_show_is_completed_once_with_usage(people, command):
    person, _ = people
    payload = process(person, text=command)
    # Redelivery does not create another error message or reprocess the update.
    bot._finish_sync(payload, bot._identity(payload), person.pk)
    reply = Job.objects.get(kind='telegram_send').payload['text']
    assert reply.startswith('/show ')
    assert 'укажите тип записи и её ID' in reply
    assert 'unpack' not in reply
    assert IncomingUpdate.objects.get(update_id=1).status == 'completed'


@pytest.mark.parametrize('caption', ['/attach', '/attach event', '/attach event id extra', '/attach project id'])
def test_malformed_attach_has_usage_and_does_not_save_document(people, caption):
    person, _ = people
    process(person, caption=caption, prepared={'upload': {
        'filename': 'ticket.pdf', 'content': b'%PDF-1.4',
        'mime_type': 'application/pdf', 'file_id': 'telegram-test-file',
    }})
    reply = Job.objects.get(kind='telegram_send').payload['text']
    assert reply.startswith('/attach event|trip|task ID')
    assert 'unpack' not in reply
    assert not Document.objects.exists()
    assert IncomingUpdate.objects.get(update_id=1).status == 'completed'


@pytest.mark.parametrize('separator', [' ', '\n', '\t'])
def test_addressed_show_accepts_whitespace_before_arguments(people, separator):
    person, _ = people
    trip = services.create_trip(person, title='Путешествие')
    process(person, text=f'/show@calendarBA_bot{separator}trip {trip.pk}')
    reply = Job.objects.get(kind='telegram_send').payload['text']
    assert trip.title in reply
    assert str(trip.pk) in reply
