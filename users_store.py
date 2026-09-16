"""
Persistência local (JSON) dos usuários deste zapy além do admin de bootstrap
(ZAPY_ADMIN_USER/ZAPY_ADMIN_PASSWORD no .env) — pensado pro porteiro do local: um
login próprio, sem precisar da senha de admin, que só abre portas/terminais e vê
eventos (ver ROLES e role_required em app.py). Mesmo espírito de
face_terminals_store.py: poucos registros, editados raramente, só por este painel.
"""
import json
import os
import uuid

from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(BASE_DIR, "users.json")

ROLES = ("admin", "porteiro")


def _read_all() -> list[dict]:
    if not os.path.isfile(STORE_PATH):
        return []
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def _write_all(users: list[dict]) -> None:
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def list_users() -> list[dict]:
    return _read_all()


def get_user(user_id: str) -> dict | None:
    return next((u for u in _read_all() if u.get("id") == user_id), None)


def find_by_username(username: str) -> dict | None:
    username = (username or "").strip().lower()
    return next((u for u in _read_all() if u.get("username", "").lower() == username), None)


def verify_password(user: dict, password: str) -> bool:
    return check_password_hash(user.get("password_hash", ""), password or "")


def create_user(data: dict) -> dict:
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ROLES else "porteiro"
    if not username or not password:
        raise ValueError("username e password são obrigatórios")
    if find_by_username(username):
        raise ValueError("já existe um usuário com esse username")

    users = _read_all()
    user = {
        "id": uuid.uuid4().hex[:8],
        "username": username,
        "name": (data.get("name") or username).strip(),
        "role": role,
        "password_hash": generate_password_hash(password),
        "active": True,
    }
    users.append(user)
    _write_all(users)
    return user


def update_user(user_id: str, data: dict) -> dict | None:
    users = _read_all()
    for i, u in enumerate(users):
        if u.get("id") != user_id:
            continue
        if "name" in data and data["name"]:
            u["name"] = str(data["name"]).strip()
        if "role" in data and data["role"] in ROLES:
            u["role"] = data["role"]
        if "active" in data:
            u["active"] = bool(data["active"])
        if data.get("password"):
            u["password_hash"] = generate_password_hash(data["password"])
        users[i] = u
        _write_all(users)
        return u
    return None


def delete_user(user_id: str) -> bool:
    users = _read_all()
    remaining = [u for u in users if u.get("id") != user_id]
    if len(remaining) == len(users):
        return False
    _write_all(remaining)
    return True


def for_display(user: dict) -> dict:
    """Cópia sem o hash da senha, pra mandar pro frontend."""
    out = dict(user)
    out.pop("password_hash", None)
    return out
