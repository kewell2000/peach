"""把磁力交给用户自己的 qBittorrent WebUI。

只覆盖登录、读版本、按需建分类和 ``torrents/add``。密码留在本机凭据文件，
地址、开关、分类和保存路径留在本机状态文件。不写账本，也不改 115 / PikPak 的任务。

协议对照 qBittorrent WebAPI 文档与 changelog 至 2.15.1。登录成功有两种：
200 且正文是 ``Ok.``、cookie 名为 ``SID``；5.2 起是 204、空正文、cookie 名以
``QBT_SID_`` 开头。失败正文是 ``Fails.``，或状态 401。添加在正文 ``Ok.`` 或
``success_count`` / ``pending_count`` / ``failure_count`` 两种形状下都算完成。
请求带与主机一致的 ``Referer`` 和 ``Origin``，不跟随重定向，响应有大小上限。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .downloads import Magnet, parse_magnet
from .follow_secrets import CredentialError, CredentialStore
from .fsutil import atomic_write_text
from .user_agent import USER_AGENT

CREDENTIAL = "download-qbittorrent"
SETTINGS_FILENAME = "qbittorrent.json"
MAX_BODY = 64 * 1024
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class QbitError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class QbitConfig:
    enabled: bool = False
    url: str = ""
    category: str = ""
    save_path: str = ""

    def payload(self) -> dict:
        return {"enabled": self.enabled, "url": self.url, "category": self.category,
                "save_path": self.save_path}


def clean_base_url(raw) -> str:
    """WebUI 根地址。登录信息单独填写，地址里不放用户名、密码或查询。"""
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
    except ValueError:
        raise ValueError("qBittorrent 地址格式不正确") from None
    if (len(text) > 2048 or parts.scheme not in {"http", "https"} or not hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or any(char.isspace() for char in text)):
        raise ValueError("地址只写协议、主机、端口和可选路径，例如 http://192.0.2.17:8085")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _origin(base: str) -> str:
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}"


def clean_category(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    spaced = any(char.isspace() and char != " " for char in text)
    if len(text) > 80 or spaced or text.endswith(".") or any(char in text for char in "\\/"):
        raise ValueError("分类名不能包含斜杠，也不能以点结尾")
    return text


def clean_save_path(raw) -> str:
    text = str(raw or "").strip()
    if len(text) > 500 or any(char in text for char in "\n\r\x00"):
        raise ValueError("保存路径过长或含有换行")
    return text


def clean_username(raw) -> str:
    text = str(raw or "").strip()
    if len(text) > 200 or any(char in text for char in "\n\r\x00"):
        raise ValueError("用户名过长或含有换行")
    return text


def clean_password(raw) -> str:
    text = str(raw or "")
    if len(text) > 200 or any(char in text for char in "\n\r\x00"):
        raise ValueError("密码过长或含有换行")
    return text


def submission_magnet(raw: str) -> Magnet:
    """只提交 infohash 磁力，不转发索引器附带的 tracker。"""
    magnet = parse_magnet(raw)
    return Magnet(magnet.info_hash, f"magnet:?xt=urn:btih:{magnet.info_hash}")


class QbitSettings:
    """地址、开关、分类和保存路径。密码不在这份文件里。"""

    def __init__(self, state_root: Path):
        self.path = Path(state_root) / SETTINGS_FILENAME

    def load(self) -> QbitConfig:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return QbitConfig()
        except (OSError, UnicodeError, json.JSONDecodeError):
            return QbitConfig()
        if not isinstance(payload, dict):
            return QbitConfig()
        try:
            return QbitConfig(
                enabled=payload.get("enabled") is True,
                url=clean_base_url(payload.get("url")),
                category=clean_category(payload.get("category")),
                save_path=clean_save_path(payload.get("save_path")))
        except ValueError:
            return QbitConfig()

    def save(self, config: QbitConfig) -> QbitConfig:
        atomic_write_text(self.path, json.dumps(config.payload(), ensure_ascii=False, indent=2) + "\n")
        return config


def _settings(service) -> QbitSettings:
    return QbitSettings(service.settings.path.parent)


def _secrets(credentials: CredentialStore) -> dict[str, str]:
    try:
        loaded = credentials.load(CREDENTIAL)
    except CredentialError:
        return {}
    return dict(loaded.values) if loaded else {}


def public_settings(service) -> dict:
    """设置页读到的形状。密码只报有没有保存。"""
    config = _settings(service).load()
    secrets = _secrets(service.credentials)
    return {"enabled": config.enabled, "url": config.url,
            "username": secrets.get("username", ""),
            "password_set": bool(secrets.get("password")),
            "category": config.category, "save_path": config.save_path}


def ready(service) -> bool:
    shown = public_settings(service)
    return bool(shown["enabled"] and shown["url"] and shown["username"] and shown["password_set"])


def save_config(service, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("设置必须是一个对象")
    current = _settings(service).load()
    secrets = _secrets(service.credentials)
    url = clean_base_url(body.get("url"))
    username = clean_username(body.get("username"))
    password = clean_password(body.get("password"))
    if password:
        stored = password
    elif body.get("clear_password") is True or url != current.url:
        stored = ""
    else:
        stored = secrets.get("password", "")
    config = QbitConfig(enabled=body.get("enabled") is True, url=url,
                        category=clean_category(body.get("category")),
                        save_path=clean_save_path(body.get("save_path")))
    if config.enabled and (not config.url or not username or not stored):
        raise ValueError("启用 qBittorrent 需要地址、用户名和密码")
    _settings(service).save(config)
    if username or stored:
        service.credentials.save(CREDENTIAL, {"username": username, "password": stored})
    else:
        service.credentials.clear(CREDENTIAL)
    return public_settings(service)


def _client(transport=None) -> httpx.Client:
    options: dict = {"timeout": _TIMEOUT, "follow_redirects": False, "trust_env": False,
                     "headers": {"User-Agent": USER_AGENT}}
    if transport is not None:
        options["transport"] = transport
    return httpx.Client(**options)


def _read(response: httpx.Response) -> bytes:
    if response.is_redirect:
        raise QbitError("这个地址发生了跳转，请填写 qBittorrent WebUI 本身的地址")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise QbitError("qBittorrent 的响应过长")
    return bytes(body)


class _Session:
    def __init__(self, base: str, transport=None):
        self.base = base
        self.origin = _origin(base)
        self.client = _client(transport)

    def close(self) -> None:
        self.client.close()

    def call(self, method: str, path: str, **kwargs) -> tuple[int, bytes]:
        headers = {"Referer": self.origin, "Origin": self.origin}
        try:
            with self.client.stream(method, f"{self.base}{path}", headers=headers,
                                     **kwargs) as response:
                return response.status_code, _read(response)
        except QbitError:
            raise
        except httpx.HTTPError:
            raise QbitError("连不上 qBittorrent，请检查地址和 WebUI 是否已开启") from None

    def login(self, username: str, password: str) -> None:
        status, body = self.call("POST", "/api/v2/auth/login",
                                 data={"username": username, "password": password})
        text = body.decode("utf-8", "replace").strip()
        if status == 403:
            raise QbitError("qBittorrent 暂时拒绝了这次登录，请稍后再试")
        if status == 401 or text == "Fails.":
            raise QbitError("qBittorrent 用户名或密码不正确")
        if not _login_accepted(status, text, self.client.cookies):
            raise QbitError("qBittorrent 没有完成登录")

    def logout(self) -> None:
        try:
            self.call("POST", "/api/v2/auth/logout")
        except QbitError:
            return

    def versions(self) -> tuple[str, str]:
        status, web = self.call("GET", "/api/v2/app/webapiVersion")
        if status != 200:
            raise QbitError("qBittorrent 没有返回 WebAPI 版本")
        status, app = self.call("GET", "/api/v2/app/version")
        if status != 200:
            raise QbitError("qBittorrent 没有返回版本号")
        return _plain(web), _plain(app)

    def ensure_category(self, name: str) -> None:
        if not name:
            return
        status, _body = self.call("POST", "/api/v2/torrents/createCategory", data={"category": name})
        if status not in {200, 409}:
            raise QbitError("qBittorrent 没有建好分类")

    def add(self, magnet: Magnet, category: str, save_path: str) -> str:
        fields = {"urls": magnet.uri}
        if save_path:
            fields["savepath"] = save_path
            fields["autoTMM"] = "false"
        if category:
            fields["category"] = category
        status, body = self.call("POST", "/api/v2/torrents/add", data=fields)
        return _added(status, body)


def _session_cookie(cookies: httpx.Cookies) -> bool:
    """5.2 起会话 cookie 按端口命名，避免同一台机器上的多个 WebUI 互相覆盖。"""
    return any(cookie.name == "SID" or cookie.name.startswith("QBT_SID_")
               for cookie in cookies.jar)


def _login_accepted(status: int, text: str, cookies: httpx.Cookies) -> bool:
    if not _session_cookie(cookies):
        return False
    if status == 204:
        return text == ""
    return status == 200 and text in {"Ok.", "Ok"}


def _plain(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip().strip('"')
    if not text or len(text) > 40 or any(char in text for char in "<>\r\n"):
        raise QbitError("qBittorrent 没有返回版本号")
    return text


def _count(payload: dict, key: str) -> int:
    value = payload.get(key, 0)
    if type(value) is not int or not 0 <= value <= 1000:
        raise QbitError("qBittorrent 的添加结果无法识别")
    return value


def _added(status: int, body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    if status == 200 and text in {"Ok.", "Ok", ""}:
        return "ok"
    if text == "Fails.":
        raise QbitError("qBittorrent 没有接受这个磁力")
    payload = None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict) and "success_count" in payload:
        success, pending = _count(payload, "success_count"), _count(payload, "pending_count")
        if success or pending:
            return "pending" if pending and not success else "ok"
        raise QbitError("qBittorrent 没有接受这个磁力")
    if status == 202:
        return "pending"
    raise QbitError("qBittorrent 没有接受这个磁力")


def check_connection(service, body: dict | None = None, *, transport=None) -> dict:
    """按表单上此刻的值登录并读版本。地址未改且密码留空时，用已保存的密码。不写设置。"""
    config = _settings(service).load()
    secrets = _secrets(service.credentials)
    form = body if isinstance(body, dict) else {}
    url = clean_base_url(form["url"] if "url" in form else config.url)
    username = clean_username(form["username"] if "username" in form else secrets.get("username", ""))
    typed = clean_password(form.get("password"))
    if typed:
        password = typed
    elif url == config.url:
        password = secrets.get("password", "")
    else:
        password = ""
    empty = {"ok": False, "version": "", "webapi_version": "", "message": ""}
    if not url or not username or not password:
        empty["message"] = "请填写 qBittorrent 地址、用户名和密码"
        return empty
    session = _Session(url, transport)
    try:
        session.login(username, password)
        webapi, version = session.versions()
        return {"ok": True, "version": version, "webapi_version": webapi, "message": ""}
    except QbitError as error:
        empty["message"] = error.detail
        return empty
    finally:
        session.logout()
        session.close()


def add_magnet(service, body: dict, *, transport=None) -> dict:
    if not isinstance(body, dict):
        raise ValueError("请求体必须是一个对象")
    config = _settings(service).load()
    secrets = _secrets(service.credentials)
    username, password = secrets.get("username", ""), secrets.get("password", "")
    if not config.enabled or not config.url or not username or not password:
        raise ValueError("先在下载渠道保存并启用 qBittorrent")
    magnet = submission_magnet(str(body.get("magnet") or ""))
    session = _Session(config.url, transport)
    try:
        session.login(username, password)
        session.ensure_category(config.category)
        outcome = session.add(magnet, config.category, config.save_path)
    except QbitError as error:
        raise ValueError(error.detail) from None
    finally:
        session.logout()
        session.close()
    return {"ok": True, "info_hash": magnet.info_hash, "pending": outcome == "pending"}
