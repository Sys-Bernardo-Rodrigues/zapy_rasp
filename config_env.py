"""
Leitura e gravação das variáveis de ambiente do Zapy no arquivo .env.
Apenas chaves permitidas são lidas/escritas.
"""
import os
import re

# Diretório do projeto (onde fica app.py e .env)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

# Chaves que o painel pode ler e escrever
ALLOWED_KEYS = frozenset({
    "ZACCESS_SERVER_URL",
    "ZACCESS_DEVICE_SERIAL",
    "ZACCESS_DEVICE_TOKEN",
    "PORT",
    "PULSE_RELE_1",
    "PULSE_RELE_2",
    "PULSE_RELE_3",
    "PULSE_RELE_4",
    # IDs de relé (CSV, ex.: "1,3,4") liberados pro porteiro abrir no /cockpit —
    # vazio/ausente = todos liberados (não restringe nada até o admin configurar).
    "COCKPIT_RELAYS",
})


def _parse_env_lines(lines: list[str]) -> dict[str, str]:
    """Converte linhas KEY=VALUE em dicionário. Mantém apenas chaves permitidas."""
    result = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and m.group(1) in ALLOWED_KEYS:
            result[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return result


def _format_line(key: str, val: str) -> str:
    if " " in val or "#" in val or "\n" in val:
        val = f'"{val}"'
    return f"{key}={val}"


def read_config() -> dict[str, str]:
    """Lê configuração atual do .env (apenas chaves permitidas)."""
    if not os.path.isfile(ENV_PATH):
        return {k: "" for k in ALLOWED_KEYS}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        data = _parse_env_lines(f.readlines())
    # Garantir todas as chaves presentes
    result = {k: "" for k in ALLOWED_KEYS}
    result.update(data)
    return result


def write_config(data: dict[str, str]) -> None:
    """Atualiza só as chaves permitidas presentes em `data`, em cima do .env existente —
    preserva comentários e qualquer outra linha (ex.: GPIOZERO_PIN_FACTORY) intocada, em
    vez de reescrever o arquivo do zero só com ALLOWED_KEYS."""
    updates: dict[str, str] = {}
    for k in ALLOWED_KEYS:
        if k not in data:
            continue
        v = (data[k] or "").strip()
        # Não sobrescrever token quando o front envia o placeholder (mantém o valor atual)
        if k == "ZACCESS_DEVICE_TOKEN" and v == "********":
            continue
        updates[k] = v

    existing_lines: list[str] = []
    if os.path.isfile(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    seen: set[str] = set()
    out_lines: list[str] = []
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", stripped) if stripped and not stripped.startswith("#") else None
        if m and m.group(1) in updates:
            key = m.group(1)
            out_lines.append(_format_line(key, updates[key]))
            seen.add(key)
        else:
            out_lines.append(raw_line.rstrip("\n"))

    for key, val in updates.items():
        if key not in seen:
            out_lines.append(_format_line(key, val))

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")


def get_config_for_display() -> dict[str, str]:
    """Retorna config para exibição no frontend (token mascarado)."""
    cfg = read_config()
    if cfg.get("ZACCESS_DEVICE_TOKEN"):
        cfg["ZACCESS_DEVICE_TOKEN"] = "********"
    return cfg
