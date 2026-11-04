import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Base(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    class Meta:
        abstract = True


class Person(Base):
    telegram_id = models.BigIntegerField(unique=True)
    chat_id = models.BigIntegerField(null=True, blank=True)
    name = models.CharField(max_length=100)
    timezone = models.CharField(max_length=64, default='Europe/Moscow')
    morning_time = models.TimeField(default='08:00')
    quiet_start = models.TimeField(default='23:00')
    quiet_end = models.TimeField(default='07:00')
    profile = models.JSONField(default=dict, blank=True)
    def __str__(self):
        return self.name


class Owned(Base):
    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='+')
    visibility = models.CharField(max_length=12, default='private', choices=[(v,v) for v in ['private','shared','busy_only']])
    domain = models.CharField(max_length=12, default='personal', choices=[('personal','personal'),('work','work')])
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    editors = models.ManyToManyField(Person, blank=True, related_name='%(class)s_editable')
    class Meta:
        abstract = True


class Project(Owned):
    goal = models.TextField(blank=True)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default='active')
    participants = models.ManyToManyField(Person, blank=True, related_name='projects')


class Trip(Owned):
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, default='planned')
    packing_notes = models.TextField(blank=True)
    participants = models.ManyToManyField(Person, blank=True, related_name='trips')


class RecurrenceRule(Owned):
    kind = models.CharField(max_length=10, default='event')
    mode = models.CharField(max_length=20, default='calendar')
    dtstart = models.DateTimeField()
    timezone = models.CharField(max_length=64, default='Europe/Moscow')
    rrule = models.TextField(blank=True)
    cycle = models.JSONField(default=list, blank=True)
    interval = models.PositiveIntegerField(default=1)
    unit = models.CharField(max_length=10, default='month')
    until = models.DateTimeField(null=True, blank=True)
    template = models.JSONField(default=dict)
    active = models.BooleanField(default=True)


class Task(Owned):
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    trip = models.ForeignKey(Trip, null=True, blank=True, on_delete=models.SET_NULL)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='subtasks')
    assignee = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='tasks')
    participants = models.ManyToManyField(Person, blank=True, related_name='joint_tasks')
    dependencies = models.ManyToManyField('self', symmetrical=False, blank=True, related_name='dependents')
    status = models.CharField(max_length=20, default='todo')
    priority = models.PositiveSmallIntegerField(default=2)
    due_at = models.DateTimeField(null=True, blank=True)
    estimated_minutes = models.PositiveIntegerField(default=60)
    remaining_minutes = models.PositiveIntegerField(default=60)
    allow_split = models.BooleanField(default=False)
    category = models.CharField(max_length=64, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    recurrence = models.ForeignKey(RecurrenceRule, null=True, blank=True, on_delete=models.SET_NULL)
    occurrence_key = models.CharField(max_length=100, null=True, blank=True)
    previous_task = models.OneToOneField('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='successor')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['recurrence','occurrence_key'], name='unique_task_occurrence')]
        indexes=[models.Index(fields=['assignee','status','due_at'])]


class Event(Owned):
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL)
    trip = models.ForeignKey(Trip, null=True, blank=True, on_delete=models.SET_NULL)
    participants = models.ManyToManyField(Person, blank=True, related_name='events')
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField()
    timezone = models.CharField(max_length=64, default='Europe/Moscow')
    end_timezone = models.CharField(max_length=64, default='Europe/Moscow')
    all_day = models.BooleanField(default=False)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=500, blank=True)
    kind = models.CharField(max_length=30, default='meeting')
    status = models.CharField(max_length=20, default='confirmed')
    flexible = models.BooleanField(default=False)
    buffer_before = models.PositiveIntegerField(default=0)
    buffer_after = models.PositiveIntegerField(default=0)
    meeting_url = models.URLField(max_length=2000, blank=True)
    packing_notes = models.TextField(blank=True)
    recurrence = models.ForeignKey(RecurrenceRule, null=True, blank=True, on_delete=models.SET_NULL)
    occurrence_key = models.CharField(max_length=100, null=True, blank=True)
    sync_status = models.CharField(max_length=20, default='local')
    class Meta:
        constraints=[models.CheckConstraint(condition=Q(ends_at__gt=models.F('starts_at')), name='positive_event_duration'), models.UniqueConstraint(fields=['recurrence','occurrence_key'], name='unique_event_occurrence')]


class OccurrenceOverride(Base):
    rule = models.ForeignKey(RecurrenceRule, on_delete=models.CASCADE)
    occurrence_key = models.CharField(max_length=100)
    action = models.CharField(max_length=10, default='modify')
    changes = models.JSONField(default=dict)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['rule','occurrence_key'],name='unique_override')]


class TripSegment(Base):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name='segments')
    event = models.OneToOneField(Event, on_delete=models.CASCADE, related_name='segment')
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=20, default='transport')
    origin = models.CharField(max_length=500, blank=True)
    destination = models.CharField(max_length=500, blank=True)
    booking_status = models.CharField(max_length=20, default='planned')
    booking_reference = models.CharField(max_length=200, blank=True)
    carrier = models.CharField(max_length=200, blank=True)
    flight_number = models.CharField(max_length=50, blank=True)
    checkin_minutes = models.PositiveIntegerField(default=0)
    baggage_minutes = models.PositiveIntegerField(default=0)
    transfer_minutes = models.PositiveIntegerField(default=0)
    travelers = models.JSONField(default=list, blank=True)
    class Meta:
        ordering=['position','event__starts_at']


class Document(Owned):
    file = models.FileField(upload_to='documents/%Y/%m/')
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    document_type = models.CharField(max_length=30, default='other')
    extracted_text = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)
    telegram_file_id = models.TextField(blank=True)
    events = models.ManyToManyField(Event, blank=True, related_name='documents')
    trips = models.ManyToManyField(Trip, blank=True, related_name='documents')
    tasks = models.ManyToManyField(Task, blank=True, related_name='documents')

    @property
    def telegram_caption(self):
        value = self.metadata.get('telegram_caption') if isinstance(self.metadata, dict) else None
        return value if isinstance(value, str) else ''


class Checklist(Base):
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.CASCADE, related_name='checklists')
    trip = models.ForeignKey(Trip, null=True, blank=True, on_delete=models.CASCADE, related_name='checklists')
    title = models.CharField(max_length=200, default='Что взять')
    source_text = models.TextField(blank=True)
    class Meta:
        constraints=[models.CheckConstraint(condition=(Q(event__isnull=False,trip__isnull=True)|Q(event__isnull=True,trip__isnull=False)),name='checklist_single_parent')]
    @property
    def parent(self):
        return self.event if self.event_id else self.trip


class ChecklistItem(Base):
    checklist = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name='items')
    text = models.CharField(max_length=300)
    quantity = models.CharField(max_length=50, blank=True)
    assignee = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)
    position = models.PositiveIntegerField(default=0)
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    class Meta:
        ordering=['position','created_at']


class Reminder(Base):
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.CASCADE, related_name='reminders')
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.CASCADE, related_name='reminders')
    recipient = models.ForeignKey(Person, on_delete=models.CASCADE)
    due_at = models.DateTimeField(db_index=True)
    offset_minutes = models.PositiveIntegerField(default=15)
    object_version = models.PositiveIntegerField(default=1)
    kind = models.CharField(max_length=20, default='reminder')
    status = models.CharField(max_length=20, default='pending')
    class Meta:
        constraints=[models.CheckConstraint(condition=(Q(event__isnull=False,task__isnull=True)|Q(event__isnull=True,task__isnull=False)),name='reminder_single_parent')]


class Job(Base):
    kind = models.CharField(max_length=50)
    queue = models.CharField(max_length=20, default='service')
    payload = models.JSONField(default=dict)
    run_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=20, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    lock_token = models.UUIDField(null=True, blank=True)
    dedup_key = models.CharField(max_length=250, unique=True)
    error = models.CharField(max_length=500, blank=True)
    class Meta:
        indexes=[models.Index(fields=['queue','status','run_at'])]


class Delivery(Base):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    recipient = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)
    chat_id = models.BigIntegerField()
    message_id = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, default='pending')
    sent_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['job','chat_id'],name='unique_job_delivery')]


class IncomingUpdate(Base):
    update_id = models.BigIntegerField(unique=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default='pending')


class Conversation(Base):
    person = models.ForeignKey(Person,on_delete=models.CASCADE)
    chat_id = models.BigIntegerField()
    messages = models.JSONField(default=list)
    pending = models.JSONField(default=dict)
    expires_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['person','chat_id'],name='unique_conversation')]


class ActionRequest(Base):
    initiator = models.ForeignKey(Person,on_delete=models.PROTECT,related_name='+')
    approver = models.ForeignKey(Person,on_delete=models.PROTECT,related_name='+')
    kind = models.CharField(max_length=30)
    target_type = models.CharField(max_length=30)
    target_id = models.UUIDField()
    target_version = models.PositiveIntegerField()
    changes = models.JSONField(default=dict)
    status = models.CharField(max_length=20,default='proposed')
    expires_at = models.DateTimeField()


class AuditLog(Base):
    actor = models.ForeignKey(Person,null=True,on_delete=models.SET_NULL)
    action = models.CharField(max_length=40)
    target_type = models.CharField(max_length=40)
    target_id = models.UUIDField()
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)


class DailyPlan(Base):
    person = models.ForeignKey(Person,on_delete=models.CASCADE)
    day = models.DateField()
    items = models.JSONField(default=list)
    status = models.CharField(max_length=20, default='draft')
    class Meta:
        constraints=[models.UniqueConstraint(fields=['person','day'],name='unique_daily_plan')]


class CalendarConnection(Base):
    person = models.OneToOneField(Person,on_delete=models.CASCADE)
    encrypted_tokens = models.TextField()
    enabled = models.BooleanField(default=True)


class CalendarSource(Base):
    connection = models.ForeignKey(CalendarConnection,on_delete=models.CASCADE,related_name='sources')
    calendar_id = models.CharField(max_length=500)
    role = models.CharField(max_length=20,default='personal')
    writable = models.BooleanField(default=False)
    sync_token = models.TextField(blank=True)
    last_sync = models.DateTimeField(null=True,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['connection','calendar_id'],name='unique_calendar_source')]


class CalendarBinding(Base):
    source = models.ForeignKey(CalendarSource,on_delete=models.CASCADE)
    event = models.ForeignKey(Event,on_delete=models.CASCADE,related_name='calendar_bindings')
    external_id = models.CharField(max_length=500)
    etag = models.TextField(blank=True)
    last_version = models.PositiveIntegerField(default=0)
    snapshot = models.JSONField(default=dict)
    conflict = models.JSONField(default=dict)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['source','external_id'],name='unique_external_event'),models.UniqueConstraint(fields=['source','event'],name='unique_event_binding')]


class OAuthState(Base):
    person = models.ForeignKey(Person,on_delete=models.CASCADE)
    state_hash = models.CharField(max_length=64,unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True,blank=True)


class AgentRun(Base):
    person = models.ForeignKey(Person,on_delete=models.CASCADE,related_name='agent_runs')
    chat_id = models.BigIntegerField()
    update_id = models.BigIntegerField(unique=True)
    input_text = models.TextField()
    status = models.CharField(max_length=20,default='running',db_index=True)
    checkpoint = models.JSONField(default=list,blank=True)
    final_reply = models.JSONField(default=dict,blank=True)
    permission_refs = models.JSONField(default=list,blank=True)
    sources = models.JSONField(default=list,blank=True)
    usage = models.JSONField(default=dict,blank=True)
    tool_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True,blank=True)
    class Meta:
        indexes=[models.Index(fields=['person','chat_id','-started_at'])]


class AgentToolCall(Base):
    run = models.ForeignKey(AgentRun,on_delete=models.CASCADE,related_name='tool_calls')
    name = models.CharField(max_length=80)
    arguments = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=20,default='completed')
    error = models.CharField(max_length=100,blank=True)
    mutation = models.BooleanField(default=False)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['run','fingerprint'],name='unique_agent_tool_receipt')]
