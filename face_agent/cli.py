"""
CLI manual para testar o face_agent contra um terminal facial real, sem
precisar do ZAccess rodando ainda (Fase 1 do plano de integração facial:
zapy fala com o terminal antes de qualquer fiação de protocolo com o servidor).

Uso:
  python -m face_agent.cli enroll --vendor hikvision --host 192.168.1.100 \\
      --user admin --password senha --employee-no 123 --name "Fulano" --photo rosto.jpg
  python -m face_agent.cli revoke    --vendor intelbras --host 192.168.1.101 --user admin --password senha --employee-no 123
  python -m face_agent.cli open-door --vendor hikvision --host 192.168.1.100 --user admin --password senha
  python -m face_agent.cli reboot    --vendor intelbras --host 192.168.1.101 --user admin --password senha
  python -m face_agent.cli health    --vendor intelbras --host 192.168.1.101 --user admin --password senha
  python -m face_agent.cli enroll-card --vendor hikvision --host 192.168.1.100 \\
      --user admin --password senha --employee-no 123 --name "Fulano" --card-no AAABBB
  python -m face_agent.cli revoke-card --vendor hikvision --host 192.168.1.100 --user admin --password senha --employee-no 123
  python -m face_agent.cli clear-all --vendor intelbras_biot --host 192.168.1.101 --user admin --password senha
"""
import argparse
import logging
import sys

from .face_client_factory import create_face_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _add_terminal_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--vendor", required=True, choices=["hikvision", "intelbras", "intelbras_biot"])
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=None, help="padrão: 443 com --https, senão 80")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--https", action="store_true")
    p.add_argument("--insecure", action="store_true", help="não valida certificado TLS (só em rede local confiável)")
    p.add_argument("--relay-level", type=int, default=0, choices=[0, 1], help="Intelbras: NO-COM(0)/NC-COM(1), depende da fiação")


def _client_from_args(args: argparse.Namespace):
    port = args.port if args.port is not None else (443 if args.https else 80)
    return create_face_client(
        args.vendor, host=args.host, port=port, username=args.user, password=args.password,
        https=args.https, verify_tls=not args.insecure, relay_level=args.relay_level,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Teste manual do face_agent contra um terminal facial real.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_enroll = sub.add_parser("enroll", help="cadastra/atualiza um rosto no terminal")
    _add_terminal_args(p_enroll)
    p_enroll.add_argument("--employee-no", required=True, help="chave externa estável da pessoa")
    p_enroll.add_argument("--name", required=True)
    p_enroll.add_argument("--photo", required=True, help="caminho do JPEG do rosto (frontal, único, <=200KB)")

    p_revoke = sub.add_parser("revoke", help="remove um rosto do terminal")
    _add_terminal_args(p_revoke)
    p_revoke.add_argument("--employee-no", required=True)

    p_enroll_card = sub.add_parser("enroll-card", help="cadastra/atualiza um cartão no terminal")
    _add_terminal_args(p_enroll_card)
    p_enroll_card.add_argument("--employee-no", required=True, help="chave externa estável da pessoa")
    p_enroll_card.add_argument("--name", required=True)
    p_enroll_card.add_argument("--card-no", required=True, help="código do cartão (hexadecimal)")

    p_revoke_card = sub.add_parser("revoke-card", help="remove um cartão do terminal")
    _add_terminal_args(p_revoke_card)
    p_revoke_card.add_argument("--employee-no", required=True)
    p_revoke_card.add_argument("--card-no", default=None, help="ignorado por alguns vendors (XPE/Hikvision removem por employee-no)")

    p_open = sub.add_parser("open-door", help="abre a porta/catraca remotamente (sem reconhecimento facial)")
    _add_terminal_args(p_open)

    p_reboot = sub.add_parser("reboot", help="reinicia o terminal fisicamente")
    _add_terminal_args(p_reboot)

    p_health = sub.add_parser("health", help="checa se o terminal está respondendo")
    _add_terminal_args(p_health)

    p_clear = sub.add_parser("clear-all", help="apaga TODOS os usuários/faces/cartões do terminal — destrutivo e irreversível")
    _add_terminal_args(p_clear)
    p_clear.add_argument("--yes", action="store_true", help="pula a confirmação interativa (uso em script)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = _client_from_args(args)

    try:
        if args.command == "enroll":
            with open(args.photo, "rb") as f:
                jpeg = f.read()
            status = client.enroll_face(args.employee_no, args.name, jpeg)
            print(f"OK: {status}")
            return 0

        if args.command == "revoke":
            client.delete_user_info(args.employee_no)
            print("OK: removido")
            return 0

        if args.command == "enroll-card":
            status = client.enroll_card(args.employee_no, args.name, args.card_no)
            print(f"OK: {status}")
            return 0

        if args.command == "revoke-card":
            client.delete_card(args.employee_no, args.card_no)
            print("OK: cartão removido")
            return 0

        if args.command == "open-door":
            result = client.open_door()
            print(result)
            return 0 if result.get("ok") else 1

        if args.command == "reboot":
            result = client.reboot_terminal()
            print(result)
            return 0 if result.get("ok") else 1

        if args.command == "health":
            if not hasattr(client, "check_health"):
                print("ERRO: health check não implementado para esse vendor")
                return 1
            print(client.check_health())
            return 0

        if args.command == "clear-all":
            if not args.yes:
                answer = input(
                    f"Isso vai apagar TODOS os usuários, faces e cartões do terminal "
                    f"{args.vendor}@{args.host}. Não tem volta. Digite 'sim' para confirmar: "
                )
                if answer.strip().lower() != "sim":
                    print("Cancelado.")
                    return 1
            result = client.clear_all_users()
            if isinstance(result, int):
                print(f"OK: {result} usuário(s) removido(s)")
            else:
                print("OK: terminal zerado")
            return 0
    except Exception as e:
        print(f"ERRO: {e}")
        return 1

    return 2  # comando desconhecido (não deveria chegar aqui, argparse já valida choices)


if __name__ == "__main__":
    sys.exit(main())
