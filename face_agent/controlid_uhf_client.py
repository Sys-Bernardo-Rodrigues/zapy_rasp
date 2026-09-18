"""
Cliente pra antena veicular Control iD iDUHF — mesma API "Linha de Acesso" do
terminal facial (ver controlid_client.py e a nota no topo daquele módulo).

VALIDADO AO VIVO contra uma iDUHF Lite real (firmware 5.14.9): a leitura da
tag é reconhecida pela antena como CARTÃO, não pelo objeto `uhf_tags` da doc
(que existe na API mas fica vazio/não é o que a antena de fato consulta pra
autorizar passagem — confirmado comparando os dois objetos num equipamento
com dezenas de veículos já funcionando: todos em `cards`, só sobras de teste
em `uhf_tags`). Por isso `enroll_tag`/`delete_tag` usam `object: "cards"`,
apesar do nome dos métodos continuar "tag" (é o vocabulário do resto do
sistema — ZAccess/painel chamam de "tag UHF", só o objeto na API é `cards`
mesmo). Pode ser um comportamento só dessa linha/firmware específico — se uma
antena futura precisar de `uhf_tags` de verdade, isso vira um parâmetro.

O `value` desse `cards` também não é o inteiro cru de 24 bits do código impresso —
ver `_card_value_from_wiegand26` em controlid_client.py pra a codificação real
(validada comparando cartões reais já cadastrados no equipamento).

Reaproveita o `_ControlIdBaseClient` (sessão, CRUD genérico de "users",
reboot, health) — só o que é específico de credencial UHF/abertura de
cancela vive aqui.
"""
from .controlid_client import ControlIdTerminal, _ControlIdBaseClient, _card_value_from_wiegand26
from .errors import FaceProvisioningError

__all__ = ["ControlIdTerminal", "ControlIdUhfClient"]


class ControlIdUhfClient(_ControlIdBaseClient):
    def __init__(self, terminal: ControlIdTerminal, *, gate_output: str = "contact", door_id: int = 1, secbox_id: int | None = None):
        """`gate_output`: "contact" (saída/relé embutido da própria antena — ação "door" da
        doc, `door_id` é o índice dessa saída) ou "secbox" (módulo SecBox externo entre a
        antena e o motor do portão — ação "sec_box", precisa de `secbox_id`, o id do objeto
        `sec_boxs` já cadastrado NA própria antena, não um id nosso). Depende de como a
        instalação foi cabeada — escolha errada não abre a cancela."""
        super().__init__(terminal)
        self.gate_output = gate_output
        self.door_id = door_id
        self.secbox_id = secbox_id

    # --- tag UHF ---

    def enroll_tag(self, employee_no: str, name: str, tag_code: str) -> str:
        """`employee_no` é o mesmo identificador estável usado pro dono do veículo nos
        outros clients (ex.: já tem face/cartão em outro terminal) — Control iD trata tag,
        cartão e face como credenciais independentes do mesmo `users.registration`, então
        reusar o employee_no aqui é o que deixa `find_registration_by_id` funcionar igual
        nos webhooks de evento desta antena."""
        if not tag_code:
            raise FaceProvisioningError("código da tag UHF vazio")
        tag_value = _card_value_from_wiegand26(tag_code)
        user_id, status = self._upsert_user(employee_no, name)
        res = self._call("/create_objects.fcgi", json_body={
            "object": "cards", "values": [{"value": tag_value, "user_id": user_id}],
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao cadastrar tag UHF de {employee_no} em {self.terminal.host} ({res.status_code}): {res.text}")
        return status

    def delete_tag(self, employee_no: str, tag_code: str) -> None:
        """Remove só ESSA credencial (tag) — mesmo contrato de `delete_card` no terminal
        facial, nunca mexe no `users`. Apagar o usuário inteiro é outra operação
        (`delete_user_info`, herdado de `_ControlIdBaseClient`), usada só quando o morador
        inteiro é excluído no ZAccess (`vehicle_user:revoke` em zaccess_client.py) — dois
        tipos de exclusão distintos, não um efeito colateral um do outro."""
        if not tag_code:
            raise FaceProvisioningError("código da tag UHF vazio")
        tag_value = _card_value_from_wiegand26(tag_code)
        user_id = self._find_user_id(employee_no)
        if user_id is None:
            return  # já não existe — remoção idempotente, mesmo espírito do enroll_card
        res = self._call("/destroy_objects.fcgi", json_body={
            "object": "cards", "where": {"cards": {"user_id": user_id, "value": tag_value}},
        })
        if not (200 <= res.status_code < 300):
            raise FaceProvisioningError(f"falha ao apagar tag UHF {tag_code} de {employee_no} em {self.terminal.host} ({res.status_code}): {res.text}")

    def clear_all_tags(self) -> int:
        return self._clear_all_users_impl()

    # --- cancela / portão ---

    def open_gate(self) -> dict:
        """"catra" é só pra catraca de pedestre (não se aplica aqui) — as duas opções reais
        pra cancela veicular são a saída/relé embutido da antena ("door") ou um módulo
        SecBox externo ("sec_box"), conforme `gate_output` configurado no cadastro desta
        antena. `reason=3` no sec_box é o valor de exemplo da doc oficial pra abertura
        remota — não documentado com outros códigos, confirmar contra a antena real."""
        if self.gate_output == "secbox":
            if self.secbox_id is None:
                return {"ok": False, "reason": "secbox_id não configurado pra essa antena"}
            return self._execute_action("sec_box", f"id={self.secbox_id}, reason=3")
        return self._execute_action("door", f"door={self.door_id}")
