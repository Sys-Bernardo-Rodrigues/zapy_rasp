"""Fase 1 do plano de integração facial: zapy fala com terminais Hikvision e
Intelbras (enroll/revoke de rosto, abertura remota de porta, reboot)."""
from .errors import FaceProvisioningError
from .face_client_factory import create_face_client

__all__ = ["FaceProvisioningError", "create_face_client"]
