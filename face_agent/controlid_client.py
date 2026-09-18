"""
Cliente REST (API "Linha de Acesso") pra terminais faciais Control iD
(iDFace, iDAccess, iDBlock etc.): sessão via login.fcgi, CRUD genérico de
objetos (create_objects/load_objects/destroy_objects), upload de foto
dedicado (user_set_image), ações remotas (execute_actions) e reboot.

Portado da documentação oficial (controlid.com.br/docs/access-api-pt) +
exemplos oficiais do fabricante (github.com/controlid/integracao) — nomes de
endpoint e formato de payload conferidos página por página, MAS ainda não
validado contra hardware real, igual o intelbras_biot_client.py. Primeira
integração real deve confirmar: comportamento de destroy_objects com `where`
vazio (não documentado — por isso clear_all_users lista e apaga por id em vez
de arriscar um "where: {}" que pode ou não significar "todos"), e o formato
exato do erro quando a sessão expira em uma chamada comum (a doc só documenta
o caminho de sucesso).

`_ControlIdBaseClient` (sessão/CRUD genérico de objetos) é compartilhado com
`controlid_uhf_client.py` — confirmado na doc oficial que a antena UHF usa a
MESMA API "Linha de Acesso" (o objeto `uhf_tags` já existe no CRUD genérico,
ao lado de `cards`/`qrcodes`), então não é um protocolo diferente por
fabricante (diferente do caso Intelbras XPE vs. Bio-T, que são de fato dois
protocolos distintos) — só um objeto e umas ações diferentes.
"""
import logging
import time
from dataclasses import dataclass

import requests

from .errors import FaceProvisioningError

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 2 * 1024 * 1024  # limite documentado do user_set_image.fcgi
REQUEST_TIMEOUT_SECONDS = 10


def _card_value_from_wiegand26(code: str) -> int:
    """VALIDADO AO VIVO comparando dois cartões reais já cadastrados neste equipamento
    (um antigo "52,04500", um lido na hora pela própria leitora "161,07404" — os números
    que o painel deles exibe já são o par facility-code/card-number Wiegand26): o
    `cards.value` que a API aceita NÃO é o inteiro de 24 bits direto do código impresso
    (`FC<<16 | CN`) — mandar isso dá cartão cadastrado só que a leitora não reconhece na
    presença física do cartão. O valor real gravado no equipamento espalha os mesmos FC/CN
    em 40 bits: `FC<<32 | CN`. Conferido batendo os dois pares fc,cn contra os dois valores
    brutos reais via load_objects — os dois batem exatamente com essa fórmula.
    `code` é o código de 24 bits (0..0xFFFFFF) já em decimal (ZAccess já converte hex->decimal
    na entrada, ver vehicleController.js/cardController.js)."""
    try:
        v = int(code)
    except ValueError:
        raise FaceProvisioningError(f"código {code!r} não é decimal (esperado inteiro)")
    if not (0 <= v <= 0xFFFFFF):
        raise FaceProvisioningError(f"código {code!r} fora da faixa de 24 bits (Wiegand26 FC+CN) esperada por este equipamento")
    fc = (v >> 16) & 0xFF
    cn = v & 0xFFFF
    return (fc << 32) | cn

# Guarda de segurança pra clear_all_*, mesmo raciocínio do Hikvision: nunca rodar pra
# sempre se o device tiver algum bug (lista sempre devolvendo os mesmos itens após o delete).
_MAX_CLEAR_ALL_BATCHES = 500
_CLEAR_ALL_BATCH_SIZE = 100


@dataclass
class ControlIdTerminal:
    host: str
    port: int
    username: str
    password: str
    https: bool = False
    verify_tls: bool = True  # False só é seguro em rede local confiável
    # Grupo (departamento, na UI do equipamento) ao qual todo usuário criado por aqui é
    # associado — validado ao vivo que SEM isso o usuário/tag fica sem nenhuma regra de
    # acesso (user_access_rules vem vazio nesses equipamentos; autorização é 100% via
    # group_access_rules -> access_rules -> portal_access_rules) e não autoriza passagem
    # nenhuma, mesmo com o cadastro em si tendo "funcionado". None = não associa a nada
    # (comportamento antigo — só use None se o equipamento já autorizar por padrão).
    group_id: int | None = None


class _ControlIdBaseClient:
    """Sessão HTTP + CRUD genérico de objetos, comum a qualquer equipamento da linha
    Control iD Access (facial ou antena UHF) — ver nota do módulo."""

    def __init__(self, terminal: ControlIdTerminal):
        self.terminal = terminal
        self._session: str | None = None

    def _base_url(self) -> str:
        proto = "https" if self.terminal.https else "http"
        return f"{proto}://{self.terminal.host}:{self.terminal.port}"

    def _login(self) -> str:
        try:
            res = requests.post(
                self._base_url() + "/login.fcgi",
                json={"login": self.terminal.username, "password": self.terminal.password},
                verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            raise FaceProvisioningError(f"falha de rede ao logar em {self.terminal.host}: {e}") from e
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"login falhou em {self.terminal.host} ({res.status_code}): {res.text}")
        try:
            session = res.json().get("session")
        except ValueError:
            session = None
        if not session:
            raise FaceProvisioningError(f"login em {self.terminal.host} não retornou session: {res.text}")
        self._session = session
        return session

    def _call(self, path: str, *, json_body=None, raw_body: bytes | None = None,
               content_type: str | None = None, extra_query: dict | None = None) -> requests.Response:
        """POST autenticado por sessão (query string `?session=...`, padrão da API Control
        iD). Sessão é cacheada e reusada entre chamadas; se a chamada falhar (qualquer
        status não-2xx), faz login de novo e tenta uma vez mais — a doc não especifica o
        formato exato de "sessão expirada" numa chamada comum, então trata qualquer falha
        como possível expiração em vez de arriscar não recuperar de uma sessão morta."""
        if self._session is None:
            self._login()

        def _do_request() -> requests.Response:
            params = {"session": self._session, **(extra_query or {})}
            kwargs: dict = {}
            if raw_body is not None:
                kwargs["data"] = raw_body
                kwargs["headers"] = {"Content-Type": content_type or "application/octet-stream"}
            elif json_body is not None:
                kwargs["json"] = json_body
            try:
                return requests.post(
                    self._base_url() + path, params=params,
                    verify=self.terminal.verify_tls, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs,
                )
            except requests.RequestException as e:
                raise FaceProvisioningError(f"falha de rede ao chamar {self.terminal.host}{path}: {e}") from e

        res = _do_request()
        if not (200 <= res.status_code < 300):
            self._login()
            res = _do_request()
        return res

    # --- objetos genéricos ("users", comum a face/cartão/tag UHF) ---

    def _find_user_id(self, employee_no: str) -> int | None:
        res = self._call("/load_objects.fcgi", json_body={
            "object": "users", "fields": ["id"], "where": {"users": {"registration": employee_no}},
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao buscar usuário {employee_no} em {self.terminal.host} ({res.status_code}): {res.text}")
        try:
            users = res.json().get("users") or []
        except ValueError:
            raise FaceProvisioningError(f"resposta não-JSON de load_objects(users) em {self.terminal.host}: {res.text}")
        return users[0]["id"] if users else None

    def find_registration_by_id(self, user_id: int) -> str | None:
        """Caminho inverso de `_find_user_id`: usado pra resolver o `employee_no`
        (registration) a partir do `user_id` interno que vem nos webhooks de evento
        (new_user_identified.fcgi só manda o id numérico, não a registration)."""
        res = self._call("/load_objects.fcgi", json_body={
            "object": "users", "fields": ["registration"], "where": {"users": {"id": user_id}},
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao buscar user_id {user_id} em {self.terminal.host} ({res.status_code}): {res.text}")
        try:
            users = res.json().get("users") or []
        except ValueError:
            raise FaceProvisioningError(f"resposta não-JSON de load_objects(users) em {self.terminal.host}: {res.text}")
        return users[0]["registration"] if users and users[0].get("registration") else None

    def _upsert_user(self, employee_no: str, name: str) -> tuple[int, str]:
        """Retorna (user_id, status) — status 'created' ou 'updated', mesmo contrato dos
        outros vendors (a API Control iD não faz upsert num único create_objects: registration
        duplicado dá erro, então busca antes)."""
        existing_id = self._find_user_id(employee_no)
        if existing_id is not None:
            if self.terminal.group_id is not None:
                self._join_group(existing_id, employee_no)
            return existing_id, "updated"
        res = self._call("/create_objects.fcgi", json_body={
            "object": "users", "values": [{"registration": employee_no, "name": name}],
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao criar usuário {employee_no} em {self.terminal.host} ({res.status_code}): {res.text}")
        try:
            ids = res.json().get("ids") or []
        except ValueError:
            ids = []
        if not ids:
            raise FaceProvisioningError(f"create_objects(users) não retornou id pra {employee_no} em {self.terminal.host}: {res.text}")
        new_id = ids[0]
        if self.terminal.group_id is not None:
            self._join_group(new_id, employee_no)
        return new_id, "created"

    def _join_group(self, user_id: int, employee_no: str) -> None:
        """Associa o usuário ao `terminal.group_id` — sem isso ele não tem
        `user_access_rules` (fica sempre vazio nesses equipamentos) nem entra em nenhum
        `group_access_rules`, ou seja, nenhuma regra de acesso o autoriza a passar por
        portal nenhum, mesmo com o cadastro (usuário + credencial) tendo dado certo.
        Idempotente: reenrolar um usuário que já está no grupo (ex.: adicionar um cartão
        depois) não deve tentar duplicar a associação, então verifica antes."""
        res = self._call("/load_objects.fcgi", json_body={
            "object": "user_groups", "fields": ["id"],
            "where": {"user_groups": {"user_id": user_id, "group_id": self.terminal.group_id}},
        })
        if 200 <= res.status_code < 300:
            try:
                if res.json().get("user_groups"):
                    return
            except ValueError:
                pass
        res = self._call("/create_objects.fcgi", json_body={
            "object": "user_groups", "values": [{"user_id": user_id, "group_id": self.terminal.group_id}],
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(
                f"usuário {employee_no} criado mas falhou ao associar ao grupo {self.terminal.group_id} "
                f"em {self.terminal.host} ({res.status_code}): {res.text}"
            )

    def _clear_all_users_impl(self) -> int:
        """Apaga TODOS os usuários (e credenciais associadas: face/cartão/tag UHF, via
        cascade documentado da própria API) do equipamento. Lista por id e apaga em lotes
        por `where: {id: {IN: [...]}}` em vez de um `destroy_objects` com `where` vazio —
        a doc não confirma que "sem filtro" significa "todos", e um erro de interpretação
        aqui é irreversível. Destrutivo — quem chama é responsável por confirmar antes.
        Não validado ao vivo ainda."""
        removed = 0
        for _ in range(_MAX_CLEAR_ALL_BATCHES):
            res = self._call("/load_objects.fcgi", json_body={
                "object": "users", "fields": ["id"], "limit": _CLEAR_ALL_BATCH_SIZE,
            })
            if not (200 <= res.status_code < 300):
                raise FaceProvisioningError(f"falha ao listar usuários de {self.terminal.host} ({res.status_code}): {res.text}")
            try:
                ids = [u["id"] for u in (res.json().get("users") or [])]
            except ValueError:
                raise FaceProvisioningError(f"resposta não-JSON de load_objects(users) em {self.terminal.host}: {res.text}")
            if not ids:
                return removed

            del_res = self._call("/destroy_objects.fcgi", json_body={
                "object": "users", "where": {"users": {"id": {"IN": ids}}},
            })
            if not (200 <= del_res.status_code < 300):
                raise FaceProvisioningError(f"falha ao apagar lote de usuários de {self.terminal.host} ({del_res.status_code}): {del_res.text}")
            removed += len(ids)
        raise FaceProvisioningError(
            f"clear_all em {self.terminal.host} não terminou após {_MAX_CLEAR_ALL_BATCHES} lotes "
            f"({removed} removidos até aqui) — abortado por segurança, pode ser um bug no device"
        )

    def delete_user_info(self, employee_no: str) -> None:
        """Apaga o usuário inteiro (libera a registration, remove credenciais associadas
        junto) — mesmo contrato dos outros vendors."""
        res = self._call("/destroy_objects.fcgi", json_body={
            "object": "users", "where": {"users": {"registration": employee_no}},
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao apagar usuário {employee_no} de {self.terminal.host} ({res.status_code}): {res.text}")

    # --- ações remotas / reboot / health (comuns) ---

    def _execute_action(self, action: str, parameters: str) -> dict:
        """POST execute_actions.fcgi — nunca lança, quem chama só loga o resultado."""
        try:
            res = self._call("/execute_actions.fcgi", json_body={
                "actions": [{"action": action, "parameters": parameters}],
            })
            if 200 <= res.status_code < 300:
                return {"ok": True}
            return {"ok": False, "reason": f"HTTP {res.status_code}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def reboot_terminal(self) -> dict:
        try:
            res = self._call("/reboot.fcgi")
            if 200 <= res.status_code < 300:
                return {"ok": True}
            return {"ok": False, "reason": f"HTTP {res.status_code}"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def check_health(self) -> dict:
        """POST system_information.fcgi — leve, sem efeito colateral, só pra status
        online/offline no painel."""
        res = self._call("/system_information.fcgi")
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"system_information.fcgi retornou {res.status_code}: {res.text}")
        try:
            return res.json()
        except ValueError:
            return {}


class ControlIdClient(_ControlIdBaseClient):
    # --- enrollment (face) ---

    def enroll_face(self, employee_no: str, name: str, jpeg: bytes) -> str:
        _validate_image(jpeg)
        user_id, status = self._upsert_user(employee_no, name)
        res = self._call(
            "/user_set_image.fcgi", raw_body=jpeg, content_type="application/octet-stream",
            extra_query={"user_id": user_id, "timestamp": int(time.time()), "match": 0},
        )
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao enviar foto de {employee_no} pra {self.terminal.host} ({res.status_code}): {res.text}")
        try:
            body = res.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("success") is False:
            raise FaceProvisioningError(f"terminal {self.terminal.host} rejeitou a foto de {employee_no}: {body}")
        return status

    def clear_all_users(self) -> int:
        return self._clear_all_users_impl()

    # --- cartão ---

    def enroll_card(self, employee_no: str, name: str, card_no: str) -> str:
        if not card_no:
            raise FaceProvisioningError("código do cartão vazio")
        card_value = _card_value_from_wiegand26(card_no)
        user_id, status = self._upsert_user(employee_no, name)
        res = self._call("/create_objects.fcgi", json_body={
            "object": "cards", "values": [{"value": card_value, "user_id": user_id}],
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao cadastrar cartão de {employee_no} em {self.terminal.host} ({res.status_code}): {res.text}")
        return status

    def delete_card(self, employee_no: str, card_no: str) -> None:
        if not card_no:
            raise FaceProvisioningError("código do cartão vazio")
        card_value = _card_value_from_wiegand26(card_no)
        user_id = self._find_user_id(employee_no)
        if user_id is None:
            return  # já não existe — remoção idempotente, mesmo espírito dos outros vendors
        res = self._call("/destroy_objects.fcgi", json_body={
            "object": "cards", "where": {"cards": {"user_id": user_id, "value": card_value}},
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao apagar cartão {card_no} de {employee_no} em {self.terminal.host} ({res.status_code}): {res.text}")

    # --- porta ---

    def open_door(self, door: int = 1) -> dict:
        return self._execute_action("door", f"door={door}")


def _validate_image(image: bytes) -> None:
    if len(image) == 0:
        raise FaceProvisioningError("imagem vazia")
    if len(image) > MAX_IMAGE_BYTES:
        raise FaceProvisioningError(f"imagem de {len(image)} bytes excede o limite do terminal ({MAX_IMAGE_BYTES} bytes / 2MB)")
    is_jpeg = len(image) > 3 and image[0] == 0xFF and image[1] == 0xD8 and image[-2] == 0xFF and image[-1] == 0xD9
    is_png = image[:8] == b"\x89PNG\r\n\x1a\n"
    if not (is_jpeg or is_png):
        raise FaceProvisioningError("buffer não parece ser um JPEG/PNG válido (magic bytes ausentes)")
