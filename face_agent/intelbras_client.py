"""
Cliente da HTTP API nativa do terminal Intelbras XPE (toggle em Segurança >
HTTP API no device) — REST JSON com Basic Auth, envelope de request/response
`{target,action,data}` / `{retcode,action,message,data}`. Bem mais simples que
o ISAPI do Hikvision: sem Digest, sem XML/multipart.

Porta em Python do TypeScript já validado ao vivo em produção no projeto-z-edu
(agente-local/src/intelbras/{client,faceProvisioning,doorControl,reboot}.ts)
contra um XPE-3200-PLUS-IP real, firmware 216.57.1.26. Documentado em
XPE3200_IP_FACE_Http_API_de_Integração.pdf.
"""
import base64
import logging
from dataclasses import dataclass

import requests

from .errors import FaceProvisioningError

logger = logging.getLogger(__name__)

MAX_JPEG_BYTES = 200 * 1024
REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class IntelbrasTerminal:
    host: str
    port: int
    username: str
    password: str
    https: bool = False
    verify_tls: bool = True  # False só é seguro em rede local confiável
    # NO-COM(0)/NC-COM(1) — depende de como o relé físico foi fiado na instalação, não é
    # constante do produto. Se um terminal novo não abrir a porta (ou abrir invertido),
    # esse é o primeiro parâmetro a tentar.
    relay_level: int = 0


class IntelbrasClient:
    def __init__(self, terminal: IntelbrasTerminal):
        self.terminal = terminal

    def _base_url(self) -> str:
        proto = "https" if self.terminal.https else "http"
        return f"{proto}://{self.terminal.host}:{self.terminal.port}"

    def call(self, target: str, action: str, data: dict | None = None) -> dict:
        """POST /api/{target}/{action} — todas as ações usadas aqui só aceitam POST
        segundo a doc, mesmo as de leitura."""
        url = f"{self._base_url()}/api/{target}/{action}"
        payload: dict = {"target": target, "action": action}
        if data is not None:
            payload["data"] = data
        try:
            res = requests.post(
                url, json=payload,
                auth=(self.terminal.username, self.terminal.password),
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            raise FaceProvisioningError(f"falha de rede ao chamar {self.terminal.host}: {e}") from e
        if res.status_code != 200:
            raise FaceProvisioningError(f"HTTP API de {self.terminal.host} respondeu {res.status_code}: {res.text}")
        try:
            return res.json()
        except ValueError:
            raise FaceProvisioningError(f"resposta não-JSON da HTTP API de {self.terminal.host}: {res.text}")

    def check_health(self) -> dict:
        """Ping leve pra saber se a HTTP API do terminal está respondendo — sem efeito
        colateral, seguro pra chamar toda hora."""
        res = self.call("system", "info")
        if res.get("retcode") != 0:
            raise FaceProvisioningError(f"system/info retornou retcode {res.get('retcode')}: {res.get('message')}")
        return res.get("data") or {}

    # --- enrollment ---

    def _user_item(self, employee_no: str, name: str, face_image: str | None = None) -> dict:
        # Validity/Relay nunca variam por pessoa: controle de horário é 100% do agente,
        # terminal só sabe "liberado" ou "removido". user/set não é patch parcial — reenviar
        # esses campos em toda chamada evita que um `set` sem eles limpe Validity/Relay.
        item = {"UserID": employee_no, "Name": name, "Validity": "0", "Relay": "1"}
        if face_image is not None:
            item["FaceImage"] = face_image
        return item

    def _find_internal_id(self, employee_no: str) -> int | None:
        res = self.call("user", "get")
        if res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao listar usuários de {self.terminal.host}: {res.get('message')}")
        items = ((res.get("data") or {}).get("item")) or []
        for item in items:
            if item.get("UserID") == employee_no:
                return item.get("ID")
        return None

    def enroll_face(self, employee_no: str, name: str, jpeg: bytes) -> str:
        """Cria (ou, se já existir, atualiza) o usuário com a face embutida no mesmo POST.
        Retorna 'created' ou 'updated'."""
        _validate_jpeg(jpeg)
        face_image = base64.b64encode(jpeg).decode("ascii")

        add_res = self.call("user", "add", {"item": [self._user_item(employee_no, name, face_image)]})
        if add_res.get("retcode") == 0:
            return "created"
        # Duplicata retorna essa mensagem exata (sem código de erro dedicado) — sinal pra
        # cair no caminho de update, que exige o ID interno (não temos salvo, listamos pra achar).
        if add_res.get("message") != "User already exist":
            raise FaceProvisioningError(f"falha ao criar usuário {employee_no}: {add_res.get('message')}")

        internal_id = self._find_internal_id(employee_no)
        if internal_id is None:
            raise FaceProvisioningError(f"terminal disse que {employee_no} já existe, mas não achei na listagem de usuários")
        set_res = self.call("user", "set", {"item": [{"ID": str(internal_id), **self._user_item(employee_no, name, face_image)}]})
        if set_res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao atualizar usuário {employee_no} (ID {internal_id}): {set_res.get('message')}")
        return "updated"

    def delete_user_info(self, employee_no: str) -> None:
        """UserID sozinho já basta pra apagar, sem precisar do ID interno."""
        res = self.call("user", "del", {"item": [{"UserID": employee_no}]})
        if res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao apagar usuário {employee_no}: {res.get('message')}")

    # --- porta / reboot ---

    def open_door(self) -> dict:
        """target 'relay' action 'trig' — abre a catraca sem reconhecimento facial. mode 0
        (auto-close) garante que o relé fecha sozinho depois de `delay` segundos, nunca
        destrava a porta indefinidamente. Nunca lança — quem chama só loga o resultado."""
        try:
            res = self.call("relay", "trig", {"mode": 0, "num": 1, "level": self.terminal.relay_level, "delay": 5})
            if res.get("retcode") == 0:
                return {"ok": True}
            return {"ok": False, "reason": res.get("message") or f"retcode {res.get('retcode')}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def reboot_terminal(self) -> dict:
        """Reinicia o terminal físico. Nunca lança — quem chama só loga o resultado."""
        try:
            res = self.call("system", "reboot")
            if res.get("retcode") == 0:
                return {"ok": True}
            return {"ok": False, "reason": res.get("message") or f"retcode {res.get('retcode')}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}


def _validate_jpeg(jpeg: bytes) -> None:
    if len(jpeg) == 0:
        raise FaceProvisioningError("JPEG vazio")
    if len(jpeg) > MAX_JPEG_BYTES:
        raise FaceProvisioningError(f"JPEG de {len(jpeg)} bytes excede o limite ({MAX_JPEG_BYTES} bytes / 200KB)")
    if not (len(jpeg) > 3 and jpeg[0] == 0xFF and jpeg[1] == 0xD8 and jpeg[-2] == 0xFF and jpeg[-1] == 0xD9):
        raise FaceProvisioningError("buffer não parece ser um JPEG válido (magic bytes SOI/EOI ausentes)")
