"""A real, isolated local-file and follow-up probe; never sends to ClawBot."""

import hashlib
import json
import secrets
import threading
import time

from .local_engine import EngineUnavailable, create_engine
from .storage import directory


def verify_local(settings):
    engine = create_engine(settings)
    if engine.readiness() != "ready":
        raise EngineUnavailable("本地执行入口尚未就绪，请运行 engine-status 检查登录状态")
    root = directory(engine.root / "verification" / secrets.token_hex(8))
    code, days = "LOCAL-" + secrets.token_hex(8).upper(), 13 + secrets.randbelow(29)
    docs = {
        "项目说明.md": f"# 本地项目\n\n项目代号：{code}\n",
        "交付规则.md": f"# 本地规则\n\n交付天数：{days}\n",
    }
    for name, text in docs.items():
        (root / name).write_text(text)
    hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in docs.items()}
    base = int(time.time() * 1000000)
    job = {"job_id": f"J9{base}", "project_directory": str(root), "thread_id": None}
    bound = []
    prompt = "请只读当前目录的《项目说明.md》和《交付规则.md》，回答项目代号、交付天数，并引用文件名和实际行号。最终仅输出含 reply 字段的 JSON。"
    started = time.monotonic()
    first = engine.run(job, prompt, bound.append, lambda _: None, threading.Event())
    job.update(job_id=f"J9{base + 1}", thread_id=first.thread_id)
    second = engine.run(
        job,
        "请沿用上轮问题，再次给出项目代号和交付天数，并保留文件引用。最终仅输出含 reply 字段的 JSON。",
        bound.append,
        lambda _: None,
        threading.Event(),
    )
    checks = {
        "first_values": code in first.text and str(days) in first.text,
        "followup_values": code in second.text and str(days) in second.text,
        "file_references": all(name in first.text for name in docs),
        "exact_session_resumed": first.thread_id == second.thread_id,
        "files_unchanged": all(
            hashlib.sha256((root / name).read_bytes()).hexdigest() == value
            for name, value in hashes.items()
        ),
    }
    receipt = {
        "checks": checks,
        "thread_id": first.thread_id,
        "first_answer": first.text,
        "followup_answer": second.text,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model_backend": settings.execution_backend,
        "clawbot_message_sent": False,
    }
    path = root / "receipt.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise ValueError("本地读取/续聊验收未通过，回执保存在 " + str(path))
    return {"passed": True, "checks": checks, "receipt": str(path)}
