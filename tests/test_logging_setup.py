import os
import subprocess
import sys
from pathlib import Path


def test_http_logging_does_not_expose_login_query_in_console_or_file(tmp_path):
    script = """
import logging,sys
from pathlib import Path
import httpx
from wechat_assistant.logging_setup import configure_logging
configure_logging(Path(sys.argv[1]))
with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
    client.get('https://wx.qq.com/example?pass_ticket=PRIVATE_SENTINEL')
logging.getLogger('httpcore.connection').warning('PRIVATE_SENTINEL')
logging.getLogger('wechat_assistant.runtime').info('safe application status')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    contents = result.stdout + result.stderr + (tmp_path / "assistant.log").read_text()
    assert "PRIVATE_SENTINEL" not in contents
    assert "safe application status" in contents
