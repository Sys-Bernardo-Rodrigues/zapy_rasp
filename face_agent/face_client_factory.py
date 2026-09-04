"""
Escolhe o client certo por vendor — quem chama só conhece os métodos comuns
(enroll_face, delete_user_info, open_door, reboot_terminal), nunca ISAPI ou a
HTTP API do Intelbras diretamente. Duck typing: as duas classes implementam a
mesma interface por convenção, sem precisar de uma base abstrata pra dois casos.
"""
from .hikvision_client import HikvisionClient, HikvisionTerminal
from .intelbras_client import IntelbrasClient, IntelbrasTerminal

VENDORS = ("hikvision", "intelbras")


def create_face_client(
    vendor: str,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    https: bool = False,
    verify_tls: bool = True,
    relay_level: int = 0,
):
    vendor = vendor.strip().lower()
    if vendor == "hikvision":
        return HikvisionClient(
            HikvisionTerminal(host=host, port=port, username=username, password=password, https=https, verify_tls=verify_tls)
        )
    if vendor == "intelbras":
        return IntelbrasClient(
            IntelbrasTerminal(
                host=host, port=port, username=username, password=password,
                https=https, verify_tls=verify_tls, relay_level=relay_level,
            )
        )
    raise ValueError(f"vendor desconhecido: {vendor!r} (esperado {VENDORS!r})")
