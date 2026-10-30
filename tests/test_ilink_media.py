import base64
import hashlib

import httpx
import pytest

from wechat_assistant.adapters.ilink_media import (
    ILinkMedia,
    decrypt,
    encrypt,
    parse_key,
    validate_cdn_url,
)


def test_crypto_matches_aes_known_vector_and_both_wire_key_encodings():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    content = bytes.fromhex("00112233445566778899aabbccddeeff")
    ciphertext = encrypt(content, key)
    assert ciphertext[:16].hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert len(ciphertext) == 32
    assert decrypt(ciphertext, key) == content
    assert decrypt(encrypt(b"", key), key) == b""
    for encoded in [key, key.hex().encode()]:
        assert parse_key(base64.b64encode(encoded).decode()) == key
    with pytest.raises(ValueError):
        decrypt(ciphertext[:16], key)
    for encoded in ["bad!", base64.b64encode(b"g" * 32).decode()]:
        with pytest.raises(ValueError):
            parse_key(encoded)


@pytest.mark.parametrize(
    "url",
    [
        "http://novac2c.cdn.weixin.qq.com/c2c/download",
        "https://novac2c.cdn.weixin.qq.com.evil.test/file",
        "https://evil.test/a",
        "https://127.0.0.1/file",
        "https://user@novac2c.cdn.weixin.qq.com/file",
        "https://novac2c.cdn.weixin.qq.com:444/file",
        "https://novac2c.cdn.weixin.qq.com/file#secret",
    ],
)
def test_cdn_rejects_extra_gateways_and_local_urls(url):
    with pytest.raises(ValueError):
        validate_cdn_url(url)


def test_cdn_encrypted_download_length_md5_and_limit():
    content, key = "中文文件正文".encode(), bytes(range(16))
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.params["encrypted_query_param"] == "a&b+=="
        assert "authorization" not in request.headers
        return httpx.Response(200, content=encrypt(content, key))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        media = ILinkMedia(None, 1000, client)
        item = {
            "len": str(len(content)),
            "md5": hashlib.md5(content).hexdigest(),
            "media": {"encrypt_query_param": "a&b+==", "aes_key": base64.b64encode(key).decode()},
        }
        assert media.download(item) == content
        for bad in [{**item, "len": "999"}, {**item, "md5": "0" * 32}]:
            with pytest.raises(ValueError):
                media.download(bad)
        media.max_bytes = 2
        with pytest.raises(ValueError):
            media.download(item)
        assert len(calls) == 3  # Declared oversize is rejected before any request.


@pytest.mark.parametrize("status,content", [(302, b""), (200, b"x" * 100)])
def test_download_does_not_follow_redirect_or_accept_oversize(status, content):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, content=content, headers={"location": "https://evil.test/"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        media = ILinkMedia(None, 8, client)
        with pytest.raises(ValueError):
            media.download({"media": {"full_url": "https://novac2c.cdn.weixin.qq.com/file"}})
    assert len(calls) == 1


def test_official_upload_encrypts_before_cdn_and_builds_file_item():
    requests = []
    upload_body = {}
    content = b"test file"

    def post(path, body):
        assert path == "/ilink/bot/getuploadurl"
        upload_body.update(body)
        return {"upload_param": "secret&param"}

    def handle(request):
        requests.append(request)
        assert request.method == "POST"
        assert "authorization" not in request.headers
        assert request.url.params["encrypted_query_param"] == "secret&param"
        assert decrypt(request.content, bytes.fromhex(upload_body["aeskey"])) == content
        return httpx.Response(200, headers={"x-encrypted-param": "download-param"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        item = ILinkMedia(post, 1000, client).upload("owner", "file.txt", content)
    assert upload_body["media_type"] == 3
    assert upload_body["rawsize"] == len(content)
    assert upload_body["filesize"] == 16
    assert upload_body["rawfilemd5"] == hashlib.md5(content).hexdigest()
    assert item["type"] == 4
    assert item["file_item"]["len"] == str(len(content))
    assert parse_key(item["file_item"]["media"]["aes_key"]) == bytes.fromhex(upload_body["aeskey"])
    assert len(requests) == 1
