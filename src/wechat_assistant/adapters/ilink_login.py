"""Workspace-scoped official iLink QR binding, independent of the bot runtime."""

import base64
import json
import os
import secrets
import time

import httpx

from ..models import NotSent
from .ilink import validate_ilink_base


class ILinkLogin:
    def __init__(self, settings, client=None):
        self.directory = settings.data_dir / "credentials"
        if self.directory.is_symlink():
            raise ValueError("ClawBot凭证目录不能是符号链接")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "ilink.json"
        self.pending = self.directory / "ilink-login.json"
        self.client = client or httpx.Client(timeout=40, trust_env=False, follow_redirects=False)

    def _request(self, method, base, path, **kwargs):
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(0x020408)}
        if method == "POST":
            headers.update(
                {
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
                }
            )
        try:
            response = self.client.request(
                method, validate_ilink_base(base) + path, headers=headers, **kwargs
            )
            if response.status_code != 200:
                raise NotSent(f"腾讯ClawBot绑定返回 HTTP {response.status_code}")
            data = response.json()
        except httpx.HTTPError:
            raise NotSent("腾讯ClawBot绑定连接失败；登录票据未输出") from None
        except ValueError:
            raise NotSent("腾讯ClawBot绑定响应无效") from None
        if not isinstance(data, dict):
            raise NotSent("腾讯ClawBot绑定响应格式不支持")
        return data

    @staticmethod
    def _save(path, value):
        if path.is_symlink():
            raise ValueError("ClawBot凭证文件不能是符号链接")
        temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                temporary.chmod(0o600)
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def begin(self):
        if self.path.exists():
            raise NotSent("该工作区已有 ClawBot 绑定，不会自动替换")
        base = "https://ilinkai.weixin.qq.com"
        data = self._request(
            "POST", base, "/ilink/bot/get_bot_qrcode?bot_type=3", json={"local_token_list": []}
        )
        code, payload = data.get("qrcode"), data.get("qrcode_img_content")
        if not isinstance(code, str) or not code or not isinstance(payload, str) or not payload:
            raise NotSent("腾讯未返回有效的ClawBot绑定二维码")
        # Encode the QR payload locally. Never use a third-party QR image service.
        import qrcode

        image_path = self.directory / "ilink-login-qr.png"
        if image_path.is_symlink():
            raise ValueError("二维码文件不能是符号链接")
        qrcode.make(payload).save(image_path)
        image_path.chmod(0o600)
        self._save(self.pending, {"qrcode": code, "baseurl": base, "started_at": time.time()})
        return image_path

    def check(self, verify_code=None):
        if not self.pending.is_file() or self.pending.is_symlink():
            raise NotSent("没有工作区内待确认的 ClawBot 二维码")
        pending = json.loads(self.pending.read_text())
        if time.time() - pending["started_at"] > 300:
            raise NotSent("ClawBot 二维码已过期，请重新运行 ilink-login")
        params = {"qrcode": pending["qrcode"]}
        if verify_code:
            params["verify_code"] = verify_code
        data = self._request(
            "GET", pending["baseurl"], "/ilink/bot/get_qrcode_status", params=params
        )
        state = data.get("status")
        if state == "scaned_but_redirect":
            host = data.get("redirect_host", "")
            base = host if host.startswith("https://") else "https://" + host
            pending["baseurl"] = validate_ilink_base(base)
            self._save(self.pending, pending)
            return "wait"
        if state == "confirmed":
            if self.path.exists():
                raise NotSent("绑定已存在，不自动覆盖；请停止其他绑定进程")
            if not all(
                isinstance(data.get(key), str) and data[key]
                for key in ("bot_token", "ilink_bot_id", "ilink_user_id")
            ):
                raise NotSent("ClawBot绑定凭证不完整")
            credentials = {key: data[key] for key in ("bot_token", "ilink_bot_id", "ilink_user_id")}
            credentials["baseurl"] = validate_ilink_base(data.get("baseurl", pending["baseurl"]))
            self._save(self.path, credentials)
            self.pending.unlink()
            (self.directory / "ilink-login-qr.png").unlink(missing_ok=True)
            return "confirmed"
        if state in {"wait", "scaned", "need_verifycode"}:
            return state
        raise NotSent("腾讯ClawBot绑定未完成或已过期；不自动替换现有账号")

    def close(self):
        self.client.close()
