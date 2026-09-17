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
from urllib.parse import urlparse

import requests

from .errors import FaceProvisioningError

logger = logging.getLogger(__name__)

MAX_JPEG_BYTES = 200 * 1024
REQUEST_TIMEOUT_SECONDS = 10

_CARD_CODE_SEP = ","  # CardCode guarda múltiplos cartões nesse formato: "AAABBB,00112233"


def _split_card_codes(raw) -> list[str]:
    if not raw:
        return []
    return [c for c in str(raw).split(_CARD_CODE_SEP) if c]


def _swap_card_bytes(hexstr: str) -> str:
    """XPE3200: a leitora embutida do terminal inverte a ordem dos bytes do código do
    cartão em relação à conversão hexadecimal padrão (decimal->hex "de livro"). Confirmado
    com hardware real: cartão decimal 2422164881 = 0x905F4D91 na conversão padrão, mas o
    terminal cadastra/lê 0x914D5F90 (bytes revertidos). Sem essa correção, um CardCode
    gravado via API nunca bate com o que a leitora física da porta lê do mesmo cartão."""
    if len(hexstr) % 2:
        hexstr = "0" + hexstr
    pairs = [hexstr[i:i + 2] for i in range(0, len(hexstr), 2)]
    return "".join(reversed(pairs))


def _card_no_to_device_code(card_no: str) -> str:
    """ZAccess sempre manda card_no em decimal (o número impresso no cartão) — converte
    pro hex de 4 bytes que o CardCode do XPE espera (doc oficial, ex.: "12EA3004") e já
    aplica a inversão de bytes da leitora embutida (_swap_card_bytes)."""
    try:
        value = int(str(card_no).strip())
    except (TypeError, ValueError):
        raise FaceProvisioningError(f"código do cartão inválido, esperado decimal: {card_no!r}")
    return _swap_card_bytes(format(value, "08X"))


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

    def _upsert_credential(self, employee_no: str, name: str, build_credential) -> str:
        """Cria (ou atualiza) o usuário aplicando o que `build_credential(existing)` devolver
        (ex.: {"FaceImage": ...} ou {"CardCode": ...}) por cima do que já existe no device —
        preserva qualquer outra credencial já cadastrada (user/set não é patch parcial:
        omitir um campo existente apaga ele, validado ao vivo em produção). `existing` (o
        usuário já buscado, ou None) é repassado pra quem monta a credencial poder decidir
        com base no que já está lá (ex.: somar um cartão à lista) sem precisar buscar nem
        mais uma vez. Retorna 'created' ou 'updated'."""
        existing = self._find_existing_user(employee_no)
        item = self._user_item(employee_no, name)
        if existing:
            for key in self._PRESERVED_FIELDS:
                value = existing.get(key)
                if value not in (None, ""):
                    item[key] = value
        item.update(build_credential(existing))

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
        return self._upsert_credential(employee_no, name, lambda existing: {"FaceImage": face_image})

    def enroll_card(self, employee_no: str, name: str, card_no: str) -> str:
        """Adiciona um cartão ao usuário, preservando face e outros cartões já cadastrados
        — CardCode guarda múltiplos cartões por pessoa separados por vírgula (documentado
        oficialmente: "AAABBB,00112233"). card_no chega em decimal (número impresso no
        cartão, mesmo formato pro painel inteiro) — ver _card_no_to_device_code pra
        conversão pro hex de 4 bytes + inversão que a leitora embutida do XPE3200 espera.
        Idempotente: card_no já presente não duplica na lista."""
        if not card_no:
            raise FaceProvisioningError("código do cartão vazio")

        device_code = _card_no_to_device_code(card_no)

        def build(existing):
            codes = _split_card_codes(existing.get("CardCode")) if existing else []
            if device_code not in codes:
                codes.append(device_code)
            return {"CardCode": _CARD_CODE_SEP.join(codes)}

        return self._upsert_credential(employee_no, name, build)

    def delete_card(self, employee_no: str, card_no: str) -> None:
        """Remove só um cartão específico do usuário (mantém a face e os outros cartões) —
        reescreve CardCode sem o card_no informado (mesma conversão de bytes do enroll_card,
        pra achar a entrada certa na lista). Idempotente: usuário ou cartão inexistente não
        lança."""
        existing = self._find_existing_user(employee_no)
        if existing is None:
            return
        device_code = _card_no_to_device_code(card_no)
        self._upsert_credential(
            employee_no, existing.get("Name") or employee_no,
            lambda e: {"CardCode": _CARD_CODE_SEP.join(c for c in _split_card_codes(e.get("CardCode")) if c != device_code)},
        )

    def delete_user_info(self, employee_no: str) -> None:
        """UserID sozinho já basta pra apagar, sem precisar do ID interno. Remove a pessoa
        inteira (face + cartão) — pra remover só o cartão, use delete_card."""
        res = self.call("user", "del", {"item": [{"UserID": employee_no}]})
        if res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao apagar usuário {employee_no}: {res.get('message')}")

    def clear_all_users(self) -> None:
        """Apaga TODOS os usuários (face + cartão) do terminal — zera o device inteiro.
        Endpoint dedicado, documentado oficialmente (api/user/clear). Destrutivo e
        irreversível no hardware — quem chama é responsável por confirmar antes."""
        res = self.call("user", "clear")
        if res.get("retcode") != 0:
            raise FaceProvisioningError(f"falha ao limpar usuários de {self.terminal.host}: {res.get('message')}")

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

    def capture_snapshot(self) -> bytes | None:
        """Tira uma foto AO VIVO da câmera (Api Snapshot, `target: snapshot, action: get`,
        firmware >= 116.57.2.116, documentada no manual) — usado quando a porta é liberada
        pelo cockpit/console, caso em que não existe evento de doorlog ainda pra puxar
        Picture. Resposta vem em data URI base64 (`data:image/jpeg;base64,...`). Nunca
        lança — quem chama trata None como 'sem foto disponível'."""
        try:
            res = self.call("snapshot", "get")
        except FaceProvisioningError as e:
            logger.warning("falha ao capturar snapshot (%s): %s", self.terminal.host, e)
            return None
        if res.get("retcode") != 0:
            logger.warning("snapshot indisponível (%s): %s", self.terminal.host, res.get("message"))
            return None
        raw = ((res.get("data") or {}).get("snapshot") or "")
        b64 = raw.split(",", 1)[1] if "," in raw else raw
        if not b64:
            return None
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            logger.warning("snapshot com base64 inválido (%s): %s", self.terminal.host, e)
            return None

    def fetch_picture(self, picture_ref: str) -> bytes | None:
        """Baixa a foto de um evento do doorlog (campo `Picture`, presente inclusive pra
        acesso negado/desconhecido — documentado no manual, seção "Eventos em tempo real").
        O device embute scheme+host PRÓPRIO na URL (sempre https, ex.:
        "https://10.101.1.121/Image/DoorPicture/foo.jpg") mesmo quando a HTTP API está
        configurada em http puro — validado ao vivo: esse https do device usa um
        certificado fraco que o Python recusa negociar (EE certificate key too weak).
        Por isso descarta o scheme+host que o device manda e sempre refaz a URL em cima de
        `_base_url()` (mesma config http/https/porta já validada pra chamar a API); se vier
        só o nome do arquivo (sem path), monta em /Image/DoorPicture/<arquivo> (mesmo padrão
        de /Image/RegisterImg documentado pro cadastro de face). Basic Auth, igual toda
        chamada da API. Nunca lança — quem chama trata None como 'sem foto disponível'."""
        path = urlparse(picture_ref).path if "://" in picture_ref else None
        url = f"{self._base_url()}{path}" if path else f"{self._base_url()}/Image/DoorPicture/{picture_ref}"
        try:
            res = requests.get(
                url, auth=(self.terminal.username, self.terminal.password),
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            logger.warning("falha ao baixar foto do evento (%s): %s", self.terminal.host, e)
            return None
        if 200 <= res.status_code < 300 and res.content:
            return res.content
        logger.warning("foto do evento indisponível (%s): HTTP %s", self.terminal.host, res.status_code)
        return None


def _validate_jpeg(jpeg: bytes) -> None:
    if len(jpeg) == 0:
        raise FaceProvisioningError("JPEG vazio")
    if len(jpeg) > MAX_JPEG_BYTES:
        raise FaceProvisioningError(f"JPEG de {len(jpeg)} bytes excede o limite ({MAX_JPEG_BYTES} bytes / 200KB)")
    if not (len(jpeg) > 3 and jpeg[0] == 0xFF and jpeg[1] == 0xD8 and jpeg[-2] == 0xFF and jpeg[-1] == 0xD9):
        raise FaceProvisioningError("buffer não parece ser um JPEG válido (magic bytes SOI/EOI ausentes)")
