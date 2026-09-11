"""
Cliente cgi-bin (Digest Auth) para a linha Bio-T da Intelbras (SS 5520, SS 5530
MF FACE, SS 7520/7530 FACE, SS 3530 MF FACE etc.) — API completamente diferente
da HTTP API nativa do XPE (ver intelbras_client.py): endpoints /cgi-bin/*.cgi,
Digest auth (RFC 2617), sem envelope target/action, resposta em texto puro
("OK" em sucesso) ou um código de erro tipo "accessControlErrorUserAlreadyExist"
embutido no corpo.

Portado da documentação oficial (https://integracao.intelbras.com.br/linha-de-faciais),
ainda não validado contra hardware real — diferente do intelbras_client.py (XPE) e
hikvision_client.py, que já rodaram ao vivo em produção. Primeira integração real
deve confirmar o envelope exato de erro (a doc só documenta os códigos, não o JSON
que os envolve).
"""
import base64
import logging
import re
from dataclasses import dataclass

import requests
from requests.auth import HTTPDigestAuth

from .errors import FaceProvisioningError

logger = logging.getLogger(__name__)

MAX_JPEG_BYTES = 100 * 1024  # limite documentado da linha Bio-T (XPE aceita 200KB)
REQUEST_TIMEOUT_SECONDS = 10

# Validade "pra sempre" e zona de tempo liberada — mesmo raciocínio do Hikvision/XPE:
# controle de horário é 100% do agente, o terminal só sabe "liberado" ou "removido".
UNRESTRICTED_VALID_FROM = "2020-01-01 00:00:00"
UNRESTRICTED_VALID_TO = "2037-12-31 23:59:59"
UNRESTRICTED_TIME_SECTION = 255

# Prefixos que identificam um código de erro no corpo da resposta (ver "Parâmetros
# Retornados em Requisições" na doc) — usados pra distinguir sucesso de erro, já que
# a API não usa um envelope JSON fixo tipo {"retcode": ...} como o XPE.
_ERROR_CODE_PREFIXES = ("accessControlError", "faceInfoManagerError", "businessCommonError")


@dataclass
class IntelbrasBioTTerminal:
    host: str
    port: int
    username: str
    password: str
    https: bool = False
    verify_tls: bool = True  # False só é seguro em rede local confiável
    # Canal da porta/relê usado em accessControl.cgi (openDoor). Numeração começa em 1
    # aqui, mas o campo "Doors" do cadastro de usuário usa índice 0-based — inconsistência
    # da própria doc oficial, não um erro de digitação.
    channel: int = 1


class IntelbrasBioTClient:
    def __init__(self, terminal: IntelbrasBioTTerminal):
        self.terminal = terminal

    def _base_url(self) -> str:
        proto = "https" if self.terminal.https else "http"
        return f"{proto}://{self.terminal.host}:{self.terminal.port}"

    def _auth(self) -> HTTPDigestAuth:
        return HTTPDigestAuth(self.terminal.username, self.terminal.password)

    def _get(self, cgi: str, params: dict) -> requests.Response:
        url = f"{self._base_url()}/cgi-bin/{cgi}"
        try:
            return requests.get(
                url, params=params, auth=self._auth(),
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            raise FaceProvisioningError(f"falha de rede ao chamar {self.terminal.host}: {e}") from e

    def _post(self, cgi: str, action: str, json_body: dict) -> requests.Response:
        url = f"{self._base_url()}/cgi-bin/{cgi}"
        try:
            return requests.post(
                url, params={"action": action}, json=json_body, auth=self._auth(),
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            raise FaceProvisioningError(f"falha de rede ao chamar {self.terminal.host}: {e}") from e

    def check_health(self) -> dict:
        """GET magicBox.cgi?action=getSoftwareVersion — leve e sem efeito colateral, só
        pra status online/offline no painel (mesmo papel do check_health dos outros dois
        clients)."""
        res = self._get("magicBox.cgi", {"action": "getSoftwareVersion"})
        if not _ok(res):
            raise FaceProvisioningError(f"getSoftwareVersion falhou em {self.terminal.host}: {res.text.strip()!r}")
        return _parse_kv(res.text)

    # --- enrollment ---

    def _user_body(self, employee_no: str, name: str) -> dict:
        return {
            "UserID": employee_no, "UserName": name, "UserType": 0,
            "Doors": [0], "TimeSections": [UNRESTRICTED_TIME_SECTION],
            "ValidFrom": UNRESTRICTED_VALID_FROM, "ValidTo": UNRESTRICTED_VALID_TO,
        }

    def _ensure_user(self, employee_no: str, name: str) -> str:
        """Cria o usuário se ainda não existir (ou atualiza os campos base se já existir)
        — pré-condição documentada oficialmente tanto pra face quanto pra cartão: o
        usuário precisa existir antes de vincular qualquer credencial. Retorna 'created'
        ou 'updated'."""
        insert_res = self._post("AccessUser.cgi", "insertMulti", {"UserList": [self._user_body(employee_no, name)]})
        if _ok(insert_res):
            return "created"
        if "accessControlErrorUserAlreadyExist" in insert_res.text:
            update_res = self._post("AccessUser.cgi", "updateMulti", {"UserList": [self._user_body(employee_no, name)]})
            if not _ok(update_res):
                raise FaceProvisioningError(f"falha ao atualizar usuário {employee_no} em {self.terminal.host}: {update_res.text.strip()!r}")
            return "updated"
        raise FaceProvisioningError(f"falha ao cadastrar usuário {employee_no} em {self.terminal.host}: {insert_res.text.strip()!r}")

    def enroll_face(self, employee_no: str, name: str, jpeg: bytes) -> str:
        """Cadastra (ou atualiza) usuário + face num terminal Bio-T. Retorna 'created' ou
        'updated'."""
        _validate_jpeg(jpeg)
        status = self._ensure_user(employee_no, name)

        face_image = base64.b64encode(jpeg).decode("ascii")
        face_body = {"FaceList": [{"UserID": employee_no, "PhotoData": [face_image]}]}
        face_res = self._post("AccessFace.cgi", "insertMulti", face_body)
        if _ok(face_res):
            return status
        if "faceInfoManagerErrorPhotoExist" in face_res.text or status == "updated":
            # Foto já existia (ou é um re-cadastro de usuário já existente): endpoint
            # próprio pra atualizar face, documentado separadamente do insert.
            update_face_res = self._post("AccessFace.cgi", "updateMulti", face_body)
            if not _ok(update_face_res):
                raise FaceProvisioningError(f"falha ao atualizar face de {employee_no} em {self.terminal.host}: {update_face_res.text.strip()!r}")
            return "updated"
        raise FaceProvisioningError(f"falha ao enviar face de {employee_no} para {self.terminal.host}: {face_res.text.strip()!r}")

    def delete_user_info(self, employee_no: str) -> None:
        """removeMulti já apaga as credenciais associadas (face e cartão inclusos), sem
        precisar remover cada uma à parte — documentado explicitamente na doc oficial. Pra
        remover só o cartão, mantendo a face, use delete_card."""
        res = self._get("AccessUser.cgi", {"action": "removeMulti", "UserIDList[0]": employee_no})
        if not _ok(res):
            raise FaceProvisioningError(f"falha ao apagar usuário {employee_no} de {self.terminal.host}: {res.text.strip()!r}")

    # --- cartão ---
    # Recurso separado do usuário (AccessCard.cgi, não AccessUser.cgi) — cadastrar/remover
    # cartão nunca toca a face já cadastrada e vice-versa.

    def enroll_card(self, employee_no: str, name: str, card_no: str) -> str:
        """Cria (ou reaproveita) o usuário e vincula o cartão — pré-condição documentada
        oficialmente: usuário já tem que existir. Cartão não é atualizável in-place (doc
        oficial: pra trocar o número, remover e cadastrar de novo) — aqui só insere."""
        if not card_no:
            raise FaceProvisioningError("código do cartão vazio")
        status = self._ensure_user(employee_no, name)
        card_body = {"CardList": [{"UserID": employee_no, "CardNo": card_no, "CardType": 0, "CardStatus": 0}]}
        card_res = self._post("AccessCard.cgi", "insertMulti", card_body)
        if not _ok(card_res):
            raise FaceProvisioningError(f"falha ao cadastrar cartão de {employee_no} em {self.terminal.host}: {card_res.text.strip()!r}")
        return status

    def delete_card(self, employee_no: str, card_no: str) -> None:
        """Remove o cartão pelo número — AccessCard.cgi?action=removeMulti não aceita
        UserID, só CardNo. employee_no não é usado — mantido no parâmetro pela interface
        comum com os outros dois vendors."""
        res = self._get("AccessCard.cgi", {"action": "removeMulti", "CardNoList[0]": card_no})
        if not _ok(res):
            raise FaceProvisioningError(f"falha ao remover cartão {card_no} de {self.terminal.host}: {res.text.strip()!r}")

    # --- porta / reboot ---

    def open_door(self) -> dict:
        """Nunca lança — quem chama só loga o resultado, mesmo contrato dos outros clients."""
        try:
            res = self._get("accessControl.cgi", {"action": "openDoor", "channel": self.terminal.channel, "Type": "Remote"})
            if _ok(res):
                return {"ok": True}
            return {"ok": False, "reason": res.text.strip() or f"HTTP {res.status_code}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def reboot_terminal(self) -> dict:
        """Reinicia o terminal físico. Nunca lança — quem chama só loga o resultado."""
        try:
            res = self._get("magicBox.cgi", {"action": "reboot"})
            if _ok(res):
                return {"ok": True}
            return {"ok": False, "reason": res.text.strip() or f"HTTP {res.status_code}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    # --- eventos ---

    def fetch_access_records(self, start_epoch: int, end_epoch: int) -> list[dict]:
        """GET recordFinder.cgi?action=find&name=AccessControlCardRec — histórico de
        acesso do terminal (mistura face/cartão/senha no mesmo log; a doc não publica uma
        tabela pro campo Method que permita filtrar só facial com segurança). Resposta é
        texto plano 'records[i].Campo=valor' — parseada aqui pra uma lista de dicts, um
        por registro, na ordem em que vieram."""
        res = self._get("recordFinder.cgi", {"action": "find", "name": "AccessControlCardRec", "StartTime": start_epoch, "EndTime": end_epoch})
        if res.status_code != 200:
            raise FaceProvisioningError(f"recordFinder.cgi falhou em {self.terminal.host}: HTTP {res.status_code}")
        return _parse_record_finder(res.text)

    def fetch_picture(self, file_name: str) -> bytes | None:
        """Baixa a imagem de um evento (campo URL de um registro de fetch_access_records)
        via FileManager.cgi. Nunca lança — quem chama trata None como 'sem foto disponível'."""
        try:
            res = self._get("FileManager.cgi", {"action": "downloadFile", "fileName": file_name})
        except FaceProvisioningError as e:
            logger.warning("falha ao baixar foto do evento (%s): %s", self.terminal.host, e)
            return None
        if res.status_code == 200 and res.content:
            return res.content
        logger.warning("foto do evento indisponível (%s): HTTP %s", self.terminal.host, res.status_code)
        return None


def _ok(res: requests.Response) -> bool:
    return res.status_code == 200 and not any(prefix in res.text for prefix in _ERROR_CODE_PREFIXES)


def _validate_jpeg(jpeg: bytes) -> None:
    if len(jpeg) == 0:
        raise FaceProvisioningError("JPEG vazio")
    if len(jpeg) > MAX_JPEG_BYTES:
        raise FaceProvisioningError(f"JPEG de {len(jpeg)} bytes excede o limite da linha Bio-T ({MAX_JPEG_BYTES} bytes / 100KB)")
    if not (len(jpeg) > 3 and jpeg[0] == 0xFF and jpeg[1] == 0xD8 and jpeg[-2] == 0xFF and jpeg[-1] == 0xD9):
        raise FaceProvisioningError("buffer não parece ser um JPEG válido (magic bytes SOI/EOI ausentes)")


_RECORD_FIELD_RE = re.compile(r"^records\[(\d+)\]\.(\w+)=(.*)$")


def _parse_record_finder(text: str) -> list[dict]:
    """Parser da resposta multiline 'records[i].Campo=valor' do recordFinder.cgi."""
    records: dict[int, dict] = {}
    for line in text.splitlines():
        match = _RECORD_FIELD_RE.match(line.strip())
        if not match:
            continue
        idx, field, value = match.groups()
        records.setdefault(int(idx), {})[field] = value
    return [records[i] for i in sorted(records)]


def _parse_kv(text: str) -> dict:
    """Parser mínimo pra resposta 'chave=valor,chave2:valor2' do magicBox.cgi (a doc
    mistura '=' e ':' como separador em exemplos diferentes do mesmo endpoint)."""
    result = {}
    for part in text.strip().split(","):
        sep = "=" if "=" in part else ":" if ":" in part else None
        if sep is None:
            continue
        key, _, value = part.partition(sep)
        result[key.strip()] = value.strip()
    return result
