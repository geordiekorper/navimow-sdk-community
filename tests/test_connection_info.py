"""MqttConnectionInfo, MowerAPI.async_get_mqtt_connection_info and NavimowSDK.from_connection_info.

The credential reply is read once, in MqttConnectionInfo.from_dict: the host,
the port, the WebSocket path and the credentials, in the shapes the reply has
been seen in and the ones a reader has to settle. The factory builds the
facade from it on a recording fake paho client, so nothing connects.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import mower_sdk
from mower_sdk import models
from mower_sdk import mqtt as mqtt_module
from mower_sdk.api import MowerAPI
from mower_sdk.errors import MowerAPIError
from mower_sdk.models import Device, MqttConnectionInfo
from mower_sdk.sdk import NavimowSDK

HOST = "broker.example.invalid"


def read(**reply: Any) -> MqttConnectionInfo:
    return MqttConnectionInfo.from_dict(reply)


# ---- reading the reply ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "endpoint"),
    [
        ({"mqttHost": f"wss://{HOST}", "mqttUrl": "/mqtt/12345"}, (HOST, 443, "/mqtt/12345")),
        ({"mqttHost": HOST, "mqttUrl": "/mqtt/12345"}, (HOST, 443, "/mqtt/12345")),
        ({"mqttHost": f"{HOST}:8443", "mqttUrl": "/mqtt"}, (HOST, 8443, "/mqtt")),
        ({"mqttHost": f"wss://{HOST}:8443", "mqttUrl": "/mqtt"}, (HOST, 8443, "/mqtt")),
        ({"mqttHost": f"WSS://{HOST.upper()}", "mqttUrl": "/mqtt"}, (HOST, 443, "/mqtt")),
        ({"mqttHost": f"  wss://{HOST}/ignored  ", "mqttUrl": " mqtt/12345 "}, (HOST, 443, "/mqtt/12345")),
        # A full mqttUrl: its path and query are the path, and its port wins over mqttHost's.
        ({"mqttHost": f"{HOST}:8443", "mqttUrl": f"wss://{HOST}:9443/mqtt/1?a=b"}, (HOST, 9443, "/mqtt/1?a=b")),
        ({"mqttHost": f"{HOST}:8443", "mqttUrl": f"wss://{HOST}/mqtt/1"}, (HOST, 8443, "/mqtt/1")),
        # mqttHost names the host even when a full mqttUrl names another.
        ({"mqttHost": HOST, "mqttUrl": "wss://other.example.invalid/mqtt"}, (HOST, 443, "/mqtt")),
        # Without mqttHost, a full mqttUrl names the host.
        ({"mqttUrl": f"wss://{HOST}/mqtt"}, (HOST, 443, "/mqtt")),
        ({"mqttHost": "", "mqttUrl": f"wss://{HOST}:9443/mqtt"}, (HOST, 9443, "/mqtt")),
        # mqttUrl missing, empty, not text, or a URL without a path: no path.
        ({"mqttHost": HOST}, (HOST, 443, "")),
        ({"mqttHost": HOST, "mqttUrl": ""}, (HOST, 443, "")),
        ({"mqttHost": HOST, "mqttUrl": None}, (HOST, 443, "")),
        ({"mqttHost": HOST, "mqttUrl": 7}, (HOST, 443, "")),
        ({"mqttHost": HOST, "mqttUrl": f"wss://{HOST}"}, (HOST, 443, "")),
        ({"mqttHost": "[2001:db8::1]:8443", "mqttUrl": "/mqtt"}, ("2001:db8::1", 8443, "/mqtt")),
        # A path is a path, however it starts, and names neither a host nor a port.
        ({"mqttHost": HOST, "mqttUrl": "//mqtt/12345"}, (HOST, 443, "//mqtt/12345")),
        ({"mqttHost": HOST, "mqttUrl": "other.example.invalid:9443/mqtt"}, (HOST, 443, "/other.example.invalid:9443/mqtt")),
        ({"mqttHost": HOST, "mqttUrl": "/mqtt?a=b"}, (HOST, 443, "/mqtt?a=b")),
        # A full URL's query is kept when its path is empty.
        ({"mqttHost": HOST, "mqttUrl": f"wss://{HOST}?a=b"}, (HOST, 443, "/?a=b")),
    ],
)
def test_the_host_port_and_path_are_read_from_each_shape(reply: dict[str, Any], endpoint: tuple[Any, ...]) -> None:
    info = MqttConnectionInfo.from_dict(reply)
    assert (info.broker, info.port, info.ws_path) == endpoint


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"mqttHost": ""},
        {"mqttHost": "   "},
        {"mqttHost": None},
        {"mqttHost": 42},
        {"mqttHost": "wss://"},
        {"mqttUrl": "/mqtt/12345"},
        {"userName": "user", "pwdInfo": "secret"},
    ],
    ids=["empty", "empty_host", "blank_host", "null_host", "number_host", "scheme_only", "path_only", "credentials_only"],
)
def test_a_reply_that_names_no_broker_is_an_api_error(reply: dict[str, Any]) -> None:
    with pytest.raises(MowerAPIError, match=r"no broker \(mqttHost\)"):
        MqttConnectionInfo.from_dict(reply)


@pytest.mark.parametrize("reply", [None, [], "wss://broker", 1])
def test_a_reply_that_is_not_an_object_is_an_api_error(reply: Any) -> None:
    with pytest.raises(MowerAPIError, match="not an object"):
        MqttConnectionInfo.from_dict(reply)


@pytest.mark.parametrize(
    ("reply", "key"),
    [
        ({"mqttHost": f"{HOST}:abc"}, "mqttHost"),
        ({"mqttHost": f"wss://{HOST}:70000"}, "mqttHost"),
        ({"mqttHost": HOST, "mqttUrl": f"wss://{HOST}:port/mqtt"}, "mqttUrl"),
    ],
)
def test_a_port_that_is_not_a_number_is_an_api_error_naming_the_key(reply: dict[str, Any], key: str) -> None:
    with pytest.raises(MowerAPIError, match=f"{key} cannot be read as a host and port") as caught:
        MqttConnectionInfo.from_dict(reply)
    assert HOST not in str(caught.value)


@pytest.mark.parametrize("key", ["mqttHost", "mqttUrl"])
def test_a_host_that_cannot_be_split_is_an_api_error_that_does_not_show_the_value(key: str) -> None:
    reply = {"mqttHost": HOST, key: "wss://[account-secret]/mqtt"}
    with pytest.raises(MowerAPIError, match=f"{key} cannot be read as a host and port") as caught:
        MqttConnectionInfo.from_dict(reply)
    assert "account-secret" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize(
    ("reply", "key", "scheme"),
    [
        ({"mqttHost": f"tcp://{HOST}"}, "mqttHost", "tcp"),
        ({"mqttHost": f"ws://{HOST}"}, "mqttHost", "ws"),
        ({"mqttHost": f"mqtts://{HOST}:8883"}, "mqttHost", "mqtts"),
        ({"mqttHost": HOST, "mqttUrl": f"ws://{HOST}/mqtt?token=secret"}, "mqttUrl", "ws"),
    ],
)
def test_a_scheme_other_than_wss_is_an_api_error_that_does_not_show_the_value(
    reply: dict[str, Any], key: str, scheme: str
) -> None:
    with pytest.raises(MowerAPIError, match=f"{key} uses the scheme '{scheme}'") as caught:
        MqttConnectionInfo.from_dict(reply)
    assert HOST not in str(caught.value) and "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("reply", "credentials"),
    [
        ({"userName": "user", "pwdInfo": "secret"}, ("user", "secret")),
        ({"userName": 12345, "pwdInfo": 678}, ("12345", "678")),
        ({"userName": "", "pwdInfo": ""}, ("", "")),
        ({"userName": "user"}, ("user", None)),
        ({"pwdInfo": "secret"}, (None, "secret")),
        ({"userName": None, "pwdInfo": None}, (None, None)),
        ({}, (None, None)),
    ],
)
def test_the_credentials_are_text_when_present_and_none_when_absent(
    reply: dict[str, Any], credentials: tuple[str | None, str | None]
) -> None:
    info = MqttConnectionInfo.from_dict({"mqttHost": HOST, **reply})
    assert (info.username, info.password) == credentials


def test_the_password_and_the_reply_stay_out_of_repr_and_the_reply_out_of_equality() -> None:
    reply = {"mqttHost": f"wss://{HOST}", "mqttUrl": "/mqtt/12345", "userName": "user", "pwdInfo": "secret"}
    info = MqttConnectionInfo.from_dict(reply)
    assert "secret" not in repr(info)
    assert "user" in repr(info)
    assert info.raw == reply and info.raw is not reply
    assert info == MqttConnectionInfo.from_dict({**reply, "subTopics": ["a"]})
    assert hash(info) == hash(MqttConnectionInfo.from_dict(reply))


def test_mqtt_connection_info_is_exported_from_models_and_the_package() -> None:
    assert mower_sdk.MqttConnectionInfo is models.MqttConnectionInfo
    assert "MqttConnectionInfo" in mower_sdk.__all__
    assert "MqttConnectionInfo" in models.__all__


# ---- the API call -------------------------------------------------------------------------------


class ReplyingAPI(MowerAPI):
    def __init__(self, reply: Any) -> None:
        super().__init__(session=None, token="token", base_url="https://api.example.invalid")  # type: ignore[arg-type]
        self.reply = reply

    async def async_get_mqtt_user_info(self) -> Any:
        return self.reply


def test_the_api_reads_the_credential_reply_into_connection_info() -> None:
    reply = {"mqttHost": f"wss://{HOST}", "mqttUrl": "/mqtt/12345", "userName": 12345, "pwdInfo": "secret"}
    info = asyncio.run(ReplyingAPI(reply).async_get_mqtt_connection_info())
    assert info == MqttConnectionInfo(HOST, 443, "/mqtt/12345", "12345", "secret")


def test_the_api_raises_for_a_reply_without_a_broker() -> None:
    with pytest.raises(MowerAPIError, match="no broker"):
        asyncio.run(ReplyingAPI({"userName": "user"}).async_get_mqtt_connection_info())


# ---- the factory --------------------------------------------------------------------------------


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [("__init__", args, kwargs)]
        FakeClient.instances.append(self)

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record

    def is_connected(self) -> bool:
        return False

    def named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]


@pytest.fixture(autouse=True)
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    monkeypatch.setattr(mqtt_module.mqtt_client, "Client", FakeClient)


INFO = MqttConnectionInfo(broker=HOST, port=443, ws_path="/mqtt/12345", username="user", password="secret")
RECORDS = [Device(id="dev-1", name="Mower", model="X430", firmware_version="1.0", serial_number="SN1")]


def test_the_factory_builds_tls_over_websocket_with_the_bearer() -> None:
    sdk = NavimowSDK.from_connection_info(INFO, access_token="tok", records=RECORDS)
    mqtt = sdk.mqtt
    assert (mqtt.broker, mqtt.port, mqtt.ws_path, mqtt.username, mqtt.password) == (
        HOST,
        443,
        "/mqtt/12345",
        "user",
        "secret",
    )
    assert mqtt.auth_headers == {"Authorization": "Bearer tok"}
    assert mqtt.records == RECORDS
    client = mqtt.client
    assert client.calls[0][2]["transport"] == "websockets"
    assert client.named("tls_set") == [((), {})]
    assert client.named("username_pw_set") == [(("user", "secret"), {})]
    assert client.named("ws_set_options") == [((), {"path": "/mqtt/12345", "headers": {"Authorization": "Bearer tok"}})]


def test_the_bearer_is_merged_into_the_callers_headers_and_replaces_their_authorization() -> None:
    headers = {"authorization": "Bearer old", "X-Client": "app"}
    sdk = NavimowSDK.from_connection_info(INFO, access_token="tok", records=[], auth_headers=headers)
    assert sdk.mqtt.auth_headers == {"X-Client": "app", "Authorization": "Bearer tok"}
    assert headers == {"authorization": "Bearer old", "X-Client": "app"}  # the caller's dict is not changed


def test_without_an_access_token_no_authorization_is_added_and_the_headers_are_kept() -> None:
    sdk = NavimowSDK.from_connection_info(INFO, access_token=None, records=[], auth_headers={"X-Client": "app"})
    assert sdk.mqtt.auth_headers == {"X-Client": "app"}
    bare = NavimowSDK.from_connection_info(INFO, access_token=None, records=[])
    assert bare.mqtt.auth_headers == {}


def test_other_options_reach_the_constructor() -> None:
    async def test() -> None:
        loop = asyncio.get_running_loop()
        sdk = NavimowSDK.from_connection_info(
            INFO,
            access_token="tok",
            records=RECORDS,
            loop=loop,
            keepalive_seconds=120,
            subscribe_location=True,
            extra_topics=["a/b"],
            reject_late_state=True,
        )
        assert sdk.loop is loop
        assert sdk.mqtt.keepalive_seconds == 120
        assert (sdk.mqtt.subscribe_location, sdk.mqtt.extra_topics) == (True, ["a/b"])
        assert sdk._reject_late_state is True

    asyncio.run(test())


@pytest.mark.parametrize("name", ["broker", "port", "ws_path", "username", "password"])
def test_an_option_the_connection_info_supplies_is_a_type_error(name: str) -> None:
    with pytest.raises(TypeError, match=f"takes {name} from the connection info"):
        NavimowSDK.from_connection_info(INFO, access_token="tok", records=[], **{name: "x"})
    assert FakeClient.instances == []


def test_an_unknown_option_is_the_constructors_type_error() -> None:
    with pytest.raises(TypeError, match="no_such_option"):
        NavimowSDK.from_connection_info(INFO, access_token="tok", records=[], no_such_option=1)


def test_connection_info_without_a_websocket_path_is_refused() -> None:
    info = MqttConnectionInfo.from_dict({"mqttHost": HOST, "userName": "user", "pwdInfo": "secret"})
    with pytest.raises(ValueError, match="no WebSocket path"):
        NavimowSDK.from_connection_info(info, access_token="tok", records=[])
    assert FakeClient.instances == []


def test_the_factory_takes_the_readers_port() -> None:
    info = MqttConnectionInfo.from_dict({"mqttHost": HOST, "mqttUrl": f"wss://{HOST}:9443/mqtt?a=b"})
    sdk = NavimowSDK.from_connection_info(info, access_token="tok", records=[])
    assert (sdk.mqtt.broker, sdk.mqtt.port, sdk.mqtt.ws_path) == (HOST, 9443, "/mqtt?a=b")
