"""
Persistência local (JSON) dos terminais faciais configurados neste zapy — cada
registro aqui é um terminal Hikvision ou Intelbras real na rede local, no
mesmo espírito do que o agente-local do z-edu guarda por escola. JSON e não
SQLite de propósito: poucos registros, editados raramente, só por este painel.
"""
import json
import os
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(BASE_DIR, "face_terminals.json")

VENDORS = ("hikvision", "intelbras", "intelbras_biot", "controlid")
_DEFAULTS = {
    "name": "",
    "vendor": "hikvision",
    "host": "",
    "port": 80,
    "username": "",
    "password": "",
    "https": False,
    "verify_tls": True,
    "relay_level": 0,  # Intelbras: NO-COM(0)/NC-COM(1), depende da fiação da instalação
    "cockpit_enabled": True,  # porteiro pode abrir esse terminal em /cockpit
    # Control iD: grupo/departamento ao qual todo usuário criado por aqui é associado — sem
    # isso o usuário fica sem regra de acesso nenhuma (validado ao vivo). "" = não associa.
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


def _write_all(terminals: list[dict]) -> None:
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(terminals, f, ensure_ascii=False, indent=2)


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
        elif field == "relay_level":
            value = 1 if int(value or 0) == 1 else 0
        elif field == "vendor":
            value = value if value in VENDORS else "hikvision"
        elif field == "group_id":
            value = int(value) if str(value).strip() else ""
        else:
            value = str(value).strip()
        base[field] = value
    return base


def list_terminals() -> list[dict]:
    return _read_all()


def get_terminal(terminal_id: str) -> dict | None:
    return next((t for t in _read_all() if t.get("id") == terminal_id), None)


def create_terminal(data: dict) -> dict:
    terminals = _read_all()
    terminal = _normalize(data)
    terminal["id"] = uuid.uuid4().hex[:8]
    terminals.append(terminal)
    _write_all(terminals)
    return terminal


def update_terminal(terminal_id: str, data: dict) -> dict | None:
    terminals = _read_all()
    for i, t in enumerate(terminals):
        if t.get("id") == terminal_id:
            # Não sobrescreve a senha com o placeholder mascarado que o front devolve.
            if data.get("password") == "********":
                data = {k: v for k, v in data.items() if k != "password"}
            terminals[i] = _normalize(data, existing=t)
            _write_all(terminals)
            return terminals[i]
    return None


def delete_terminal(terminal_id: str) -> bool:
    terminals = _read_all()
    remaining = [t for t in terminals if t.get("id") != terminal_id]
    if len(remaining) == len(terminals):
        return False
    _write_all(remaining)
    return True


def for_display(terminal: dict) -> dict:
    """Cópia com a senha mascarada, pra mandar pro frontend."""
    out = dict(terminal)
    if out.get("password"):
        out["password"] = "********"
    return out
