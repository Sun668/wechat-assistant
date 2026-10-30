"""Owner-only iLink text/file transport, based on Tencent's published protocol.

User completes QR login separately and imports credentials. No automatic account
switching, credential discovery or permissive parsing of owner identity.
"""

import base64
import hashlib
import json
import secrets
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..models import FilePayload, NotSent, Receipt, SendUncertain


def validate_ilink_base(base):
    parsed = urlparse(base)
    host = parsed.hostname or ""
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (
        parsed.scheme != "https"
        or not valid_port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not (host == "weixin.qq.com" or host.endswith(".weixin.qq.com"))
    ):
        raise ValueError("iLink 地址必须是腾讯微信 HTTPS 服务，不能使用额外网关")
    return base.rstrip("/")


class ILink:
    def __init__(
        self,
        credential_file: Path,
        store,
        owner_id: str,
        client=None,
        event_sink=None,
    ):
        self.path, self.store, self.owner_id = credential_file, store, owner_id
        # A project gateway consumes entire events in original batch order. In particular,
        # a project-switch text must commit before the next event's file is assigned.
        self.event_sink = event_sink
        credentials = json.loads(credential_file.read_text())
        self.token = credentials["bot_token"]
        if credentials.get("ilink_user_id", owner_id) != owner_id:
            raise ValueError("iLink 扫码绑定人与配置操作者不一致")
        self.base = validate_ilink_base(credentials.get("baseurl", "https://ilinkai.weixin.qq.com"))
        self.client = client or httpx.Client(timeout=45, trust_env=False, follow_redirects=False)
        self.media = None

    def _post(self, path, body):
        response = self.client.post(
            self.base + path,
            json={
                **body,
                "base_info": {"channel_version": "2.4.8", "bot_agent": "ClawBotProjects/0.3"},
            },
            headers={
                "AuthorizationType": "ilink_bot_token",
                "Authorization": "Bearer " + self.token,
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
                "iLink-App-Id": "bot",
                "iLink-App-ClientVersion": str(0x020408),
            },
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("iLink 返回了无效的协议结果")
        if path == "/ilink/bot/sendmessage":
            self.store.set_value(
                "ilink_last_send_result",
                json.dumps(
                    {
                        "http_status": response.status_code,
                        "ret_present": "ret" in result,
                        "ret": result.get("ret") if type(result.get("ret")) is int else None,
                        "errcode_present": "errcode" in result,
                        "errcode": result.get("errcode")
                        if type(result.get("errcode")) is int
                        else None,
                        "empty_result": not result,
                    }
                ),
            )
        if result.get("ret", 0) != 0 or result.get("errcode", 0) != 0:
            # Never include server messages that might echo credentials.
            raise RuntimeError(f"iLink 业务错误 {result.get('ret', result.get('errcode'))}")
        return result

    def poll(self) -> int:
        if self.event_sink is None:
            raise ValueError("尚未配置 ClawBot 项目事件接收器")
        # Drain the persisted batch before advancing the remote cursor.
        batch = json.loads(self.store.get_value("ilink_pending", "[]"))
        if not batch:
            result = self._post(
                "/ilink/bot/getupdates", {"get_updates_buf": self.store.get_value("ilink_cursor")}
            )
            batch = result.get("msgs", [])
            with self.store.transaction():
                self.store.set_value("ilink_pending", json.dumps(batch, ensure_ascii=False))
                cursor = result.get("get_updates_buf")
                if cursor:
                    self.store.set_value("ilink_cursor", cursor)
        accepted = 0
        for raw in batch:
            if raw.get("from_user_id") != self.owner_id or raw.get("group_id"):
                continue
            self.store.remember_source_event("ilink", str(raw.get("message_id", "")), raw)
            token = raw.get("context_token")
            if token:
                self.store.set_value("ilink_context:" + self.owner_id, token)
            self.event_sink(raw)
            accepted += 1
        return accepted

    def acknowledge_poll(self):
        self.store.set_value("ilink_pending", "[]")

    def can_notify(self):
        return bool(self.store.get_value("ilink_context:" + self.owner_id))

    def send(self, chat_id: str, text: str, client_id: str) -> Receipt:
        return self._send_items(chat_id, [{"type": 1, "text_item": {"text": text}}], client_id)

    def send_file(self, chat_id: str, file: FilePayload, content: bytes, client_id: str) -> Receipt:
        self._context(chat_id)
        if self.media is None:
            raise NotSent("附件适配器未配置")
        if len(content) != file.size or hashlib.sha256(content).hexdigest() != file.sha256:
            raise NotSent("文件内容与授权快照不一致")
        try:
            item = self.media.upload(chat_id, file.name, content)
        except Exception as exc:
            # CDN upload is not a chat send; no sendmessage request has been made yet.
            raise NotSent("附件上传未完成，尚未发送聊天消息；请人工检查") from exc
        return self._send_items(chat_id, [item], client_id)

    def _context(self, chat_id):
        if chat_id != self.owner_id:
            raise NotSent("iLink 仅允许向绑定的操作者发送")
        context = self.store.get_value("ilink_context:" + self.owner_id)
        if not context:
            raise NotSent("尚无会话上下文，请先给 ClawBot 发一条消息")
        return context

    def _send_items(self, chat_id, items, client_id):
        context = self._context(chat_id)
        try:
            result = self._post(
                "/ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": chat_id,
                        "client_id": client_id,
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": context,
                        "item_list": items,
                    }
                },
            )
            # Tencent's published client accepts a successful HTTP JSON response
            # without `ret`; nonzero `ret`/`errcode` has already been rejected.
        except Exception as exc:
            raise SendUncertain("iLink 请求未能确认成功；请核对会话") from exc
        # Use an explicit server ID when provided; otherwise quote matching still
        # requires the complete random marker and never assumes client_id == svr_id.
        server_id = result.get("message_id") or result.get("msg_id")
        return Receipt(
            external_id=str(server_id) if server_id is not None else client_id, verified=True
        )

    def close(self):
        self.client.close()
        if self.media:
            self.media.client.close()
