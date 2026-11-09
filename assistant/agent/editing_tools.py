"""Native tools for editing existing records and the speaker's preferences."""
from agents.tool_context import ToolContext
from assistant.editing_schemas import (
    UpdateProjectArgs, UpdateTripArgs, UpdateChecklistArgs, AddChecklistItemArgs,
    UpdateChecklistItemArgs, RemoveChecklistItemArgs, UnlinkDocumentArgs, UpdateTripSegmentArgs,
)
from assistant.recurrence_actions import UpdateRecurrenceArgs, StopRecurrenceArgs
from assistant.reminder_actions import UpdateRemindersArgs
from assistant.preferences import PreferenceChanges


def build_editing_tools(function_tool, operation):
    def payload(data):
        return data.model_dump(mode='json', exclude_none=True)

    @function_tool
    async def update_project(ctx: ToolContext, data: UpdateProjectArgs) -> dict:
        """Edit, complete or archive an existing project; read its current version first."""
        return await operation(ctx,'update_project',payload(data))

    @function_tool
    async def update_trip(ctx: ToolContext, data: UpdateTripArgs) -> dict:
        """Edit trip details/dates/status. For a requested move of the whole itinerary set shift_events=true;
        changing starts_on then shifts linked events by the same days, preserving local times."""
        return await operation(ctx,'update_trip',payload(data))

    @function_tool
    async def update_trip_segment(ctx: ToolContext, data: UpdateTripSegmentArgs) -> dict:
        """Edit route endpoints, booking details or travel buffers. Read segment ID/version in its trip.
        Mark booked only on explicit booking confirmation; this does not buy tickets."""
        return await operation(ctx,'update_trip_segment',payload(data))

    @function_tool
    async def update_checklist(ctx: ToolContext, data: UpdateChecklistArgs) -> dict:
        """Edit an existing checklist's title/source notes, without replacing its items."""
        return await operation(ctx,'update_checklist',payload(data))

    @function_tool
    async def add_checklist_item(ctx: ToolContext, data: AddChecklistItemArgs) -> dict:
        """Add to an EXISTING checklist. Read its current version; do not create a duplicate list."""
        return await operation(ctx,'add_checklist_item',payload(data))

    @function_tool
    async def update_checklist_item(ctx: ToolContext, data: UpdateChecklistItemArgs) -> dict:
        """Edit an existing item, quantity, assignee, order or checked state using its current version."""
        return await operation(ctx,'update_checklist_item',payload(data))

    @function_tool
    async def remove_checklist_item(ctx: ToolContext, data: RemoveChecklistItemArgs) -> dict:
        """Remove the exact checklist item requested by the user, using its current item version."""
        return await operation(ctx,'remove_checklist_item',payload(data))

    @function_tool
    async def unlink_document(ctx: ToolContext, data: UnlinkDocumentArgs) -> dict:
        """Remove an incorrect document-to-event/task/trip link, preserving the original file and its other links."""
        return await operation(ctx,'unlink_document',payload(data))

    @function_tool
    async def update_reminders(ctx: ToolContext, data: UpdateRemindersArgs) -> dict:
        """Add/remove/replace/disable reminders for an existing event/task. Read record/version first.
        local_time=11:00 and offset_minutes=0 means 11:00 on its date, including all-day birthdays.
        Default recipient is the speaker; specify another allowed participant only when requested."""
        return await operation(ctx,'update_reminders',payload(data))

    @function_tool
    async def update_recurrence(ctx: ToolContext, data: UpdateRecurrenceArgs) -> dict:
        """Change FUTURE repeats' rule, weekday/time, duration or details, preserving past occurrences.
        expected_version belongs to the source event/task. For one occurrence use update_event/update_task."""
        return await operation(ctx,'update_recurrence',payload(data))

    @function_tool
    async def stop_recurrence(ctx: ToolContext, data: StopRecurrenceArgs) -> dict:
        """Stop an existing recurrence; by default cancel its future occurrences too.
        Use cancel_future=false to keep existing records, e.g. before converting a mistaken task to an event."""
        return await operation(ctx,'stop_recurrence',payload(data))

    @function_tool
    async def read_preferences(ctx: ToolContext) -> dict:
        """Read the current speaker's schedule settings and version before changing preferences."""
        return await operation(ctx,'read_preferences',{},mutation=False)

    @function_tool
    async def update_preferences(ctx: ToolContext, changes: PreferenceChanges, expected_version: int) -> dict:
        """Change the speaker's morning plan time, quiet hours, work/sleep hours, timezone or planning limits.
        No slash command is needed. Read preferences/version first; weekdays are Monday=0 to Sunday=6."""
        return await operation(ctx,'update_preferences',dict(changes=payload(changes),expected_version=expected_version))

    return [update_project,update_trip,update_trip_segment,update_checklist,add_checklist_item,
            update_checklist_item,remove_checklist_item,unlink_document,update_reminders,
            update_recurrence,stop_recurrence,read_preferences,update_preferences]
