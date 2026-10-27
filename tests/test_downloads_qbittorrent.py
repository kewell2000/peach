"""qBittorrent 下载渠道：公开设置形状、本机凭据，以及用假 WebUI 提交磁力。"""
from __future__ import annotations

import json
from urllib.parse import parse_qs
from unittest import mock

import httpx

from peach import downloads_qbittorrent as qb
from peach import web_downloads
from test_web_downloads import HASH, _App

EXAMPLE = "http://192.0.2.17:8085"
OTHER = "http://192.0.2.18:8085"
PASSWORD = "qb-secret-password"
SAVE_URL = "/api/configuration/downloads/qbittorrent"
CHECK_URL = "/api/configuration/downloads/qbittorrent/check"
ADD_URL = "/api/configuration/downloads/qbittorrent/add"
PUBLIC_KEYS = {"enabled", "url", "username", "password_set", "category", "save_path"}


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("utf-8"))


class _WebUI:
    def __init__(self, *, login_status=200, login_body=b"Ok.", add_status=200,
                 add_body=b'{"success_count":1,"pending_count":0,"failure_count":0}',
                 category_status=200, session_cookie="SID=sid-token"):
        self.login_status = login_status
        self.login_body = login_body
        self.add_status = add_status
        self.add_body = add_body
        self.category_status = category_status
        self.session_cookie = session_cookie
        self.calls: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/auth/login"):
            if self.login_status in {301, 302, 303, 307, 308}:
                return httpx.Response(self.login_status, headers={"Location": "http://192.0.2.99/other"})
            headers = {"Set-Cookie": f"{self.session_cookie}; HttpOnly; Path=/"} if self.session_cookie else {}
            return httpx.Response(self.login_status, content=self.login_body, headers=headers)
        if path.endswith("/app/webapiVersion"):
            return httpx.Response(200, content=b"2.15.1")
        if path.endswith("/app/version"):
            return httpx.Response(200, content=b"v5.1.2")
        if path.endswith("/torrents/createCategory"):
            return httpx.Response(self.category_status, content=b"")
        if path.endswith("/torrents/add"):
            return httpx.Response(self.add_status, content=self.add_body)
        if path.endswith("/auth/logout"):
            return httpx.Response(200, content=b"Ok.")
        return httpx.Response(404, content=b"missing")

    def paths(self) -> list[str]:
        return [request.url.path for request in self.calls]


class QbitRouteTests(_App):
    def setUp(self):
        super().setUp()
        self.local = httpx_client(self)
        self.web = _WebUI()
        original = qb._client

        def wrapped(transport=None):
            return original(self.web.transport)

        self.enterContext(mock.patch.object(qb, "_client", wrapped))

    def body(self, **overrides) -> dict:
        payload = {"enabled": True, "url": EXAMPLE, "username": "admin", "password": PASSWORD,
                   "category": "peach", "save_path": "/downloads/peach"}
        payload.update(overrides)
        return payload

    def save(self, **overrides):
        response = self.local.post(SAVE_URL, json=self.body(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_saved_settings_hide_the_password(self):
        shown = self.save()
        self.assertEqual(set(shown), PUBLIC_KEYS)
        self.assertEqual(shown, {"enabled": True, "url": EXAMPLE, "username": "admin",
                                 "password_set": True, "category": "peach",
                                 "save_path": "/downloads/peach"})
        self.assertNotIn(PASSWORD, json.dumps(shown))
        payload = web_downloads.settings_payload(self.service, {})
        self.assertEqual(payload["qbittorrent"], shown)
        self.assertNotIn(PASSWORD, json.dumps(payload))
        state = (self.root / "state" / qb.SETTINGS_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, state)
        self.assertNotIn("username", state)
        secret = json.loads(self.secrets()["download-qbittorrent.json"])
        self.assertEqual(secret, {"username": "admin", "password": PASSWORD})
        listed = self.client.get("/api/downloads").json()
        self.assertEqual(listed["qbittorrent"], {"ready": True})

    def test_a_blank_password_stays_until_the_address_changes_or_it_is_cleared(self):
        self.save()
        kept = self.save(password="")
        self.assertTrue(kept["password_set"])
        self.assertEqual(json.loads(self.secrets()["download-qbittorrent.json"])["password"], PASSWORD)
        moved = self.save(enabled=False, url=OTHER, password="")
        self.assertFalse(moved["password_set"])
        self.assertNotIn(PASSWORD, self.secrets()["download-qbittorrent.json"])
        self.save(password=PASSWORD)
        cleared = self.save(enabled=False, password="", clear_password=True)
        self.assertFalse(cleared["password_set"])
        self.assertNotIn(PASSWORD, self.secrets()["download-qbittorrent.json"])

    def test_cloud_settings_do_not_drop_qbittorrent(self):
        self.save()
        web_downloads.save_settings(self.service, {"clouddrive_address": "127.0.0.1:19798"}, {})
        shown = web_downloads.settings_payload(self.service, {})["qbittorrent"]
        self.assertTrue(shown["password_set"])
        self.assertEqual(shown["url"], EXAMPLE)

    def test_remote_and_cross_origin_writes_are_refused(self):
        self.assertEqual(self.client.post(SAVE_URL, json=self.body()).status_code, 403)
        refused = self.local.post(SAVE_URL, json=self.body(), headers={"Origin": "https://other.invalid"})
        self.assertEqual(refused.status_code, 403)
        self.assertFalse((self.root / "state" / qb.SETTINGS_FILENAME).exists())

    def test_add_before_it_is_enabled_does_not_call_out(self):
        response = self.local.post(ADD_URL, json={"magnet": f"magnet:?xt=urn:btih:{HASH}"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("先在下载渠道保存并启用 qBittorrent", response.text)
        self.assertEqual(self.web.calls, [])

    def test_check_uses_the_saved_password_only_for_the_same_address(self):
        self.save()
        self.web.calls.clear()
        with self.assertNoLogs("peach.downloads_qbittorrent", level="DEBUG"):
            report = self.local.post(CHECK_URL, json={"url": EXAMPLE, "username": "admin", "password": ""})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json(), {"ok": True, "version": "v5.1.2", "webapi_version": "2.15.1",
                                         "message": ""})
        self.assertNotIn(PASSWORD, report.text)
        login = next(request for request in self.web.calls if request.url.path.endswith("/auth/login"))
        self.assertEqual(_form(login)["password"], [PASSWORD])
        self.assertEqual(login.headers["origin"], EXAMPLE)
        self.assertEqual(login.headers["referer"], EXAMPLE)
        self.web.calls.clear()
        other = self.local.post(CHECK_URL, json={"url": OTHER, "username": "admin", "password": ""})
        self.assertEqual(other.json()["ok"], False)
        self.assertIn("请填写 qBittorrent 地址、用户名和密码", other.json()["message"])
        self.assertEqual(self.web.calls, [])

    def test_a_magnet_is_added_with_only_the_infohash(self):
        self.save()
        self.web.calls.clear()
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=ABC-123&tr=http://tracker.example/announce"
        with self.assertNoLogs("peach.downloads_qbittorrent", level="DEBUG"):
            response = self.local.post(ADD_URL, json={"magnet": magnet})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "info_hash": HASH, "pending": False})
        self.assertNotIn(PASSWORD, response.text)
        self.assertEqual(self.web.paths(), [
            "/api/v2/auth/login", "/api/v2/torrents/createCategory", "/api/v2/torrents/add",
            "/api/v2/auth/logout"])
        login, category, added, _logout = self.web.calls
        self.assertEqual(_form(login)["password"], [PASSWORD])
        self.assertNotIn(PASSWORD, added.content.decode("utf-8"))
        self.assertEqual(added.headers["cookie"], "SID=sid-token")
        self.assertEqual(added.headers["origin"], EXAMPLE)
        self.assertEqual(added.headers["referer"], EXAMPLE)
        self.assertEqual(_form(category)["category"], ["peach"])
        fields = _form(added)
        self.assertEqual(fields["urls"], [f"magnet:?xt=urn:btih:{HASH}"])
        self.assertEqual(fields["savepath"], ["/downloads/peach"])
        self.assertEqual(fields["category"], ["peach"])
        self.assertEqual(fields["autoTMM"], ["false"])
        self.assertNotIn("tr", fields["urls"][0])

    def test_webapi_215_login_accepts_204_and_a_port_cookie(self):
        self.web.login_status = 204
        self.web.login_body = b""
        self.web.session_cookie = "QBT_SID_8085=port-token"
        self.save()
        self.web.calls.clear()
        report = self.local.post(CHECK_URL, json={"url": EXAMPLE, "username": "admin", "password": ""})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["ok"], True)
        added = self.local.post(ADD_URL, json={"magnet": f"magnet:?xt=urn:btih:{HASH}"})
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["ok"], True)
        sent = [request.headers["cookie"] for request in self.web.calls
                if request.url.path.endswith("/torrents/add")]
        self.assertEqual(sent, ["QBT_SID_8085=port-token"])

    def test_an_empty_204_without_a_session_cookie_is_not_a_login(self):
        self.web.login_status = 204
        self.web.login_body = b""
        self.web.session_cookie = ""
        report = self.local.post(CHECK_URL, json={"url": EXAMPLE, "username": "admin", "password": PASSWORD})
        self.assertEqual(report.json()["ok"], False)
        self.assertEqual(report.json()["message"], "qBittorrent 没有完成登录")

    def test_a_legacy_ok_and_an_existing_category_still_add(self):
        self.web.add_body = b"Ok."
        self.web.category_status = 409
        self.save(category="peach")
        self.web.calls.clear()
        response = self.local.post(ADD_URL, json={"magnet": HASH})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pending"], False)
        self.assertIn("/api/v2/torrents/add", self.web.paths())

    def test_a_rejected_login_does_not_echo_the_password(self):
        self.web.login_body = b"Fails."
        self.save()
        self.web.calls.clear()
        report = self.local.post(CHECK_URL, json={"url": EXAMPLE, "username": "admin", "password": PASSWORD})
        self.assertEqual(report.json()["ok"], False)
        self.assertEqual(report.json()["message"], "qBittorrent 用户名或密码不正确")
        self.assertNotIn(PASSWORD, report.text)
        self.assertEqual(self.web.paths(), ["/api/v2/auth/login", "/api/v2/auth/logout"])

    def test_a_redirect_is_not_followed(self):
        self.web.login_status = 302
        report = self.local.post(CHECK_URL, json={"url": EXAMPLE, "username": "admin", "password": PASSWORD})
        self.assertEqual(report.json()["ok"], False)
        self.assertIn("跳转", report.json()["message"])
        self.assertTrue(self.web.calls)
        self.assertTrue(all(request.url.host == "192.0.2.17" for request in self.web.calls))


def httpx_client(test: QbitRouteTests):
    from fastapi.testclient import TestClient
    return TestClient(test.app, base_url="http://127.0.0.1", client=("127.0.0.1", 123))
