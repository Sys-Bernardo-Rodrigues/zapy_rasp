"""
Self-check do face_agent — sem terminal físico. Mocka `requests` pra validar o
protocolo (envelope Intelbras, branches create/update, XML sem format=json no
Hikvision, circuit breaker de auth). Rodar: python -m unittest test_face_agent -v
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import requests

import face_terminals_store
from face_agent.errors import FaceProvisioningError
from face_agent.events_poller import (
    EventsPoller,
    _find_field_ci,
    _normalize_event,
    fetch_hikvision_events_since,
    fetch_intelbras_events_since,
)
from face_agent.face_client_factory import create_face_client
from face_agent.hikvision_client import MAX_CONSEC_AUTH_FAILURES, HikvisionClient, HikvisionTerminal
from face_agent.intelbras_client import IntelbrasClient, IntelbrasTerminal
from face_agent.local_store import LocalStore, PollCursor, RosterEntry
from face_agent.schedule_enforcer import enforce_schedule, is_within_schedule

JPEG = b"\xff\xd8" + b"x" * 10 + b"\xff\xd9"


class IntelbrasClientTest(unittest.TestCase):
    def _client(self, **kwargs):
        return IntelbrasClient(IntelbrasTerminal(host="10.0.0.1", port=80, username="admin", password="pw", **kwargs))

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_create(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "add", "message": "OK"})
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["target"], "user")
        self.assertEqual(sent["action"], "add")
        self.assertEqual(sent["data"]["item"][0]["UserID"], "123")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_update_when_already_exists(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": -1, "message": "User already exist"}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 3)

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.intelbras_client.requests.post")
    def test_open_door_never_raises(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("timeout")
        result = self._client().open_door()
        self.assertEqual(result, {"ok": False, "reason": "falha de rede ao chamar 10.0.0.1: timeout"})

    @patch("face_agent.intelbras_client.requests.post")
    def test_open_door_uses_relay_level(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0})
        self._client(relay_level=1).open_door()
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["level"], 1)


class HikvisionClientTest(unittest.TestCase):
    def _client(self):
        return HikvisionClient(HikvisionTerminal(host="10.0.0.2", port=80, username="admin", password="pw"))

    @patch("face_agent.hikvision_client.requests.request")
    def test_open_door_sends_xml_without_format_json(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200)
        result = self._client().open_door()
        self.assertEqual(result, {"ok": True})
        _, kwargs = mock_request.call_args
        self.assertNotIn("format", kwargs["params"])
        self.assertIn(b"<cmd>open</cmd>", kwargs["data"])

    @patch("face_agent.hikvision_client.requests.request")
    def test_json_request_adds_format_json(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {})
        self._client().request("POST", "AccessControl/UserInfo/Record", json_body={"a": 1})
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["params"]["format"], "json")

    @patch("face_agent.hikvision_client.requests.request")
    def test_enroll_create_flow(self, mock_request):
        mock_request.side_effect = [
            MagicMock(status_code=200),  # UserInfo/Record
            MagicMock(status_code=200),  # FaceDataRecord
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        self.assertEqual(mock_request.call_count, 2)

    @patch("face_agent.hikvision_client.requests.request")
    def test_circuit_breaker_opens_after_consecutive_auth_failures(self, mock_request):
        mock_request.return_value = MagicMock(status_code=401)
        client = self._client()
        for _ in range(MAX_CONSEC_AUTH_FAILURES):
            with self.assertRaises(FaceProvisioningError):
                client.request("GET", "System/status")
        with self.assertRaises(FaceProvisioningError) as ctx:
            client.request("GET", "System/status")
        self.assertIn("circuit breaker aberto", str(ctx.exception))

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.hikvision_client.requests.request")
    def test_check_health_parses_json(self, mock_request):
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {"DeviceInfo": {"model": "DS-K1T671MF-L"}})
        info = self._client().check_health()
        self.assertEqual(info["model"], "DS-K1T671MF-L")

    @patch("face_agent.hikvision_client.requests.request")
    def test_check_health_falls_back_to_xml_when_content_type_lies(self, mock_request):
        # VALIDADO AO VIVO: System/deviceInfo do DS-K1T671MF-L alega Content-Type: application/json
        # mas manda XML de verdade no corpo.
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<DeviceInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            "<model>DS-K1T671MF-L</model><deviceName>Access Controller</deviceName>"
            "</DeviceInfo>"
        )
        response = MagicMock(status_code=200, text=xml)
        response.json.side_effect = ValueError("not json")
        mock_request.return_value = response
        info = self._client().check_health()
        self.assertEqual(info, {"model": "DS-K1T671MF-L", "deviceName": "Access Controller"})


class FaceClientFactoryTest(unittest.TestCase):
    def test_unknown_vendor_raises(self):
        with self.assertRaises(ValueError):
            create_face_client("acme", host="x", port=80, username="a", password="b")

    def test_picks_class_by_vendor(self):
        hik = create_face_client("hikvision", host="x", port=80, username="a", password="b")
        intel = create_face_client("intelbras", host="x", port=80, username="a", password="b")
        self.assertIsInstance(hik, HikvisionClient)
        self.assertIsInstance(intel, IntelbrasClient)


class FaceTerminalsStoreTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)  # começa sem arquivo, igual ao primeiro uso real
        self._patcher = patch.object(face_terminals_store, "STORE_PATH", path)
        self._patcher.start()
        self._path = path

    def tearDown(self):
        self._patcher.stop()
        if os.path.exists(self._path):
            os.remove(self._path)

    def test_create_list_update_delete_roundtrip(self):
        created = face_terminals_store.create_terminal(
            {"name": "Catraca", "vendor": "hikvision", "host": "192.168.1.100", "port": 80, "username": "admin", "password": "segredo"}
        )
        self.assertTrue(created["id"])
        self.assertEqual(face_terminals_store.list_terminals(), [created])

        updated = face_terminals_store.update_terminal(created["id"], {"name": "Catraca Entrada", "password": "novo_segredo"})
        self.assertEqual(updated["name"], "Catraca Entrada")
        self.assertEqual(updated["password"], "novo_segredo")
        self.assertEqual(updated["host"], "192.168.1.100")  # campo não enviado no update permanece

        self.assertTrue(face_terminals_store.delete_terminal(created["id"]))
        self.assertEqual(face_terminals_store.list_terminals(), [])

    def test_update_with_masked_password_placeholder_keeps_existing_password(self):
        created = face_terminals_store.create_terminal({"name": "X", "host": "h", "username": "u", "password": "segredo_real"})
        updated = face_terminals_store.update_terminal(created["id"], {"name": "X2", "password": "********"})
        self.assertEqual(updated["password"], "segredo_real")

    def test_for_display_masks_password(self):
        terminal = {"password": "segredo"}
        self.assertEqual(face_terminals_store.for_display(terminal)["password"], "********")

    def test_invalid_vendor_falls_back_to_hikvision(self):
        created = face_terminals_store.create_terminal({"name": "X", "vendor": "acme", "host": "h", "username": "u", "password": "p"})
        self.assertEqual(created["vendor"], "hikvision")


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.remove(path)  # começa sem arquivo, igual ao primeiro uso real
        self.store = LocalStore(path)
        self._path = path

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self._path + suffix):
                os.remove(self._path + suffix)

    def test_roster_upsert_list_set_enrolled_remove(self):
        self.store.upsert_roster("t1", "123", "Fulano", JPEG, {"enabled": True, "startTime": "08:00", "endTime": "18:00"})
        entries = self.store.list_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Fulano")
        self.assertFalse(entries[0].enrolled)

        self.store.set_enrolled("t1", "123", True)
        self.assertTrue(self.store.list_by_terminal("t1")[0].enrolled)

        # upsert de novo não duplica, só atualiza
        self.store.upsert_roster("t1", "123", "Fulano Silva", JPEG, None)
        entries = self.store.list_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Fulano Silva")
        self.assertIsNone(entries[0].access_schedule)

        self.store.remove_roster("t1", "123")
        self.assertEqual(self.store.list_by_terminal("t1"), [])

    def test_cursor_get_set_roundtrip(self):
        self.assertIsNone(self.store.get_cursor("t1"))
        self.store.set_cursor(PollCursor(terminal_id="t1", last_event_time="2026-01-01T00:00:00-03:00", search_id="42"))
        cursor = self.store.get_cursor("t1")
        self.assertEqual(cursor.last_event_time, "2026-01-01T00:00:00-03:00")
        self.assertEqual(cursor.search_id, "42")

        self.store.set_cursor(PollCursor(terminal_id="t1", last_event_time="2026-01-02T00:00:00-03:00", search_id="43"))
        self.assertEqual(self.store.get_cursor("t1").search_id, "43")


class ScheduleEnforcerTest(unittest.TestCase):
    def test_disabled_schedule_always_allows(self):
        self.assertTrue(is_within_schedule(None))
        self.assertTrue(is_within_schedule({"enabled": False}))

    def test_blocks_outside_weekday(self):
        schedule = {
            "enabled": True, "startTime": "00:00", "endTime": "23:59",
            "weekdays": {"mon": False, "tue": True, "wed": True, "thu": True, "fri": True, "sat": True, "sun": True},
        }
        monday = datetime(2026, 1, 5, 12, 0)  # 2026-01-05 é segunda-feira
        self.assertFalse(is_within_schedule(schedule, monday))

    def test_normal_window(self):
        schedule = {
            "enabled": True, "startTime": "08:00", "endTime": "18:00",
            "weekdays": {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        }
        self.assertTrue(is_within_schedule(schedule, datetime(2026, 1, 5, 12, 0)))
        self.assertFalse(is_within_schedule(schedule, datetime(2026, 1, 5, 19, 0)))

    def test_overnight_window_wraps_midnight(self):
        schedule = {
            "enabled": True, "startTime": "22:00", "endTime": "06:00",
            "weekdays": {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        }
        self.assertTrue(is_within_schedule(schedule, datetime(2026, 1, 5, 23, 0)))
        self.assertTrue(is_within_schedule(schedule, datetime(2026, 1, 5, 3, 0)))
        self.assertFalse(is_within_schedule(schedule, datetime(2026, 1, 5, 12, 0)))

    def test_enforce_schedule_enrolls_and_removes(self):
        store = MagicMock()
        client = MagicMock()
        schedule_day = {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        entry_should_enroll = RosterEntry(
            terminal_id="t1", employee_no="1", name="A", jpeg=JPEG, enrolled=False,
            access_schedule={"enabled": True, "startTime": "00:00", "endTime": "23:59", "weekdays": schedule_day},
        )
        entry_should_remove = RosterEntry(
            terminal_id="t1", employee_no="2", name="B", jpeg=JPEG, enrolled=True,
            access_schedule={"enabled": True, "startTime": "00:00", "endTime": "00:01", "weekdays": schedule_day},
        )
        store.list_by_terminal.return_value = [entry_should_enroll, entry_should_remove]

        result = enforce_schedule(client, store, "t1", "Terminal 1", now=datetime(2026, 1, 5, 12, 0))

        self.assertEqual(result["enrolled"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 0)
        client.enroll_face.assert_called_once_with("1", "A", JPEG)
        client.delete_user_info.assert_called_once_with("2")
        store.set_enrolled.assert_any_call("t1", "1", True)
        store.set_enrolled.assert_any_call("t1", "2", False)

    def test_enforce_schedule_one_failure_does_not_block_others(self):
        store = MagicMock()
        client = MagicMock()
        client.enroll_face.side_effect = [Exception("terminal offline"), None]
        schedule_always = {"enabled": True, "startTime": "00:00", "endTime": "23:59", "weekdays": {d: True for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}}
        entries = [
            RosterEntry(terminal_id="t1", employee_no="1", name="A", jpeg=JPEG, enrolled=False, access_schedule=schedule_always),
            RosterEntry(terminal_id="t1", employee_no="2", name="B", jpeg=JPEG, enrolled=False, access_schedule=schedule_always),
        ]
        store.list_by_terminal.return_value = entries

        result = enforce_schedule(client, store, "t1", "Terminal 1", now=datetime(2026, 1, 5, 12, 0))

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["enrolled"], 1)
        self.assertEqual(client.enroll_face.call_count, 2)


class EventsPollerFieldTest(unittest.TestCase):
    def test_find_field_ci_shallow_wins_over_deep(self):
        obj = {"Major": 5, "nested": {"major": 99}}
        self.assertEqual(_find_field_ci(obj, "major"), 5)

    def test_find_field_ci_missing_returns_none(self):
        self.assertIsNone(_find_field_ci({"a": 1}, "b"))

    def test_normalize_event_inactive_state_is_none(self):
        self.assertIsNone(_normalize_event("t1", {"eventState": "inactive"}, "poll"))

    def test_normalize_event_success_minor_code(self):
        event = _normalize_event("t1", {"major": 5, "minor": 75, "employeeNo": "123", "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertTrue(event["success"])
        self.assertEqual(event["employee_no"], "123")

    def test_normalize_event_non_success_minor_code(self):
        event = _normalize_event("t1", {"major": 5, "minor": 76, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertFalse(event["success"])


class HikvisionEventsFetchTest(unittest.TestCase):
    @patch("face_agent.events_poller.get_device_time")
    def test_fetch_paginates_with_more_token(self, mock_device_time):
        mock_device_time.return_value = "2026-01-01T12:00:00-03:00"
        client = MagicMock()
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "AcsEvent": {
                "responseStatusStrg": "MORE",
                "InfoList": [{"major": 5, "minor": 75, "employeeNo": "1", "dateTime": "2026-01-01T11:00:00-03:00"}],
            }
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "AcsEvent": {
                "responseStatusStrg": "OK",
                "InfoList": [{"major": 5, "minor": 75, "employeeNo": "2", "dateTime": "2026-01-01T11:05:00-03:00"}],
            }
        }
        client.request.side_effect = [page1, page2]

        events, next_cursor = fetch_hikvision_events_since(client, "t1", PollCursor(terminal_id="t1"))

        self.assertEqual(len(events), 2)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(next_cursor.last_event_time, "2026-01-01T11:05:00-03:00")

    @patch("face_agent.events_poller.get_device_time")
    def test_fetch_skips_events_at_or_before_cursor(self, mock_device_time):
        mock_device_time.return_value = "2026-01-01T12:00:00-03:00"
        client = MagicMock()
        res = MagicMock(status_code=200)
        res.json.return_value = {
            "AcsEvent": {
                "responseStatusStrg": "OK",
                "InfoList": [{"major": 5, "minor": 75, "employeeNo": "1", "dateTime": "2026-01-01T10:00:00-03:00"}],
            }
        }
        client.request.return_value = res

        cursor = PollCursor(terminal_id="t1", last_event_time="2026-01-01T10:00:00-03:00")
        events, next_cursor = fetch_hikvision_events_since(client, "t1", cursor)

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.last_event_time, "2026-01-01T12:00:00-03:00")


class IntelbrasEventsFetchTest(unittest.TestCase):
    def test_first_cycle_anchors_cursor_without_events(self):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [{"ID": "10"}, {"ID": "15"}]}}

        events, next_cursor = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1"))

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.search_id, "15")

    def test_returns_only_new_items_and_maps_desconhecido_to_none(self):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "123"},
            {"ID": "11", "Date": "2026-01-01", "Time": "10:05:00", "Status": "Fail", "UserID": "Desconhecido"},
        ]}}

        events, next_cursor = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["employee_no"], "123")
        self.assertIsNone(events[1]["employee_no"])
        self.assertEqual(next_cursor.search_id, "11")

    def test_ignores_items_already_seen(self):
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "123"},
        ]}}
        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="10"))
        self.assertEqual(events, [])


class EventsPollerTest(unittest.TestCase):
    def test_poll_once_persists_cursor(self):
        store = MagicMock()
        store.get_cursor.return_value = None
        fake_events = [{"dedupe_key": "x"}]
        fake_cursor = PollCursor(terminal_id="t1", search_id="1")
        fetch = MagicMock(return_value=(fake_events, fake_cursor))

        poller = EventsPoller(store, "t1", fetch)
        events = poller.poll_once()

        self.assertEqual(events, fake_events)
        store.set_cursor.assert_called_once_with(fake_cursor)


if __name__ == "__main__":
    unittest.main()
