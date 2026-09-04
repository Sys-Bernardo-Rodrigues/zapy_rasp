"""
Self-check do face_agent — sem terminal físico. Mocka `requests` pra validar o
protocolo (envelope Intelbras, branches create/update, XML sem format=json no
Hikvision, circuit breaker de auth). Rodar: python -m unittest test_face_agent -v
"""
import unittest
from unittest.mock import MagicMock, patch

import requests

from face_agent.errors import FaceProvisioningError
from face_agent.face_client_factory import create_face_client
from face_agent.hikvision_client import MAX_CONSEC_AUTH_FAILURES, HikvisionClient, HikvisionTerminal
from face_agent.intelbras_client import IntelbrasClient, IntelbrasTerminal

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


class FaceClientFactoryTest(unittest.TestCase):
    def test_unknown_vendor_raises(self):
        with self.assertRaises(ValueError):
            create_face_client("acme", host="x", port=80, username="a", password="b")

    def test_picks_class_by_vendor(self):
        hik = create_face_client("hikvision", host="x", port=80, username="a", password="b")
        intel = create_face_client("intelbras", host="x", port=80, username="a", password="b")
        self.assertIsInstance(hik, HikvisionClient)
        self.assertIsInstance(intel, IntelbrasClient)


if __name__ == "__main__":
    unittest.main()
