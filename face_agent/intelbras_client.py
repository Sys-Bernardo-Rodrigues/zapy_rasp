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

    # Campos de credencial que podem já existir no usuário (enrolado por fora do ZAccess,
    # ou por uma chamada anterior de credencial diferente) — user/set reescreve o item
    # inteiro, então precisam ser preservados explicitamente pra uma chamada de cartão não
    # apagar a face já cadastrada (e vice-versa). PrivatePIN/LiftFloorNum/ScheduleRelay/
    # WebRelay também entram aqui pelo mesmo motivo, mesmo sem uso direto pelo ZAccess hoje.
    _PRESERVED_FIELDS = ("CardCode", "FaceImage", "PrivatePIN", "LiftFloorNum", "ScheduleRelay", "WebRelay")

    def _user_item(self, employee_no: str, name: str) -> dict:
        # Validity/Relay nunca variam por pessoa: controle de horário é 100% do agente,
        # terminal só sabe "liberado" ou "removido".
        return {"UserID": employee_no, "Name": name, "Validity": "0", "Relay": "1"}

    def _find_existing_user(self, employee_no: str) -> dict | None:
        """Busca o item completo do usuário (não só o ID interno) — usado tanto pra achar
        o ID de update quanto pra preservar credenciais já existentes."""
        res = self.call("user", "get")
        if res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao listar usuários de {self.terminal.host}: {res.get('message')}")
        items = ((res.get("data") or {}).get("item")) or []
        for item in items:
            if item.get("UserID") == employee_no:
                return item
        return None

    def _upsert_credential(self, employee_no: str, name: str, credential: dict) -> str:
        """Cria (ou atualiza) o usuário aplicando `credential` (ex.: {"FaceImage": ...} ou
        {"CardCode": ...}) por cima do que já existe no device — preserva qualquer outra
        credencial já cadastrada (user/set não é patch parcial: omitir um campo existente
        apaga ele, validado ao vivo em produção). Retorna 'created' ou 'updated'."""
        existing = self._find_existing_user(employee_no)
        item = self._user_item(employee_no, name)
        if existing:
            for key in self._PRESERVED_FIELDS:
                value = existing.get(key)
                if value not in (None, ""):
                    item[key] = value
        item.update(credential)

        if existing is None:
            add_res = self.call("user", "add", {"item": [item]})
            if add_res.get("retcode") == 0:
                return "created"
            # Duplicata retorna essa mensagem exata (sem código de erro dedicado) — corrida
            # entre o get acima e o add (outra chamada criou o usuário nesse meio-tempo).
            if add_res.get("message") != "User already exist":
                raise FaceProvisioningError(f"falha ao criar usuário {employee_no}: {add_res.get('message')}")
            existing = self._find_existing_user(employee_no)
            if existing is None:
                raise FaceProvisioningError(f"terminal disse que {employee_no} já existe, mas não achei na listagem de usuários")

        set_res = self.call("user", "set", {"item": [{"ID": str(existing["ID"]), **item}]})
        if set_res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao atualizar usuário {employee_no} (ID {existing['ID']}): {set_res.get('message')}")
        return "updated"

    def enroll_face(self, employee_no: str, name: str, jpeg: bytes) -> str:
        """Cria (ou atualiza) o usuário com a face embutida, preservando cartão já
        cadastrado. Retorna 'created' ou 'updated'."""
        _validate_jpeg(jpeg)
        face_image = base64.b64encode(jpeg).decode("ascii")
        return self._upsert_credential(employee_no, name, {"FaceImage": face_image})

    def enroll_card(self, employee_no: str, name: str, card_no: str) -> str:
        """Cria (ou atualiza) o usuário com o cartão, preservando face já cadastrada.
        card_no é tratado como opaco (formato hexadecimal por convenção do device). O
        device aceita múltiplos cartões por pessoa via CardCode separado por vírgula, mas
        essa camada só lida com um cartão por vez.
        ponytail: suporte a múltiplos cartões por pessoa, se algum dia precisar."""
        if not card_no:
            raise FaceProvisioningError("código do cartão vazio")
        return self._upsert_credential(employee_no, name, {"CardCode": card_no})

    def delete_card(self, employee_no: str, card_no: str | None = None) -> None:
        """Remove só o cartão do usuário (mantém a face, se houver) — CardCode vazio some
        o campo sem apagar o resto do cadastro. card_no não é usado (XPE não guarda
        múltiplos cartões separados aqui, só o campo único) — mantido no parâmetro pela
        interface comum com os outros dois vendors. Idempotente: usuário inexistente não
        lança."""
        existing = self._find_existing_user(employee_no)
        if existing is None:
            return
        self._upsert_credential(employee_no, existing.get("Name") or employee_no, {"CardCode": ""})

    def delete_user_info(self, employee_no: str) -> None:
        """UserID sozinho já basta pra apagar, sem precisar do ID interno. Remove a pessoa
        inteira (face + cartão) — pra remover só o cartão, use delete_card."""
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
