"""
Escolhe o client certo por vendor — quem chama só conhece os métodos comuns
(enroll_face, delete_user_info, open_door, reboot_terminal), nunca ISAPI ou a
HTTP API do Intelbras diretamente. Duck typing: as duas classes implementam a
mesma interface por convenção, sem precisar de uma base abstrata pra dois casos.
"""
from .controlid_client import ControlIdClient, ControlIdTerminal
from .hikvision_client import HikvisionClient, HikvisionTerminal
from .intelbras_biot_client import IntelbrasBioTClient, IntelbrasBioTTerminal
from .intelbras_client import IntelbrasClient, IntelbrasTerminal

# "intelbras" = HTTP API nativa do XPE (intelbras_client.py). "intelbras_biot" = API
# cgi-bin/Digest da linha Bio-T/SS (intelbras_biot_client.py) — protocolo diferente,
# mesmo fabricante, por isso vendor separado em vez de um parâmetro "modelo".
VENDORS = ("hikvision", "intelbras", "intelbras_biot", "controlid")


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
    group_id: int | None = None,
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
    if vendor == "intelbras_biot":
        return IntelbrasBioTClient(
            IntelbrasBioTTerminal(host=host, port=port, username=username, password=password, https=https, verify_tls=verify_tls)
        )
    if vendor == "controlid":
        return ControlIdClient(
            ControlIdTerminal(
                host=host, port=port, username=username, password=password,
                https=https, verify_tls=verify_tls, group_id=group_id,
            )
        )
    raise ValueError(f"vendor desconhecido: {vendor!r} (esperado {VENDORS!r})")
