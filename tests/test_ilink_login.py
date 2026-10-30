import json
import stat
import time

import httpx
import pytest

from wechat_assistant.adapters.ilink import ILink, validate_ilink_base
from wechat_assistant.adapters.ilink_login import ILinkLogin
from wechat_assistant.config import Settings
from wechat_assistant.models import NotSent


@pytest.mark.parametrize(
    "url",
    [
        "https://proxy.example/",
        "https://weixin.qq.com.attacker.org/",
        "http://ilinkai.weixin.qq.com",
        "https://ilinkai.weixin.qq.com:8443",
        "https://user@ilinkai.weixin.qq.com",
        "https://ilinkai.weixin.qq.com/api",
    ],
)
def test_ilink_rejects_external_gateway(url):
    with pytest.raises(ValueError):
        validate_ilink_base(url)


def test_workspace_scoped_official_binding_encodes_qr_locally(tmp_path):
    settings = Settings(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "ilinkai.weixin.qq.com"
        assert "authorization" not in request.headers
        if request.method == "POST":
            assert json.loads(request.content) == {"local_token_list": []}
            return httpx.Response(
                200,
                json={
                    "qrcode": "local-qr",
                    "qrcode_img_content": "https://weixin.qq.com/x/fixture",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "confirmed",
                "bot_token": "PRIVATE_TOKEN",
                "ilink_bot_id": "bot-one",
                "ilink_user_id": "owner",
                "baseurl": "https://ilinkai.weixin.qq.com",
            },
        )

    login = ILinkLogin(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        qr = login.begin()
        assert qr.read_bytes().startswith(b"\x89PNG")
        assert len(calls) == 1  # no QR generation/download website
        assert login.check() == "confirmed"
        credentials = json.loads(login.path.read_text())
        assert credentials["ilink_user_id"] == "owner"
        assert stat.S_IMODE(login.path.stat().st_mode) == 0o600
        assert not qr.exists() and not login.pending.exists()
        with pytest.raises(NotSent, match="已有 ClawBot 绑定"):
            login.begin()
        with pytest.raises(ValueError, match="绑定人与配置操作者不一致"):
            ILink(login.path, None, "other")
    finally:
        login.close()


def test_bind_redirect_cannot_send_ticket_to_extra_gateway(tmp_path):
    settings = Settings(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant")
    login = ILinkLogin(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, json={"status": "scaned_but_redirect", "redirect_host": "vendor.example"}
                )
            )
        ),
    )
    try:
        login._save(
            login.pending,
            {
                "qrcode": "secret",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "started_at": time.time(),
            },
        )
        with pytest.raises(ValueError):
            login.check()
        assert not login.path.exists()
    finally:
        login.close()
