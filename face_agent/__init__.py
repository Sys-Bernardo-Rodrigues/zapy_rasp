"""Fase 1 do plano de integração facial: zapy fala com terminais Hikvision e
Intelbras (enroll/revoke de rosto, abertura remota de porta, reboot), guarda
roster local com agenda de horário, e lê eventos de reconhecimento (poll)."""
from .errors import FaceProvisioningError
from .events_poller import EventsPoller, fetch_hikvision_events_since, fetch_intelbras_events_since
from .face_client_factory import create_face_client
from .local_store import LocalStore, PollCursor, RosterEntry
from .schedule_enforcer import enforce_schedule, is_within_schedule

__all__ = [
    "FaceProvisioningError",
    "create_face_client",
    "LocalStore",
    "PollCursor",
    "RosterEntry",
    "is_within_schedule",
    "enforce_schedule",
    "EventsPoller",
    "fetch_hikvision_events_since",
    "fetch_intelbras_events_since",
]
