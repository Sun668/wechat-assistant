"""Tencent CDN file protocol only; no bot loop, proxies or third-party gateway.

Wire compatibility: Tencent/openclaw-weixin src/cdn/{upload,pic-decrypt,cdn-url}.ts.
AES-128-ECB/PKCS7 is required by that protocol, not a new storage encryption scheme.
"""

import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


def validate_cdn_url(url: str) -> str:
    parsed = urlsplit(url)
    try:
        port_ok = parsed.port in (None, 443)
    except ValueError:
        port_ok = False
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not port_ok
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not host.endswith(".cdn.weixin.qq.com")
        or len(url) > 16384
    ):
        raise ValueError("附件地址必须是腾讯微信 CDN 的 HTTPS 地址")
    return url


def parse_key(encoded: str) -> bytes:
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) == 32:
            decoded = bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) == 16:
            return decoded
    except (ValueError, UnicodeError):
        pass
    raise ValueError("附件加密密钥格式无效")


def encrypt(content: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(content) + padder.finalize()
    encoder = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encoder.update(padded) + encoder.finalize()


def decrypt(content: bytes, key: bytes) -> bytes:
    decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decoder.update(content) + decoder.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


class ILinkMedia:
    def __init__(self, post, max_bytes: int, client=None):
        self.post, self.max_bytes = post, max_bytes
        # Separate pool: long polling must not hold up attachments or approvals.
        self.client = client or httpx.Client(
            timeout=45,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )

    def download(self, item: dict) -> bytes:
        media = item.get("media") or {}
        declared = item.get("len")
        if declared is not None and not 0 <= int(declared) <= self.max_bytes:
            raise ValueError("附件超过工作区文件大小限制")
        url = media.get("full_url")
        if not url:
            param = media.get("encrypt_query_param")
            if not isinstance(param, str) or not param:
                raise ValueError("附件缺少下载地址")
            url = CDN_BASE + "/download?" + urlencode({"encrypted_query_param": param})
        url = validate_cdn_url(url)
        key = parse_key(media["aes_key"]) if media.get("aes_key") else None
        if media.get("encrypt_type") not in (None, 0, 1):
            raise ValueError("不支持的附件加密类型")
        if media.get("encrypt_type") == 1 and key is None:
            raise ValueError("加密附件缺少密钥")
        encrypted = bytearray()
        # Bounded stream; status redirects and excessive/decompressed data are rejected.
        with self.client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"腾讯附件下载失败（HTTP {response.status_code}）")
            length = response.headers.get("content-length")
            if length and int(length) > self.max_bytes + 16:
                raise ValueError("附件超过工作区文件大小限制")
            for part in response.iter_bytes(65536):
                encrypted.extend(part)
                if len(encrypted) > self.max_bytes + 16:
                    raise ValueError("附件超过工作区文件大小限制")
        content = decrypt(bytes(encrypted), key) if key else bytes(encrypted)
        if len(content) > self.max_bytes:
            raise ValueError("附件超过工作区文件大小限制")
        if declared is not None and len(content) != int(declared):
            raise ValueError("附件长度校验不一致")
        if item.get("md5") and hashlib.md5(content).hexdigest() != item["md5"].lower():
            raise ValueError("附件完整性校验失败")
        return content

    def upload(self, chat_id: str, name: str, content: bytes) -> dict:
        if len(content) > self.max_bytes:
            raise ValueError("附件超过工作区文件大小限制")
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        ciphertext = encrypt(content, key)
        result = self.post(
            "/ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": 3,
                "to_user_id": chat_id,
                "rawsize": len(content),
                "rawfilemd5": hashlib.md5(content).hexdigest(),
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": key.hex(),
            },
        )
        url = result.get("upload_full_url")
        if not url:
            param = result.get("upload_param")
            if not isinstance(param, str) or not param:
                raise ValueError("腾讯未返回附件上传地址")
            url = (
                CDN_BASE
                + "/upload?"
                + urlencode({"encrypted_query_param": param, "filekey": filekey})
            )
        # No bearer token is sent to the CDN; no automatic retry of any upload/send.
        response = self.client.post(
            validate_cdn_url(url),
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
        )
        param = response.headers.get("x-encrypted-param")
        if response.status_code != 200 or not param:
            raise ValueError("腾讯附件上传未确认成功；尚未发送聊天消息")
        return {
            "type": 4,
            "file_item": {
                "media": {
                    "encrypt_query_param": param,
                    "aes_key": base64.b64encode(key.hex().encode()).decode(),
                    "encrypt_type": 1,
                },
                "file_name": name,
                "len": str(len(content)),
            },
        }
