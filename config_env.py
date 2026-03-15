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


def _serialize_env(data: dict[str, str]) -> str:
    """Gera conteúdo de arquivo .env (apenas chaves permitidas)."""
    lines = []
    order = [
        "ZACCESS_SERVER_URL", "ZACCESS_DEVICE_SERIAL", "ZACCESS_DEVICE_TOKEN", "PORT",
        "PULSE_RELE_1", "PULSE_RELE_2", "PULSE_RELE_3", "PULSE_RELE_4",
    ]
    for key in order:
        if key in data and data[key] is not None:
            val = str(data[key]).strip()
            if " " in val or "#" in val or "\n" in val:
                val = f'"{val}"'
            lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


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
    """Escreve no .env apenas chaves permitidas. Não sobrescreve token com placeholder."""
    current = read_config()
    for k in ALLOWED_KEYS:
        if k not in data:
            continue
        v = (data[k] or "").strip()
        # Não sobrescrever token quando o front envia o placeholder (mantém o valor atual)
        if k == "ZACCESS_DEVICE_TOKEN" and v == "********":
            continue
        current[k] = v
    content = _serialize_env(current)
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def get_config_for_display() -> dict[str, str]:
    """Retorna config para exibição no frontend (token mascarado)."""
    cfg = read_config()
    if cfg.get("ZACCESS_DEVICE_TOKEN"):
        cfg["ZACCESS_DEVICE_TOKEN"] = "********"
    return cfg
