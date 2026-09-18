"""
Persistência local (JSON) das antenas veiculares UHF configuradas neste zapy —
mesmo espírito de face_terminals_store.py, mas pra antenas (Control iD iDUHF)
em vez de terminais faciais: só um fabricante suportado até agora.
"""
import json
import os
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(BASE_DIR, "vehicle_antennas.json")

VENDORS = ("controlid",)
GATE_OUTPUTS = ("contact", "secbox")
_DEFAULTS = {
    "name": "",
    "vendor": "controlid",
    "host": "",
    "port": 80,
    "username": "",
    "password": "",
    "https": False,
    "verify_tls": True,
    "cockpit_enabled": True,  # porteiro pode abrir a cancela dessa antena em /cockpit
    # Como a cancela é acionada — saída/relé embutido da própria antena ("contact",
    # usa door_id) ou um módulo SecBox externo entre a antena e o motor ("secbox",
    # usa secbox_id, o id do objeto sec_boxs já cadastrado NA antena) — depende de
    # como a instalação foi cabeada.
    "gate_output": "contact",
    "door_id": 1,
    "secbox_id": "",
    # Grupo/departamento ao qual todo usuário criado por aqui é associado — sem isso o
    # usuário fica sem regra de acesso nenhuma (validado ao vivo). "" = não associa.
    "group_id": "",
}


def _read_all() -> list[dict]:
    if not os.path.isfile(STORE_PATH):
        return []
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def _write_all(antennas: list[dict]) -> None:
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(antennas, f, ensure_ascii=False, indent=2)


def _normalize(data: dict, existing: dict | None = None) -> dict:
    base = dict(existing) if existing else dict(_DEFAULTS)
    for field, default in _DEFAULTS.items():
        if field not in data:
            continue
        value = data[field]
        if field == "port":
            value = int(value) if str(value).strip() else 80
        elif field in ("https", "verify_tls", "cockpit_enabled"):
            value = bool(value)
        elif field == "vendor":
            value = value if value in VENDORS else "controlid"
        elif field == "gate_output":
            value = value if value in GATE_OUTPUTS else "contact"
        elif field == "door_id":
            value = int(value) if str(value).strip() else 1
        elif field == "secbox_id":
            value = int(value) if str(value).strip() else ""
        elif field == "group_id":
            value = int(value) if str(value).strip() else ""
        else:
            value = str(value).strip()
        base[field] = value
    return base


def list_antennas() -> list[dict]:
    return _read_all()


def get_antenna(antenna_id: str) -> dict | None:
    return next((a for a in _read_all() if a.get("id") == antenna_id), None)


def create_antenna(data: dict) -> dict:
    antennas = _read_all()
    antenna = _normalize(data)
    antenna["id"] = uuid.uuid4().hex[:8]
    antennas.append(antenna)
    _write_all(antennas)
    return antenna


def update_antenna(antenna_id: str, data: dict) -> dict | None:
    antennas = _read_all()
    for i, a in enumerate(antennas):
        if a.get("id") == antenna_id:
            # Não sobrescreve a senha com o placeholder mascarado que o front devolve.
            if data.get("password") == "********":
                data = {k: v for k, v in data.items() if k != "password"}
            antennas[i] = _normalize(data, existing=a)
            _write_all(antennas)
            return antennas[i]
    return None


def delete_antenna(antenna_id: str) -> bool:
    antennas = _read_all()
    remaining = [a for a in antennas if a.get("id") != antenna_id]
    if len(remaining) == len(antennas):
        return False
    _write_all(remaining)
    return True


def for_display(antenna: dict) -> dict:
    """Cópia com a senha mascarada, pra mandar pro frontend."""
    out = dict(antenna)
    if out.get("password"):
        out["password"] = "********"
    return out
