"""
Cliente ISAPI (Hikvision) para terminais faciais: Digest auth, enroll/revoke de
rosto, abertura remota de porta e reboot físico.

Porta em Python do TypeScript já validado ao vivo em produção no projeto-z-edu
(agente-local/src/isapi/{client,faceProvisioning,doorControl,reboot}.ts) contra
um DS-K1T671MF-L real, firmware V3.7.0. Não reinventa o handshake Digest
(RFC 2617) na mão como o TS faz — `requests` já cuida disso.
"""
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests
from requests.auth import HTTPDigestAuth

from .errors import FaceProvisioningError

logger = logging.getLogger(__name__)

MAX_JPEG_BYTES = 200 * 1024

# FDID "1" é a única lib de rosto exposta nesse modelo (faceLibType "blackFD") — apesar
# do nome, é a lib padrão de pessoas autorizadas, não uma blacklist literal.
FACE_LIBRARY_ID = "1"
FACE_LIBRARY_TYPE = "blackFD"

# Porta 1, template "1" sem restrição de horário — controle de horário é feito enrolando/
# removendo a pessoa inteira (ver plano de integração facial, seção "Agenda de acesso"),
# não por RightPlan custom no device. Sem esse campo o device cria a pessoa mas nega
# acesso silenciosamente (sem log de evento), mesmo com o rosto reconhecido.
UNRESTRICTED_TEMPLATE_NO = "1"

# ~5 falhas de senha trava a conta admin por ~30min no device — abre o circuito antes disso.
MAX_CONSEC_AUTH_FAILURES = 3
CIRCUIT_OPEN_SECONDS = 30 * 60

REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class HikvisionTerminal:
    host: str
    port: int
    username: str
    password: str
    https: bool = False
    verify_tls: bool = True  # False só é seguro em rede local confiável


class HikvisionClient:
    def __init__(self, terminal: HikvisionTerminal):
        self.terminal = terminal
        self._consec_auth_failures = 0
        self._circuit_open_until = 0.0

    def _base_url(self) -> str:
        proto = "https" if self.terminal.https else "http"
        return f"{proto}://{self.terminal.host}:{self.terminal.port}"

    def request(self, method: str, path: str, *, json_body=None, xml=None, files=None, query=None) -> requests.Response:
        """Chama um endpoint ISAPI. `xml` manda corpo XML cru (alguns endpoints só aceitam
        XML e devolvem HTTP 400 com `?format=json` — mesmo pitfall documentado no z-edu para
        RemoteControl/door e Event/notification/httpHosts). `files` é multipart (upload de foto)."""
        if time.monotonic() < self._circuit_open_until:
            raise FaceProvisioningError(
                f"circuit breaker aberto para {self.terminal.host}: muitas falhas de autenticação "
                f"seguidas, aguardando pra não travar a conta no device (lockout ~30min)"
            )

        url = self._base_url() + "/ISAPI/" + path.lstrip("/")
        params = dict(query or {})
        if xml is None:
            params.setdefault("format", "json")

        kwargs: dict = {}
        if xml is not None:
            kwargs["data"] = xml.encode("utf-8")
            kwargs["headers"] = {"Content-Type": "application/xml"}
        elif files is not None:
            kwargs["files"] = files
        elif json_body is not None:
            kwargs["json"] = json_body

        # Instância nova de HTTPDigestAuth a cada chamada, de propósito: reaproveitar uma
        # instância entre requisições faz o `requests` mandar Authorization preventivo com
        # o nonce cacheado e `nc` incrementado (otimização normal do RFC 2617) — mas esse
        # firmware não aceita isso na rota multipart (Intelligent/FDLib/FaceDataRecord) e
        # simplesmente reseta a conexão, sem responder 401. Forçar um desafio completo em
        # toda chamada custa um round-trip a mais, mas é o que funciona contra esse device
        # (validado ao vivo, reprodutível: cache de nonce = reset; sem cache = sempre OK).
        auth = HTTPDigestAuth(self.terminal.username, self.terminal.password)
        try:
            res = requests.request(
                method, url, auth=auth, params=params,
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs,
            )
        except requests.RequestException as e:
            raise FaceProvisioningError(f"falha de rede ao chamar {self.terminal.host}: {e}") from e

        if res.status_code == 401:
            self._register_auth_failure()
            raise FaceProvisioningError(
                f"falha de autenticação Digest no terminal {self.terminal.host} "
                f"(credenciais incorretas ou relógio dessincronizado > 5min — checar NTP antes de assumir senha errada)"
            )
        self._consec_auth_failures = 0
        return res

    def _register_auth_failure(self) -> None:
        self._consec_auth_failures += 1
        if self._consec_auth_failures >= MAX_CONSEC_AUTH_FAILURES:
            self._circuit_open_until = time.monotonic() + CIRCUIT_OPEN_SECONDS
            logger.warning(
                "circuit breaker aberto para %s: %d falhas de autenticação seguidas",
                self.terminal.host, self._consec_auth_failures,
            )

    # --- enrollment ---

    def enroll_face(self, employee_no: str, name: str, jpeg: bytes) -> str:
        """Cria (ou atualiza) o UserInfo e envia a foto. Retorna 'created' ou 'updated'."""
        _validate_jpeg(jpeg)
        status = self._upsert_user_info(employee_no, name)
        self._upload_face(employee_no, name, jpeg)
        return status

    def _upsert_user_info(self, employee_no: str, name: str) -> str:
        res = self.request("POST", "AccessControl/UserInfo/Record", json_body=_user_info_body(employee_no, name))
        if 200 <= res.status_code < 300:
            return "created"

        sub_status = _extract_sub_status(res)
        # Firmwares variam o subStatusCode pra "já existe" — esse modelo usa "employeeNoAlreadyExist".
        if sub_status in ("deviceUserAlreadyExist", "employeeNoAlreadyExist"):
            body = {"UserInfo": {"employeeNo": employee_no, "name": name, **_access_fields()}}
            res2 = self.request("PUT", "AccessControl/UserInfo/Modify", json_body=body)
            if not (200 <= res2.status_code < 300):
                raise FaceProvisioningError(f"falha ao atualizar UserInfo existente ({res2.status_code}): {res2.text}")
            return "updated"

        raise FaceProvisioningError(f"falha ao criar UserInfo ({res.status_code}, subStatusCode={sub_status}): {res.text}")

    def _upload_face(self, employee_no: str, name: str, jpeg: bytes) -> None:
        # Schema achatado (não aninhado sob "FaceDataRecord"). FPID = employeeNo por
        # convenção: amarra a entrada da lib facial de volta ao UserInfo.
        metadata = {"faceLibType": FACE_LIBRARY_TYPE, "FDID": FACE_LIBRARY_ID, "FPID": employee_no, "name": name}
        # Ordem importa: metadata JSON antes dos bytes JPEG — o device rejeita se a imagem vier primeiro.
        files = [
            ("FaceDataRecord", (None, json.dumps(metadata), "application/json")),
            ("img", ("face.jpg", jpeg, "image/jpeg")),
        ]
        res = self.request("POST", "Intelligent/FDLib/FaceDataRecord", files=files)
        if 200 <= res.status_code < 300:
            return

        if _extract_sub_status(res) == "deviceUserAlreadyExistFace":
            # Único caminho provado nesse firmware pra re-enroll (foto atualizada): nem PUT
            # nem DELETE direto em FaceDataRecord/FDSearch/Delete funcionam — apaga o
            # UserInfo inteiro (libera o FPID) e recria do zero.
            self.delete_user_info(employee_no)
            recreate = self.request("POST", "AccessControl/UserInfo/Record", json_body=_user_info_body(employee_no, name))
            if not (200 <= recreate.status_code < 300):
                raise FaceProvisioningError(f"falha ao recriar UserInfo pra re-enroll ({recreate.status_code}): {recreate.text}")
            retry = self.request("POST", "Intelligent/FDLib/FaceDataRecord", files=files)
            if 200 <= retry.status_code < 300:
                return
            raise FaceProvisioningError(f"falha ao reenviar face após recriar UserInfo ({retry.status_code}): {retry.text}")

        raise FaceProvisioningError(f"falha ao enviar face ({res.status_code}): {res.text}")

    def delete_user_info(self, employee_no: str) -> None:
        """Apaga o UserInfo inteiro (libera o FPID, remove a face e o cartão associados
        junto) — único caminho provado nesse firmware pra remover uma face. Pra remover só
        o cartão, mantendo a face, use delete_card."""
        body = {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
        res = self.request("PUT", "AccessControl/UserInfo/Delete", json_body=body)
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao apagar UserInfo ({res.status_code}): {res.text}")

    # --- cartão ---
    # Recurso separado do UserInfo (diferente da face, que vive dentro do próprio
    # UserInfo) — cadastrar/remover cartão nunca toca a face já cadastrada e vice-versa.
    # Documentação pública: ISAPI "Manage Card Information"
    # (https://tpp.hikvision.com/Wiki/ISAPI/Access%20Control%20on%20Person) — não validado
    # ao vivo ainda, diferente do resto deste client.

    def enroll_card(self, employee_no: str, name: str, card_no: str) -> str:
        """Cria (ou reaproveita) o UserInfo e vincula o cartão — chamável mesmo sem face
        cadastrada ainda. employeeNo/cardNo não são editáveis depois de criados: pra trocar
        o número, delete_card + enroll_card de novo."""
        if not card_no:
            raise FaceProvisioningError("código do cartão vazio")
        status = self._upsert_user_info(employee_no, name)
        body = {"CardInfo": {"employeeNo": employee_no, "cardNo": card_no}}
        res = self.request("POST", "AccessControl/CardInfo/Record", json_body=body)
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao cadastrar cartão de {employee_no} ({res.status_code}): {res.text}")
        return status

    def delete_card(self, employee_no: str, card_no: str | None = None) -> None:
        """Remove o cartão vinculado ao employeeNo (mantém a face, se houver) — a doc
        documenta EmployeeNoList e CardNoList como mutuamente exclusivos no mesmo delete;
        apagar por employeeNo evita ter que guardar o número do cartão só pra revogar
        depois. card_no não é usado — mantido no parâmetro pela interface comum com os
        outros dois vendors."""
        body = {"CardInfoDelCond": {"EmployeeNoList": [{"employeeNo": employee_no}]}}
        res = self.request("PUT", "AccessControl/CardInfo/Delete", json_body=body)
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao apagar cartão de {employee_no} ({res.status_code}): {res.text}")

    # --- porta / reboot ---

    def open_door(self) -> dict:
        """PUT RemoteControl/door/1 — corpo tem que ser XML, JSON dá HTTP 400 nesse endpoint
        específico (validado ao vivo no z-edu). Nunca lança — quem chama só loga o resultado."""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            "<cmd>open</cmd></RemoteControlDoor>"
        )
        try:
            res = self.request("PUT", "AccessControl/RemoteControl/door/1", xml=xml)
            if 200 <= res.status_code < 300:
                return {"ok": True}
            return {"ok": False, "reason": f"HTTP {res.status_code}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def reboot_terminal(self) -> dict:
        """Reinicia o terminal físico. Nunca lança — quem chama só loga o resultado."""
        try:
            res = self.request("PUT", "System/reboot")
            if 200 <= res.status_code < 300:
                return {"ok": True}
            return {"ok": False, "reason": f"HTTP {res.status_code}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def fetch_picture(self, picture_url: str) -> bytes | None:
        """Baixa a foto capturada num evento de reconhecimento (campo `pictureURL` de um
        AcsEvent minor=75) — URL completa servida pelo device fora do namespace /ISAPI/
        (ex.: /LOCALS/pic/...), mesma Digest auth. Validado ao vivo contra um DS-K1T671MF-L
        real. Nunca lança — quem chama trata None como 'sem foto disponível'."""
        try:
            res = requests.get(
                picture_url, auth=HTTPDigestAuth(self.terminal.username, self.terminal.password),
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            logger.warning("falha ao baixar foto do evento (%s): %s", self.terminal.host, e)
            return None
        if 200 <= res.status_code < 300 and res.content:
            return res.content
        logger.warning("foto do evento indisponível (%s): HTTP %s", self.terminal.host, res.status_code)
        return None

    def check_health(self) -> dict:
        """GET System/deviceInfo — endpoint ISAPI padrão presente em praticamente todo device
        Hikvision, leve e sem efeito colateral. Só pra status online/offline no painel."""
        res = self.request("GET", "System/deviceInfo")
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"System/deviceInfo retornou {res.status_code}: {res.text}")
        try:
            body = res.json()
            return body.get("DeviceInfo", body) if isinstance(body, dict) else {}
        except ValueError:
            pass
        # VALIDADO AO VIVO contra o DS-K1T671MF-L: esse endpoint alega
        # `Content-Type: application/json` mas manda XML de verdade no corpo mesmo com
        # `?format=json` — mesma família de pitfall do doorControl.ts/alertHost.ts do
        # z-edu, aqui num endpoint diferente. Fallback pra XML achatado.
        try:
            return _parse_flat_xml(res.text)
        except ET.ParseError:
            return {}


def _validate_jpeg(jpeg: bytes) -> None:
    if len(jpeg) == 0:
        raise FaceProvisioningError("JPEG vazio")
    if len(jpeg) > MAX_JPEG_BYTES:
        raise FaceProvisioningError(f"JPEG de {len(jpeg)} bytes excede o limite do terminal ({MAX_JPEG_BYTES} bytes / 200KB)")
    if not (len(jpeg) > 3 and jpeg[0] == 0xFF and jpeg[1] == 0xD8 and jpeg[-2] == 0xFF and jpeg[-1] == 0xD9):
        raise FaceProvisioningError("buffer não parece ser um JPEG válido (magic bytes SOI/EOI ausentes)")


def _access_fields() -> dict:
    return {"doorRight": "1", "RightPlan": [{"doorNo": 1, "planTemplateNo": UNRESTRICTED_TEMPLATE_NO}]}


def _user_info_body(employee_no: str, name: str) -> dict:
    return {
        "UserInfo": {
            "employeeNo": employee_no,
            "name": name,
            "userType": "normal",
            "Valid": {
                "enable": True,
                "beginTime": "2020-01-01T00:00:00",
                "endTime": "2037-12-31T23:59:59",
                "timeType": "local",
            },
            **_access_fields(),
        }
    }


def _extract_sub_status(res: requests.Response) -> str | None:
    try:
        data = res.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        value = data.get("subStatusCode")
        return value if isinstance(value, str) else None
    return None


def _parse_flat_xml(text: str) -> dict:
    """Parser mínimo pra XML achatado (sem aninhamento) tipo DeviceInfo — pega tag:texto de
    cada filho direto da raiz, ignorando o namespace (`{url}tag` -> `tag`). Não serve pra XML
    com estrutura aninhada (RightPlan, etc.) — só pra respostas simples tipo chave/valor."""
    root = ET.fromstring(text)
    return {child.tag.split("}")[-1]: child.text for child in root}
