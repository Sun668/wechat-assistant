"""Bind the owner's private ClawBot channel without importing global credentials."""

import getpass
import time

from .adapters.ilink_login import ILinkLogin
from .models import NotSent


def run_ilink_login(settings, args):
    import fcntl

    login = ILinkLogin(settings)
    lock = login.directory / "ilink-login.lock"
    if lock.is_symlink():
        raise ValueError("ClawBot绑定锁不能是符号链接")
    try:
        with lock.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NotSent("该工作区正在进行另一个ClawBot绑定") from None
            if not args.check:
                path = login.begin()
                print(f"请扫描本地 ClawBot ClawBot绑定二维码：{path}", flush=True)
                if args.qr_only:
                    return
            code = getpass.getpass("输入手机显示的绑定验证码：") if args.verify_code else None
            deadline = time.monotonic() + 240
            previous = None
            while time.monotonic() < deadline:
                state = login.check(code)
                code = None
                if state == "confirmed":
                    print("ClawBot 已绑定到当前工作区。", flush=True)
                    print("启动后给 ClawBot 发送“项目列表”，再选择工作项目。", flush=True)
                    return
                if state == "need_verifycode":
                    code = getpass.getpass("请在本终端输入手机显示的绑定验证码：")
                elif state != previous:
                    print("等待手机确认" if state == "scaned" else "等待扫码", flush=True)
                previous = state
                time.sleep(1)
            raise NotSent("ClawBot扫码等待超时；未绑定，不会发送消息")
    finally:
        login.close()
