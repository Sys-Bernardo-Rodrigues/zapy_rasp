"""
Decide, a cada ciclo, se cada pessoa do roster local deveria estar com a face
liberada no terminal AGORA, de acordo com o `access_schedule` dela — funciona
com ou sem a nuvem no ar, porque só depende do LocalStore. Porta de
agente-local/src/enrollment/scheduleEnforcer.ts.
"""
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# datetime.weekday(): mon=0 ... sun=6. Mesmas chaves do faceAccessSchedule.weekdays
# vindo do servidor (ver Projeto-ZAccess/server/src/models/LocationUser.js).
_WEEKDAY_BY_PY_DAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_within_schedule(schedule: Optional[dict], now: Optional[datetime] = None) -> bool:
    """`enabled: False`/`None` sempre libera. Dia fora de `weekdays` bloqueia o dia
    inteiro. Janela `startTime > endTime` vira virada de madrugada (ex 22:00-06:00):
    dentro se a hora atual está ANTES do fim OU DEPOIS do início."""
    if not schedule or not schedule.get("enabled"):
        return True

    now = now or datetime.now()
    weekday = _WEEKDAY_BY_PY_DAY[now.weekday()]
    if not (schedule.get("weekdays") or {}).get(weekday):
        return False

    now_minutes = now.hour * 60 + now.minute
    start = _to_minutes(schedule["startTime"])
    end = _to_minutes(schedule["endTime"])
    if start <= end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def enforce_schedule(client, store, terminal_id: str, terminal_name: str, now: Optional[datetime] = None) -> dict:
    """Passa pelo roster local do terminal e corrige o estado real (enrolado/
    removido) pra bater com o horário permitido agora. Só mexe em quem tem
    horário restrito E cujo estado diverge do esperado; erro de uma pessoa não
    trava as outras."""
    entries = store.list_by_terminal(terminal_id)
    restricted = [e for e in entries if e.access_schedule and e.access_schedule.get("enabled")]

    result = {"enrolled": 0, "removed": 0, "failed": 0, "restricted_count": len(restricted)}

    for entry in restricted:
        should_be_enrolled = is_within_schedule(entry.access_schedule, now)
        if should_be_enrolled == entry.enrolled:
            continue

        ctx = {"terminal_id": terminal_id, "terminal_name": terminal_name, "employee_no": entry.employee_no, "name": entry.name}
        try:
            if should_be_enrolled:
                client.enroll_face(entry.employee_no, entry.name, entry.jpeg)
                store.set_enrolled(terminal_id, entry.employee_no, True)
                result["enrolled"] += 1
                logger.info("accessSchedule: dentro da janela permitida, face liberada no terminal - %s", ctx)
            else:
                client.delete_user_info(entry.employee_no)
                store.set_enrolled(terminal_id, entry.employee_no, False)
                result["removed"] += 1
                logger.info("accessSchedule: fora da janela permitida, face removida do terminal - %s", ctx)
        except Exception:
            result["failed"] += 1
            logger.exception("accessSchedule: falha ao aplicar mudança de horário (%s) - tenta de novo no próximo ciclo", ctx)

    return result
