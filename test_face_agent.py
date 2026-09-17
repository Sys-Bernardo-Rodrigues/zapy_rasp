"""
Self-check do face_agent — sem terminal físico. Mocka `requests` pra validar o
protocolo (envelope Intelbras, branches create/update, XML sem format=json no
Hikvision, circuit breaker de auth). Rodar: python -m unittest test_face_agent -v
"""
import base64
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
    fetch_intelbras_biot_events_since,
    fetch_intelbras_events_since,
)
from face_agent.face_client_factory import create_face_client
from face_agent.hikvision_client import MAX_CONSEC_AUTH_FAILURES, HikvisionClient, HikvisionTerminal
from face_agent.intelbras_biot_client import IntelbrasBioTClient, IntelbrasBioTTerminal
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
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123"}]}}),  # get: já existe
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),  # set
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 2)

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_update_preserves_existing_card(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "CardCode": "AAABBB"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().enroll_face("123", "Fulano", JPEG)
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "AAABBB")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_falls_back_to_set_when_add_races(self, mock_post):
        # get inicial não acha ninguém, mas o add falha com "já existe" (outra chamada
        # criou o usuário nesse meio-tempo) — cai pro get+set.
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}}),
            MagicMock(status_code=200, json=lambda: {"retcode": -1, "message": "User already exist"}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 9, "UserID": "123"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 4)

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_create(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "add", "message": "OK"}),
        ]
        status = self._client().enroll_card("123", "Fulano", "1")
        self.assertEqual(status, "created")
        sent = mock_post.call_args.kwargs["json"]
        # decimal 1 -> hex "00000001" -> bytes revertidos pelo XPE3200 -> "01000000".
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_reverses_bytes_for_xpe3200(self, mock_post):
        # Caso real: cartão decimal 2422164881 = hex "padrão" 905F4D91, mas a leitora
        # embutida do XPE3200 lê/grava 914D5F90 (bytes revertidos).
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "add", "message": "OK"}),
        ]
        self._client().enroll_card("123", "Fulano", "2422164881")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "914D5F90")

    def test_enroll_card_rejects_non_decimal(self):
        # ZAccess sempre manda decimal agora — um valor não numérico é erro de input, não
        # deve virar hex "por acidente".
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "905F4D91")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_preserves_existing_face(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "FaceImage": "existingb64"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        status = self._client().enroll_card("123", "Fulano", "1")
        self.assertEqual(status, "updated")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["FaceImage"], "existingb64")
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_appends_to_existing_cards(self, mock_post):
        # Pessoa já tem o cartão decimal 1 (gravado como "01000000") — cadastrar o cartão
        # decimal 2 (gravado como "02000000") não pode sobrescrever, só somar.
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "CardCode": "01000000"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().enroll_card("123", "Fulano", "2")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000,02000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_enroll_card_idempotent_when_already_present(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [{"ID": 7, "UserID": "123", "CardCode": "01000000"}]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().enroll_card("123", "Fulano", "1")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "01000000")

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    @patch("face_agent.intelbras_client.requests.post")
    def test_delete_card_clears_field_keeps_face(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
                {"ID": 7, "UserID": "123", "Name": "Fulano", "FaceImage": "existingb64", "CardCode": "01000000"},
            ]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
                {"ID": 7, "UserID": "123", "Name": "Fulano", "FaceImage": "existingb64", "CardCode": "01000000"},
            ]}}),
            MagicMock(status_code=200, json=lambda: {"retcode": 0}),
        ]
        self._client().delete_card("123", "1")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "")
        self.assertEqual(sent["data"]["item"][0]["FaceImage"], "existingb64")

    @patch("face_agent.intelbras_client.requests.post")
    def test_delete_card_keeps_other_cards(self, mock_post):
        existing_response = MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": [
            {"ID": 7, "UserID": "123", "Name": "Fulano", "CardCode": "01000000,02000000"},
        ]}})
        mock_post.side_effect = [existing_response, existing_response, MagicMock(status_code=200, json=lambda: {"retcode": 0})]
        self._client().delete_card("123", "1")
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["data"]["item"][0]["CardCode"], "02000000")

    @patch("face_agent.intelbras_client.requests.post")
    def test_delete_card_noop_when_user_missing(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "data": {"item": []}})
        self._client().delete_card("999", "1")  # não deve lançar
        self.assertEqual(mock_post.call_count, 1)

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

    @patch("face_agent.intelbras_client.requests.get")
    def test_fetch_picture_builds_url_from_bare_filename(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        result = self._client().fetch_picture("2026-01-01_10-00-00.jpg")
        self.assertEqual(result, JPEG)
        sent_url = mock_get.call_args.args[0]
        self.assertEqual(sent_url, "http://10.0.0.1:80/Image/DoorPicture/2026-01-01_10-00-00.jpg")

    @patch("face_agent.intelbras_client.requests.get")
    def test_fetch_picture_discards_device_scheme_and_host(self, mock_get):
        """O device sempre embute https no campo Picture (cert fraco, Python recusa) —
        precisa ignorar o scheme+host que ele manda e usar a config da própria API (http)."""
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        self._client().fetch_picture("https://10.101.1.121/Image/DoorPicture/foo.jpg")
        sent_url = mock_get.call_args.args[0]
        self.assertEqual(sent_url, "http://10.0.0.1:80/Image/DoorPicture/foo.jpg")

    @patch("face_agent.intelbras_client.requests.get")
    def test_fetch_picture_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().fetch_picture("foo.jpg"))

    @patch("face_agent.intelbras_client.requests.post")
    def test_capture_snapshot_decodes_base64_data_uri(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {
            "retcode": 0, "action": "get",
            "data": {"snapshot": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii")},
        })
        self.assertEqual(self._client().capture_snapshot(), JPEG)

    @patch("face_agent.intelbras_client.requests.post")
    def test_capture_snapshot_returns_none_on_retcode_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 1, "message": "erro"})
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.intelbras_client.requests.post")
    def test_capture_snapshot_returns_none_on_network_error(self, mock_post):
        mock_post.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.intelbras_client.requests.post")
    def test_clear_all_users(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": 0, "action": "clear", "message": "OK"})
        self._client().clear_all_users()
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent["target"], "user")
        self.assertEqual(sent["action"], "clear")

    @patch("face_agent.intelbras_client.requests.post")
    def test_clear_all_users_raises_on_error(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"retcode": -1, "message": "erro"})
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()


class IntelbrasBioTClientTest(unittest.TestCase):
    def _client(self, **kwargs):
        return IntelbrasBioTClient(IntelbrasBioTTerminal(host="10.0.0.3", port=80, username="admin", password="pw", **kwargs))

    @patch("face_agent.intelbras_biot_client.requests.post")
    def test_enroll_create(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, text="OK"),  # AccessUser insertMulti
            MagicMock(status_code=200, text="OK"),  # AccessFace insertMulti
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "created")
        first_call = mock_post.call_args_list[0]
        self.assertEqual(first_call.kwargs["params"], {"action": "insertMulti"})
        self.assertEqual(first_call.kwargs["json"]["UserList"][0]["UserID"], "123")

    @patch("face_agent.intelbras_biot_client.requests.post")
    def test_enroll_update_when_already_exists(self, mock_post):
        mock_post.side_effect = [
            MagicMock(status_code=200, text="accessControlErrorUserAlreadyExist"),  # insertMulti
            MagicMock(status_code=200, text="OK"),  # updateMulti (usuário)
            MagicMock(status_code=200, text="OK"),  # AccessFace insertMulti
        ]
        status = self._client().enroll_face("123", "Fulano", JPEG)
        self.assertEqual(status, "updated")
        self.assertEqual(mock_post.call_count, 3)

    def test_enroll_rejects_invalid_jpeg(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_face("123", "Fulano", b"not a jpeg")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_open_door_never_raises(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        result = self._client().open_door()
        self.assertEqual(result, {"ok": False, "reason": "falha de rede ao chamar 10.0.0.3: timeout"})

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_open_door_uses_channel(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="OK")
        self._client(channel=2).open_door()
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["channel"], 2)

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_check_health_parses_kv(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="version=2.000.00IB003.0.R,build:2021-06-22")
        info = self._client().check_health()
        self.assertEqual(info["version"], "2.000.00IB003.0.R")
        self.assertEqual(info["build"], "2021-06-22")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_delete_user_info_raises_on_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="accessControlErrorRelevantUserNotFound")
        with self.assertRaises(FaceProvisioningError):
            self._client().delete_user_info("123")

    @patch("face_agent.intelbras_biot_client.requests.get")
    @patch("face_agent.intelbras_biot_client.requests.post")
    def test_enroll_card_create(self, mock_post, mock_get):
        mock_post.side_effect = [
            MagicMock(status_code=200, text="OK"),  # AccessUser insertMulti
            MagicMock(status_code=200, text="OK"),  # AccessCard insertMulti
        ]
        status = self._client().enroll_card("123", "Fulano", "AAABBB")
        self.assertEqual(status, "created")
        card_call = mock_post.call_args_list[1]
        self.assertEqual(card_call.kwargs["json"]["CardList"][0]["CardNo"], "AAABBB")

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_delete_card_uses_cardno_not_userid(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="OK")
        self._client().delete_card("123", "AAABBB")
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["CardNoList[0]"], "AAABBB")
        self.assertNotIn("UserID", sent)

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_clear_all_users(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="OK")
        self._client().clear_all_users()
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["action"], "clear")
        self.assertEqual(sent["name"], "AccessControlCard")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_clear_all_users_raises_on_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text="businessCommonErrorUnKnownError")
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_fetch_access_records_parses_indexed_fields(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, text=(
            "found=2\r\n"
            "records[0].RecNo=1\r\n"
            "records[0].UserID=123\r\n"
            "records[1].RecNo=2\r\n"
            "records[1].UserID=\r\n"
        ))
        records = self._client().fetch_access_records(0, 1)
        self.assertEqual(records, [{"RecNo": "1", "UserID": "123"}, {"RecNo": "2", "UserID": ""}])
        sent = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent["name"], "AccessControlCardRec")

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_fetch_picture_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("boom")
        self.assertIsNone(self._client().fetch_picture("/pic/a.jpg"))

    @patch("face_agent.intelbras_biot_client.requests.get")
    def test_fetch_picture_returns_bytes_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        self.assertEqual(self._client().fetch_picture("/pic/a.jpg"), JPEG)


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

    @patch("face_agent.hikvision_client.requests.get")
    def test_fetch_picture_returns_bytes_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        result = self._client().fetch_picture("http://10.0.0.2/LOCALS/pic/foo.jpeg@WEB1")
        self.assertEqual(result, JPEG)

    @patch("face_agent.hikvision_client.requests.get")
    def test_fetch_picture_returns_none_on_http_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"")
        self.assertIsNone(self._client().fetch_picture("http://10.0.0.2/LOCALS/pic/foo.jpeg@WEB1"))

    @patch("face_agent.hikvision_client.requests.get")
    def test_fetch_picture_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().fetch_picture("http://10.0.0.2/LOCALS/pic/foo.jpeg@WEB1"))

    @patch("face_agent.hikvision_client.requests.get")
    def test_capture_snapshot_returns_bytes_on_success(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=JPEG)
        self.assertEqual(self._client().capture_snapshot(), JPEG)
        sent_url = mock_get.call_args.args[0]
        self.assertEqual(sent_url, "http://10.0.0.2:80/ISAPI/Streaming/channels/101/picture")

    @patch("face_agent.hikvision_client.requests.get")
    def test_capture_snapshot_returns_none_on_http_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, content=b"")
        self.assertIsNone(self._client().capture_snapshot())

    @patch("face_agent.hikvision_client.requests.get")
    def test_capture_snapshot_returns_none_on_network_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("boom")
        self.assertIsNone(self._client().capture_snapshot())

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

    @patch("face_agent.hikvision_client.requests.request")
    def test_enroll_card_create_flow(self, mock_request):
        mock_request.side_effect = [
            MagicMock(status_code=200),  # UserInfo/Record
            MagicMock(status_code=200),  # CardInfo/Record
        ]
        status = self._client().enroll_card("123", "Fulano", "AAABBB")
        self.assertEqual(status, "created")
        self.assertEqual(mock_request.call_count, 2)
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["CardInfo"]["cardNo"], "AAABBB")
        # VALIDADO AO VIVO: sem cardType o device recusa com "MessageParametersLack"/"cardType".
        self.assertEqual(kwargs["json"]["CardInfo"]["cardType"], "normalCard")

    def test_enroll_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().enroll_card("123", "Fulano", "")

    @patch("face_agent.hikvision_client.requests.request")
    def test_delete_card_uses_cardno_not_employee_no(self, mock_request):
        # Deletar por employeeNo apagaria TODOS os cartões da pessoa — bug corrigido, uma
        # pessoa pode ter mais de um cartão.
        mock_request.return_value = MagicMock(status_code=200)
        self._client().delete_card("123", "AAABBB")
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["json"]["CardInfoDelCond"], {"CardNoList": [{"cardNo": "AAABBB"}]})

    def test_delete_card_rejects_empty(self):
        with self.assertRaises(FaceProvisioningError):
            self._client().delete_card("123", "")

    @patch("face_agent.hikvision_client.requests.request")
    def test_clear_all_users_deletes_in_batches_until_empty(self, mock_request):
        mock_request.side_effect = [
            MagicMock(status_code=200, json=lambda: {"UserInfoSearch": {
                "UserInfo": [{"employeeNo": "1"}, {"employeeNo": "2"}], "responseStatusStrg": "OK",
            }}),
            MagicMock(status_code=200),  # delete do lote
            MagicMock(status_code=200, json=lambda: {"UserInfoSearch": {"UserInfo": [], "responseStatusStrg": "NO_MATCHES"}}),
        ]
        removed = self._client().clear_all_users()
        self.assertEqual(removed, 2)
        self.assertEqual(mock_request.call_count, 3)
        delete_call = mock_request.call_args_list[1]
        self.assertEqual(
            delete_call.kwargs["json"]["UserInfoDelCond"]["EmployeeNoList"],
            [{"employeeNo": "1"}, {"employeeNo": "2"}],
        )

    @patch("face_agent.hikvision_client.requests.request")
    def test_clear_all_users_raises_on_search_failure(self, mock_request):
        mock_request.return_value = MagicMock(status_code=500, text="erro")
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()

    @patch("face_agent.hikvision_client.requests.request")
    def test_clear_all_users_aborts_if_never_empties(self, mock_request):
        # device com bug: search sempre devolve o mesmo usuário mesmo após apagar.
        mock_request.side_effect = lambda *a, **k: (
            MagicMock(status_code=200, json=lambda: {"UserInfoSearch": {"UserInfo": [{"employeeNo": "1"}], "responseStatusStrg": "OK"}})
            if k.get("json", {}).get("UserInfoSearchCond")
            else MagicMock(status_code=200)
        )
        with self.assertRaises(FaceProvisioningError):
            self._client().clear_all_users()


class FaceClientFactoryTest(unittest.TestCase):
    def test_unknown_vendor_raises(self):
        with self.assertRaises(ValueError):
            create_face_client("acme", host="x", port=80, username="a", password="b")

    def test_picks_class_by_vendor(self):
        hik = create_face_client("hikvision", host="x", port=80, username="a", password="b")
        intel = create_face_client("intelbras", host="x", port=80, username="a", password="b")
        biot = create_face_client("intelbras_biot", host="x", port=80, username="a", password="b")
        self.assertIsInstance(hik, HikvisionClient)
        self.assertIsInstance(intel, IntelbrasClient)
        self.assertIsInstance(biot, IntelbrasBioTClient)


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

    def test_card_roster_upsert_list_set_enrolled_remove(self):
        self.store.upsert_card_roster("t1", "123", "Fulano", "AAABBB")
        entries = self.store.list_card_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].card_no, "AAABBB")
        self.assertFalse(entries[0].enrolled)

        self.store.set_card_enrolled("t1", "123", "AAABBB", True)
        self.assertTrue(self.store.list_card_by_terminal("t1")[0].enrolled)

        # upsert do MESMO card_no não duplica, só atualiza o nome
        self.store.upsert_card_roster("t1", "123", "Fulano Silva", "AAABBB")
        entries = self.store.list_card_by_terminal("t1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Fulano Silva")

        self.store.remove_card_roster("t1", "123", "AAABBB")
        self.assertEqual(self.store.list_card_by_terminal("t1"), [])

    def test_card_roster_supports_multiple_cards_per_person(self):
        self.store.upsert_card_roster("t1", "123", "Fulano", "AAABBB")
        self.store.upsert_card_roster("t1", "123", "Fulano", "CCCDDD")
        entries = sorted(self.store.list_card_by_terminal("t1"), key=lambda e: e.card_no)
        self.assertEqual([e.card_no for e in entries], ["AAABBB", "CCCDDD"])

        # remove só um cartão, o outro continua
        self.store.remove_card_roster("t1", "123", "AAABBB")
        entries = self.store.list_card_by_terminal("t1")
        self.assertEqual([e.card_no for e in entries], ["CCCDDD"])

    def test_cursor_get_set_roundtrip(self):
        self.assertIsNone(self.store.get_cursor("t1"))
        self.store.set_cursor(PollCursor(terminal_id="t1", last_event_time="2026-01-01T00:00:00-03:00", search_id="42"))
        cursor = self.store.get_cursor("t1")
        self.assertEqual(cursor.last_event_time, "2026-01-01T00:00:00-03:00")
        self.assertEqual(cursor.search_id, "42")

    def test_add_events_dedupes_and_persists_picture(self):
        base = {"terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00",
                "direction": "in", "success": True, "source": "poll"}
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": JPEG}])
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": JPEG}])  # reenvio não duplica

        events = self.store.list_recent_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].has_picture)
        self.assertEqual(self.store.get_event_picture("k1"), JPEG)

    def test_add_events_prunes_events_older_than_retention(self):
        from datetime import datetime, timedelta, timezone
        from face_agent.local_store import EVENTS_RETENTION_DAYS, _BR_TZ

        now = datetime.now(_BR_TZ)
        old_time = (now - timedelta(days=EVENTS_RETENTION_DAYS + 1)).isoformat()
        recent_time = (now - timedelta(days=1)).isoformat()
        base = {"terminal_id": "t1", "employee_no": "123", "direction": "in", "success": True, "source": "poll"}

        self.store.add_events([{**base, "dedupe_key": "old", "time": old_time, "picture": None}])
        self.store.add_events([{**base, "dedupe_key": "recent", "time": recent_time, "picture": None}])

        keys = {e.dedupe_key for e in self.store.list_recent_events()}
        self.assertEqual(keys, {"recent"})

    def test_query_events_name_prefers_roster_falls_back_to_device_name(self):
        # t1/123 está no roster (nome "oficial", sync do ZAccess) -> ganha do device_name.
        # t2/999 nunca foi cadastrado por aqui (enrolado direto no terminal) -> só
        # sobra o nome que o próprio device mandou no evento.
        self.store.upsert_roster("t1", "123", "Fulano do Roster", JPEG, None)
        self.store.add_events([
            {"dedupe_key": "e1", "terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00",
             "direction": "in", "success": True, "source": "poll", "picture": None, "device_name": "Fulano do Device"},
            {"dedupe_key": "e2", "terminal_id": "t2", "employee_no": "999", "time": "2026-01-01T11:00:00-03:00",
             "direction": "in", "success": True, "source": "poll", "picture": None, "device_name": "Ciclano (Teste)"},
        ])
        by_key = {e.dedupe_key: e for e in self.store.list_recent_events()}
        self.assertEqual(by_key["e1"].name, "Fulano do Roster")
        self.assertEqual(by_key["e2"].name, "Ciclano (Teste)")

        # busca por texto também casa contra o device_name
        found, total = self.store.query_events(q="Ciclano")
        self.assertEqual(total, 1)
        self.assertEqual(found[0].dedupe_key, "e2")

    def test_add_events_backfills_device_name_without_overwriting(self):
        base = {"terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00",
                "direction": "in", "success": True, "source": "poll"}
        # 1a passada: sem device_name (terminal antigo, ou campo ausente naquele evento)
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": None, "device_name": None}])
        self.assertIsNone(self.store.list_recent_events()[0].name)

        # 2a passada (reprocesso, ex.: cursor rebobinado): agora vem com device_name -> preenche
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": None, "device_name": "Fulano"}])
        self.assertEqual(self.store.list_recent_events()[0].name, "Fulano")

        # 3a passada com outro nome não sobrescreve o que já foi preenchido
        self.store.add_events([{**base, "dedupe_key": "k1", "picture": None, "device_name": "Outro Nome"}])
        self.assertEqual(self.store.list_recent_events()[0].name, "Fulano")

    def test_query_events_filters_search_and_paginates(self):
        self.store.upsert_roster("t1", "123", "Fulano de Tal", JPEG, None)
        events = [
            {"dedupe_key": "e1", "terminal_id": "t1", "employee_no": "123", "time": "2026-01-01T10:00:00-03:00", "direction": "in", "success": True, "source": "poll", "picture": None},
            {"dedupe_key": "e2", "terminal_id": "t1", "employee_no": None, "time": "2026-01-01T11:00:00-03:00", "direction": "in", "success": False, "source": "poll", "picture": None},
            {"dedupe_key": "e3", "terminal_id": "t2", "employee_no": "123", "time": "2026-01-01T12:00:00-03:00", "direction": "in", "success": True, "source": "poll", "picture": None},
        ]
        self.store.add_events(events)

        # nome vem via join com o roster
        all_events, total = self.store.query_events()
        self.assertEqual(total, 3)
        by_key = {e.dedupe_key: e for e in all_events}
        self.assertEqual(by_key["e1"].name, "Fulano de Tal")
        self.assertIsNone(by_key["e2"].name)

        # filtro por terminal
        t1_events, t1_total = self.store.query_events(terminal_id="t1")
        self.assertEqual(t1_total, 2)
        self.assertTrue(all(e.terminal_id == "t1" for e in t1_events))

        # filtro por sucesso
        fails, fail_total = self.store.query_events(success=False)
        self.assertEqual(fail_total, 1)
        self.assertEqual(fails[0].dedupe_key, "e2")

        # busca por nome (via roster, escopado por terminal — só e1 tem roster em t1) e
        # por employee_no (casa direto, sem depender de roster — e1 e e3)
        by_name, by_name_total = self.store.query_events(q="Fulano")
        self.assertEqual(by_name_total, 1)
        self.assertEqual(by_name[0].dedupe_key, "e1")
        by_empno, by_empno_total = self.store.query_events(q="123")
        self.assertEqual(by_empno_total, 2)

        # paginação (mais recente primeiro)
        page1, total_p = self.store.query_events(limit=2, offset=0)
        page2, _ = self.store.query_events(limit=2, offset=2)
        self.assertEqual(total_p, 3)
        self.assertEqual([e.dedupe_key for e in page1], ["e3", "e2"])
        self.assertEqual([e.dedupe_key for e in page2], ["e1"])

    def test_get_event_by_dedupe_key(self):
        self.store.add_events([{"dedupe_key": "solo", "terminal_id": "t1", "employee_no": None,
                                 "time": "2026-01-01T10:00:00-03:00", "direction": "in",
                                 "success": True, "source": "poll", "picture": JPEG}])
        event = self.store.get_event("solo")
        self.assertEqual(event.dedupe_key, "solo")
        self.assertTrue(event.has_picture)
        self.assertIsNone(self.store.get_event("nao-existe"))

    def test_add_events_without_picture(self):
        self.store.add_events([{
            "dedupe_key": "k2", "terminal_id": "t1", "employee_no": None, "time": "2026-01-01T10:00:00-03:00",
            "direction": "unknown", "success": False, "source": "poll",
        }])
        events = self.store.list_recent_events()
        self.assertFalse(events[0].has_picture)
        self.assertIsNone(self.store.get_event_picture("k2"))

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

    def test_normalize_event_ignores_non_face_minor_codes(self):
        # minor 21/22 = contato de porta abriu/fechou — mesmo stream de AcsEvent do
        # reconhecimento facial, mas não é uma tentativa de acesso (validado ao vivo
        # contra um DS-K1T671MF-L real). Sem esse filtro viram "Negado" falso no painel.
        self.assertIsNone(_normalize_event("t1", {"major": 5, "minor": 21, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll"))
        self.assertIsNone(_normalize_event("t1", {"major": 5, "minor": 22, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll"))

    def test_normalize_event_extracts_picture_url(self):
        event = _normalize_event("t1", {
            "major": 5, "minor": 75, "employeeNo": "123", "dateTime": "2026-01-01T00:00:00-03:00",
            "pictureURL": "http://192.168.1.100/LOCALS/pic/foo.jpeg@WEB1",
        }, "poll")
        self.assertEqual(event["picture_url"], "http://192.168.1.100/LOCALS/pic/foo.jpeg@WEB1")

    def test_normalize_event_no_picture_url(self):
        event = _normalize_event("t1", {"major": 5, "minor": 76, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertIsNone(event["picture_url"])

    def test_normalize_event_extracts_device_name(self):
        # cardholder name que o próprio Hikvision manda no AcsEvent (validado ao vivo
        # contra um DS-K1T671MF-L real) — só existe pra quem foi enrolado direto no
        # device, sem passar pelo roster local/ZAccess.
        event = _normalize_event("t1", {
            "major": 5, "minor": 75, "employeeNo": "123", "dateTime": "2026-01-01T00:00:00-03:00",
            "name": "Bernardo (Teste)",
        }, "poll")
        self.assertEqual(event["device_name"], "Bernardo (Teste)")

    def test_normalize_event_no_device_name(self):
        event = _normalize_event("t1", {"major": 5, "minor": 76, "dateTime": "2026-01-01T00:00:00-03:00"}, "poll")
        self.assertIsNone(event["device_name"])


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
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "UserID": "123", "Picture": "2026-01-01_10-00-00.jpg"},
            {"ID": "11", "Date": "2026-01-01", "Time": "10:05:00", "Status": "Fail", "UserID": "Desconhecido"},
        ]}}

        events, next_cursor = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["employee_no"], "123")
        self.assertIsNone(events[1]["employee_no"])
        self.assertEqual(next_cursor.search_id, "11")

    def test_picture_field_present_even_for_desconhecido(self):
        """Doorlog manda `Picture` pra qualquer evento, inclusive acesso negado/desconhecido
        (validado no manual oficial do XPE, seção "Eventos em tempo real") — sem esse campo
        virando picture_url, o painel nunca mostrava foto de gente não cadastrada."""
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Fail", "UserID": "Desconhecido", "Picture": "2026-01-01_10-00-00.jpg"},
        ]}}

        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(events[0]["picture_url"], "2026-01-01_10-00-00.jpg")

    def test_skips_doorlog_items_without_userid(self):
        """Abertura via API/relay (cockpit) não carrega UserID (doc oficial: exemplo
        Code=OpenDoor/Name=HTTPAPI/Type=Cloud não tem esse campo) — sem o skip, duplicava
        com o evento que a rota do cockpit já registra direto."""
        client = MagicMock()
        client.call.return_value = {"retcode": 0, "data": {"item": [
            {"ID": "10", "Date": "2026-01-01", "Time": "10:00:00", "Status": "Success", "Code": "OpenDoor", "Name": "HTTPAPI", "Type": "Cloud"},
            {"ID": "11", "Date": "2026-01-01", "Time": "10:05:00", "Status": "Success", "UserID": "123"},
        ]}}

        events, _ = fetch_intelbras_events_since(client, "t1", "in", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["employee_no"], "123")


class IntelbrasBioTEventsFetchTest(unittest.TestCase):
    def test_first_cycle_anchors_cursor_without_events(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [{"RecNo": "10"}, {"RecNo": "15"}]

        events, next_cursor = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1"))

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.search_id, "15")

    def test_returns_only_new_items_and_maps_error_to_failure(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [
            {"RecNo": "10", "CreateTime": "1735732800", "ErrorCode": "0", "UserID": "123", "Type": "Entry", "URL": "/pic/a.jpg"},
            {"RecNo": "11", "CreateTime": "1735732900", "ErrorCode": "16", "UserID": "", "Type": "Entry"},
        ]

        events, next_cursor = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["employee_no"], "123")
        self.assertEqual(events[0]["direction"], "in")
        self.assertEqual(events[0]["picture_url"], "/pic/a.jpg")
        self.assertFalse(events[1]["success"])
        self.assertIsNone(events[1]["employee_no"])
        self.assertEqual(next_cursor.search_id, "11")

    def test_ignores_items_already_seen(self):
        client = MagicMock()
        client.fetch_access_records.return_value = [
            {"RecNo": "9", "CreateTime": "1735732800", "ErrorCode": "0", "UserID": "1", "Type": "Entry"},
        ]

        events, next_cursor = fetch_intelbras_biot_events_since(client, "t1", PollCursor(terminal_id="t1", search_id="9"))

        self.assertEqual(events, [])
        self.assertEqual(next_cursor.search_id, "9")

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
