import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from wechat_assistant.adapters.ilink import ILink
from wechat_assistant.config import Settings
from wechat_assistant.models import Receipt, SendUncertain
from wechat_assistant.projects.api import create_app
from wechat_assistant.projects.bridge import NativeBridge
from wechat_assistant.projects.delivery import OwnerDelivery
from wechat_assistant.projects.router import ProjectRouter
from wechat_assistant.projects.runtime import ProjectRuntime
from wechat_assistant.projects.storage import ProjectStore

P = "11111111-1111-4111-8111-111111111111"
Q = "22222222-2222-4222-8222-222222222222"


def event(text="", files=(), quote=None, **extra):
    items = [{"type": 1, "text_item": {"text": text}}] if text else []
    items += [{"type": 4, "file_item": {"file_name": name}} for name in files]
    if quote is not None:
        items[0]["ref_msg"] = quote
    return {"from_user_id": "owner", "message_id": uuid.uuid4().hex, "item_list": items, **extra}


@pytest.fixture
def gateway(tmp_path):
    store = ProjectStore(tmp_path / ".wechat-assistant", "owner", 1024 * 1024)
    for pid, name in ((P, "甲项目"), (Q, "乙项目")):
        path = tmp_path / "client-projects" / name
        path.mkdir(parents=True)
        store.register(pid, name, str(path))
    router = ProjectRouter(store)
    yield store, router, NativeBridge(store)
    store.close()


def select(router, name="甲项目"):
    return router.ingest(event("选择项目 " + name))


def ready_file(store, router, name="资料.txt", content=b"project data"):
    router.ingest(event(files=[name]))
    file = store.rows("SELECT * FROM files ORDER BY created_at DESC")[0]
    store.save_file(file, content)
    return store.one("SELECT * FROM files WHERE id=?", (file["id"],))


def complete_message(bridge, project=P, text="原生项目的答案", thread="native-thread-a"):
    claimed = bridge.claim(project)
    bridge.bind_thread(project, claimed["job_id"], claimed["claim_token"], thread)
    bridge.complete(project, claimed["job_id"], claimed["claim_token"], text, thread_id=thread)
    bridge.store.set_value("task_engine:" + claimed["task_id"], "codex_shared")
    return claimed


def test_must_select_before_chat_or_files(gateway):
    store, router, _ = gateway
    assert router.ingest(event("问题", files=["私密.docx"]))["status"] == "rejected"
    assert not store.rows("SELECT * FROM jobs")
    assert not store.rows("SELECT * FROM files")
    assert len(store.rows("SELECT * FROM source_events")) == 1
    assert "选择项目" in store.rows("SELECT * FROM outbox")[0]["body"]
    router.ingest(event("项目列表"))
    selected = router.ingest(event("选择项目 1"))
    assert selected["status"] == "command"
    assert store.get_value("active_project") in {P, Q}


@pytest.mark.parametrize("command", ["/help", "帮助", "help"])
@pytest.mark.parametrize("selected", [False, True])
def test_help_returns_commands_without_model_work_or_changing_selection(gateway, command, selected):
    store, router, _ = gateway
    if selected:
        select(router)
    active = store.get_value("active_project")
    raw = event(command)
    assert router.ingest(raw)["status"] == "command"
    replies = store.rows("SELECT body FROM outbox WHERE source_event=?", (raw["message_id"],))
    assert len(replies) == 1
    for entry in ("/help", "退出项目", "重试文件", "/upload-path reset", "继续 T000001"):
        assert entry in replies[0]["body"]
    assert store.get_value("active_project") == active
    assert not store.rows("SELECT * FROM jobs")
    assert not store.rows("SELECT * FROM tasks")
    assert router.ingest(raw)["status"] == "duplicate"
    assert len(store.rows("SELECT 1 FROM outbox WHERE source_event=?", (raw["message_id"],))) == 1


@pytest.mark.parametrize(
    ("command", "menu_index"),
    [
        ("选择项目一", 0),
        ("选择项目 一", 0),
        ("选择项目二", 1),
        ("选择项目 第二个", 1),
        ("二", 1),
        ("选择项目 2", 1),
    ],
)
def test_select_project_accepts_chinese_ordinal_and_optional_space(gateway, command, menu_index):
    store, router, _ = gateway
    router.project_menu()
    expected = json.loads(store.get_value("project_choices"))[menu_index]
    assert router.ingest(event(command))["status"] == "command"
    assert store.get_value("active_project") == expected


def test_chinese_ordinal_does_not_consume_project_name(gateway):
    store, router, _ = gateway
    path = store.project_dir(P).parent / "十月项目"
    path.mkdir()
    local_id = "33333333-3333-4333-8333-333333333333"
    store.register(local_id, "十月项目", str(path))
    router.project_menu()
    assert router.ingest(event("选择项目十月项目"))["status"] == "command"
    assert store.get_value("active_project") == local_id


def test_any_registered_native_project_and_ambiguous_name(gateway):
    store, router, _ = gateway
    store.register("g-p-project-cccccccc", "甲项目")
    assert select(router)["status"] == "rejected"
    assert select(router, P)["status"] == "command"
    with pytest.raises(ValueError):
        store.register("local-folder-id", "本地目录")


def test_switch_pins_messages_files_and_continues_project_dialogue(gateway):
    store, router, _ = gateway
    select(router)
    first = router.ingest(event("第一个问题"))
    followup = router.ingest(event("接着解释"))
    assert first["task_id"] == followup["task_id"]
    file = ready_file(store, router)
    select(router, "乙项目")
    second = router.ingest(event("另一个项目的问题"))
    assert second["project_id"] == Q
    assert store.one("SELECT * FROM files WHERE id=?", (file["id"],))["project_id"] == P
    select(router)
    assert router.ingest(event("继续前文"))["task_id"] == first["task_id"]
    router.ingest(event("新对话"))
    assert router.ingest(event("独立问题"))["task_id"] != first["task_id"]


def test_old_executor_thread_starts_new_cli_task_instead_of_pausing_queue(gateway):
    store, router, bridge = gateway
    select(router)
    old = router.ingest(event("旧入口的问题"))
    claimed = bridge.claim(P)
    bridge.bind_thread(P, claimed["job_id"], claimed["claim_token"], "old-client-thread")
    bridge.complete(
        P, claimed["job_id"], claimed["claim_token"], "旧答复", thread_id="old-client-thread"
    )
    current = router.ingest(event("现在继续提问"))
    assert current["task_id"] != old["task_id"]
    assert (
        store.one("SELECT thread_id FROM tasks WHERE id=?", (current["task_id"],))["thread_id"]
        is None
    )
    notice = store.one(
        "SELECT body FROM outbox WHERE dedupe_key=?", ("started:" + current["job_id"],)
    )
    assert notice["body"].endswith("已收到。")
    assert router.ingest(event(f"继续 {old['task_id']} 追问"))["status"] == "rejected"
    assert store.one("SELECT state FROM jobs WHERE id=?", (current["job_id"],))["state"] == "queued"


def test_new_question_reports_an_existing_project_queue_blocker(gateway):
    store, router, bridge = gateway
    select(router)
    first = router.ingest(event("会执行不明的问题"))
    claimed = bridge.claim(P)
    bridge.fail(P, claimed["job_id"], claimed["claim_token"])
    second = router.ingest(event("后续问题"))
    notice = store.one(
        "SELECT body FROM outbox WHERE dedupe_key=?", ("started:" + second["job_id"],)
    )
    assert first["job_id"] in notice["body"]
    assert "队列" in notice["body"] and "暂停" in notice["body"]


def test_second_message_gets_one_queue_notice_without_progress(gateway):
    store, router, bridge = gateway
    select(router)
    first = router.ingest(event("第一条"))
    claim = bridge.claim(P)
    second = router.ingest(event("第二条"))
    receipts = store.rows(
        "SELECT dedupe_key,body FROM outbox WHERE job_id IN (?,?) ORDER BY id",
        (first["job_id"], second["job_id"]),
    )
    assert len(receipts) == 2
    assert receipts[0]["body"].endswith("已收到。")
    assert "正在排队" in receipts[1]["body"]
    assert first["job_id"] in receipts[1]["body"]
    bridge.bind_thread(P, claim["job_id"], claim["claim_token"], "thread")
    bridge.complete(P, claim["job_id"], claim["claim_token"], "第一条结果", thread_id="thread")
    assert not store.rows("SELECT * FROM outbox WHERE dedupe_key LIKE 'progress:%'")


def test_ilink_labels_received_and_result_messages_with_original_request(gateway, tmp_path):
    store, router, bridge = gateway
    select(router)
    raw = event("需要排队的任务")
    raw["message_id"] = 7521822943853638024
    raw["item_list"][0]["msg_id"] = "v1:4214714151872408135"
    task = router.ingest(raw)
    first_claim = bridge.claim(P)
    queued_raw = event("下一条")
    queued_raw["message_id"] = 7521822995468660616
    queued_raw["item_list"][0]["msg_id"] = "v1:13616077886302037402"
    queued = router.ingest(queued_raw)
    bridge.bind_thread(P, first_claim["job_id"], first_claim["claim_token"], "thread")
    bridge.complete(
        P, first_claim["job_id"], first_claim["claim_token"],
        "第一条结果", thread_id="thread"
    )
    second_claim = bridge.claim(P)
    bridge.bind_thread(P, second_claim["job_id"], second_claim["claim_token"], "thread")
    bridge.complete(
        P, second_claim["job_id"], second_claim["claim_token"],
        "第二条结果", thread_id="thread"
    )
    store.set_value("ilink_context:owner", "test-context")
    sent = []

    def reply(request):
        payload = json.loads(request.content)
        sent.append(payload["msg"]["item_list"][0])
        return httpx.Response(200, json={"ret": 0, "message_id": "bot-receipt"})

    credential = tmp_path / "ilink.json"
    credential.write_text(json.dumps({"bot_token": "test-only", "ilink_user_id": "owner"}))
    ilink = ILink(credential, store, "owner", client=httpx.Client(transport=httpx.MockTransport(reply)))
    OwnerDelivery(store, ilink).flush()
    assert all("ref_msg" not in part for part in sent)
    labeled = [part["text_item"]["text"] for part in sent if "↪ 对应你的消息：" in part["text_item"]["text"]]
    assert len(labeled) == 4
    assert labeled[0].startswith("↪ 对应你的消息：需要排队的任务\n")
    assert labeled[1].startswith("↪ 对应你的消息：下一条\n")
    assert "正在排队" in labeled[1]
    assert labeled[2].startswith("↪ 对应你的消息：需要排队的任务\n")
    assert "第一条结果" in labeled[2]
    assert labeled[3].startswith("↪ 对应你的消息：下一条\n")
    assert "第二条结果" in labeled[3]
    assert store.one("SELECT state FROM outbox WHERE dedupe_key=?", ("started:" + task["job_id"],))["state"] == "sent"
    assert store.one("SELECT state FROM outbox WHERE dedupe_key=?", ("started:" + queued["job_id"],))["state"] == "sent"
    ilink.close()


def test_replay_after_switch_does_not_reassign(gateway):
    store, router, _ = gateway
    select(router)
    raw = event("问题", files=["附件.txt"])
    router.ingest(raw)
    select(router, "乙项目")
    assert router.ingest(raw)["status"] == "duplicate"
    assert {r["project_id"] for r in store.rows("SELECT * FROM jobs")} == {P}
    assert len(store.rows("SELECT * FROM files")) == 1


def test_ilink_batch_order_routes_switch_before_upload(gateway, tmp_path):
    store, router, _ = gateway
    credential = tmp_path / "ilink.json"
    credential.write_text(json.dumps({"bot_token": "test-only", "ilink_user_id": "owner"}))
    batch = [
        event("选择项目 甲项目"),
        event(files=["甲.txt"]),
        event("切换项目 乙项目"),
        event(files=["乙.txt"]),
    ]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"msgs": batch, "get_updates_buf": "cursor"})
        )
    )
    ilink = ILink(credential, store, "owner", client=client, event_sink=router.ingest)
    assert ilink.poll() == 4
    assert [
        (r["name"], r["project_id"]) for r in store.rows("SELECT * FROM files ORDER BY created_at")
    ] == [("甲.txt", P), ("乙.txt", Q)]
    ilink.poll()  # persisted unacknowledged batch is deduplicated
    assert len(store.rows("SELECT * FROM files")) == 2
    ilink.acknowledge_poll()
    ilink.close()


def test_project_fifo_blocks_pending_files_but_not_other_projects(gateway):
    store, router, bridge = gateway
    select(router)
    router.ingest(event(files=["知识.txt"]))
    router.ingest(event("分析资料"))
    select(router, "乙项目")
    router.ingest(event("独立问题"))
    assert bridge.claim(P) is None
    assert bridge.claim(Q)["kind"] == "message"
    file = store.rows("SELECT * FROM files")[0]
    store.save_file(file, b"data")
    claim = bridge.claim(P)
    assert claim["kind"] == "message"
    assert claim["files"][0]["name"] == "知识.txt"
    assert bridge.claim(P) is None


def test_concurrent_claims_are_project_isolated_and_exclusive(gateway):
    _, router, bridge = gateway
    select(router)
    router.ingest(event("甲问题"))
    select(router, "乙项目")
    router.ingest(event("乙问题"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(bridge.claim, [P, Q] * 4))
    assert {c["project_id"] for c in claims if c} == {P, Q}
    assert sum(c is not None for c in claims) == 2


def test_exact_quote_uses_original_project_without_changing_current(gateway):
    store, router, bridge = gateway
    select(router)
    task = router.ingest(event("问题"))
    complete_message(bridge)
    with store.transaction() as db:
        db.execute(
            "UPDATE outbox SET state='sent',external_id='1234567890123456789' WHERE kind='result'"
        )
    select(router, "乙项目")
    response = router.ingest(
        event("修改成三点", quote={"message_item": {"type": 0, "msg_id": 1234567890123456789}})
    )
    assert response["project_id"] == P
    assert response["task_id"] == task["task_id"]
    assert store.get_value("active_project") == Q
    assert router.ingest(event("修改", quote={"message_item": {"type": 0}}))["status"] == "rejected"
    result = store.one("SELECT * FROM outbox WHERE kind='result'")
    assert (
        router.ingest(event("再改", quote={"title": f"【CB:{result['marker'][:8]}】"}))["status"]
        == "rejected"
    )
    assert (
        router.ingest(event("再改", quote={"title": f"【CB:{result['marker']}】"}))["project_id"]
        == P
    )


def test_file_bytes_scoped_and_changed_source_invalidates_result(gateway):
    store, router, bridge = gateway
    select(router)
    file = ready_file(store, router)
    router.ingest(event("总结"))
    claim = bridge.claim(P)
    bridge.bind_thread(P, claim["job_id"], claim["claim_token"], "thread")
    assert all(f["id"] == file["id"] for f in claim["files"])
    store.file_path(file).write_bytes(b"changed")
    with pytest.raises(ValueError, match="快照"):
        bridge.complete(P, claim["job_id"], claim["claim_token"], "结果", thread_id="thread")
    assert not store.rows(
        "SELECT * FROM outbox WHERE task_id=? AND kind='result'", (claim["task_id"],)
    )


def test_restart_quarantines_native_execution_and_sends(gateway):
    store, router, bridge = gateway
    select(router)
    router.ingest(event("问题"))
    claim = bridge.claim(P)
    bridge.bind_thread(P, claim["job_id"], claim["claim_token"], "thread")
    store.set_value("main_started:" + claim["job_id"], "true")
    with store.transaction() as db:
        db.execute("UPDATE outbox SET state='sending'")
    store.recover()
    assert bridge.claim(P) is None
    with pytest.raises(ValueError):
        bridge.complete(P, claim["job_id"], claim["claim_token"], "过期结果", thread_id="thread")
    bridge.reconcile(P, claim["job_id"], "resume_bound")
    resumed = bridge.claim(P)
    assert resumed["resume_only"] is True
    assert resumed["thread_id"] == "thread"
    assert resumed["claim_token"] != claim["claim_token"]
    assert {r["state"] for r in store.rows("SELECT state FROM outbox")} == {
        "uncertain", "pending"
    }
    assert "服务重启时执行状态不明" in store.one(
        "SELECT body FROM outbox WHERE dedupe_key=?", ("failed:" + claim["job_id"],)
    )["body"]


def test_restart_requeues_unsubmitted_job(gateway):
    store, router, bridge = gateway
    select(router)
    task = router.ingest(event("尚未提交的问题"))
    bridge.claim(P)
    store.recover()
    assert store.one("SELECT state FROM jobs WHERE id=?", (task["job_id"],))["state"] == "queued"
    assert bridge.claim(P)["job_id"] == task["job_id"]
    assert not store.rows("SELECT * FROM outbox WHERE dedupe_key=?", ("failed:" + task["job_id"],))


def test_restart_recovers_complete_native_answer_without_resubmission(gateway):
    store, router, bridge = gateway
    select(router)
    task = router.ingest(event("已有完整结果的问题"))
    claim = bridge.claim(P)
    bridge.bind_thread(P, claim["job_id"], claim["claim_token"], "thread")
    store.set_value("main_started:" + claim["job_id"], "true")
    work = store.root / "engine" / "jobs" / claim["job_id"]
    work.mkdir(parents=True)
    (work / "answer.json").write_text(json.dumps({"reply": "原生完整结果"}))
    store.recover()
    resumed = bridge.claim(P)
    assert resumed["job_id"] == task["job_id"]
    assert resumed["resume_only"] is True
    assert resumed["thread_id"] == "thread"
    assert not store.rows("SELECT * FROM outbox WHERE dedupe_key=?", ("failed:" + task["job_id"],))


def test_restart_does_not_mistake_classification_for_final_answer(gateway):
    store, router, bridge = gateway
    select(router)
    router.ingest(event("上一轮"))
    complete_message(bridge)
    task = router.ingest(event("新一轮"))
    bridge.claim(P)
    store.set_value("classification_started:" + task["job_id"], "true")
    work = store.root / "engine" / "jobs" / task["job_id"]
    work.mkdir(parents=True)
    (work / "answer.json").write_text(json.dumps({"reply": "意图识别结果"}))
    store.recover()
    resumed = bridge.claim(P)
    assert resumed["job_id"] == task["job_id"]
    assert resumed["resume_only"] is False


def test_restart_reclassifies_unfinished_clarification(gateway):
    store, router, bridge = gateway
    select(router)
    task = router.ingest(event("需要澄清的问题"))
    bridge.claim(P)
    store.set_value("classification_started:" + task["job_id"], "true")
    store.set_value("intent_classified:" + task["job_id"], "clarify")
    store.recover()
    assert store.get_value("intent_classified:" + task["job_id"]) == ""
    assert bridge.claim(P)["job_id"] == task["job_id"]


def test_discard_expired_uncertain_work_unblocks_project_without_resubmission(gateway):
    store, router, bridge = gateway
    select(router)
    task = router.ingest(event("两点发消息"))
    claim = bridge.claim(P)
    bridge.fail(P, claim["job_id"], claim["claim_token"])
    bridge.reconcile(P, task["job_id"], "discard")
    assert store.one("SELECT state FROM jobs WHERE id=?", (task["job_id"],))["state"] == "completed"
    assert store.get_value("active_task:" + P) == ""
    assert bridge.claim(P) is None
    next_job = router.ingest(event("下一条普通消息"))
    assert next_job["task_id"] != task["task_id"]


class Transport:
    def __init__(self, uncertain=False):
        self.sent = []
        self.uncertain = uncertain

    def can_notify(self):
        return True

    def send(self, destination, body, client_id):
        assert destination == "owner"
        self.sent.append(body)
        if self.uncertain:
            raise SendUncertain("test")
        return Receipt(external_id=client_id, verified=True)

    def send_file(self, destination, payload, content, client_id):
        assert destination == "owner"
        self.sent.append((payload.name, content))
        return Receipt(external_id=client_id, verified=True)


def test_owner_result_auto_delivery_and_no_uncertain_retry(gateway):
    store, router, bridge = gateway
    select(router)
    router.ingest(event("问题"))
    complete_message(bridge)
    transport = Transport(uncertain=True)
    delivery = OwnerDelivery(store, transport)
    delivery.flush()
    count = len(transport.sent)
    delivery.flush()
    assert len(transport.sent) == count
    assert any("原生项目的答案" in body for body in transport.sent)
    assert not store.rows("SELECT name FROM sqlite_master WHERE name='approvals'")


def test_completion_marker_is_only_on_final_result_part(gateway):
    store, router, bridge = gateway
    select(router)
    task = router.ingest(event("请给出长答复"))
    claim = bridge.claim(P)
    bridge.bind_thread(P, claim["job_id"], claim["claim_token"], "codex-thread")
    bridge.complete(P, claim["job_id"], claim["claim_token"], "答" * 3000, thread_id="codex-thread")
    parts = store.rows("SELECT body FROM outbox WHERE kind='result' ORDER BY id")
    marker = f"✅ 本轮任务已完成（{task['job_id']}）"
    assert len(parts) == 2
    assert marker not in parts[0]["body"]
    assert parts[1]["body"].endswith(marker)
    transport = Transport()
    OwnerDelivery(store, transport).flush()
    assert sum(marker in body for body in transport.sent if isinstance(body, str)) == 1


def test_file_send_by_name_is_limited_to_selected_project(gateway):
    store, router, _ = gateway
    select(router)
    ready_file(store, router, "资料.txt", b"A")
    select(router, "乙项目")
    assert router.ingest(event("发送文件《资料.txt》"))["status"] == "rejected"
    ready_file(store, router, "资料.txt", b"B")
    assert router.ingest(event("发送文件《资料.txt》"))["status"] == "accepted"
    transport = Transport()
    OwnerDelivery(store, transport).flush()
    assert ("资料.txt", b"B") in transport.sent
    assert ("资料.txt", b"A") not in transport.sent


def test_foreign_sender_group_and_echo_do_not_create_work(gateway):
    store, router, _ = gateway
    select(router)
    assert router.ingest(event("坏消息", from_user_id="stranger"))["status"] == "ignored"
    assert router.ingest(event("群消息", group_id="group"))["status"] == "ignored"
    with store.transaction() as db:
        db.execute("UPDATE outbox SET external_id='echo-id',state='sent'")
    assert router.ingest(event("回显", message_id="echo-id"))["status"] == "echo"
    assert not store.rows("SELECT * FROM jobs")


def test_native_thread_cannot_be_reused_across_projects(gateway):
    _, router, bridge = gateway
    select(router)
    router.ingest(event("甲"))
    complete_message(bridge, thread="same-thread")
    select(router, "乙项目")
    router.ingest(event("乙"))
    claim = bridge.claim(Q)
    with pytest.raises(ValueError):
        bridge.bind_thread(Q, claim["job_id"], claim["claim_token"], "same-thread")


def test_api_requires_project_token_and_never_exposes_global_files(tmp_path):
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
        execution_backend="codex_shared",
    )
    runtime = ProjectRuntime(settings, transport=Transport())
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runtime.store.register(P, "甲", str(first))
    runtime.store.register(Q, "乙", str(second))
    client = TestClient(create_app(runtime, background=False))
    operator = {"Authorization": "Bearer " + runtime.api_token}
    assert client.get("/health").status_code == 401
    assert client.get("/projects", headers=operator).status_code == 200
    assert client.post(f"/bridge/{P}/claim", headers=operator).status_code == 404
    assert client.get("/files", headers=operator).status_code == 404
    health = client.get("/health", headers=operator).json()
    assert health["wechat_listener"] is False
    assert health["native_executor"] == "codex_shared_local"
    runtime.store.close()


def test_restart_keeps_selected_project_and_its_current_task(tmp_path):
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
        execution_backend="codex_shared",
    )
    unavailable_engine = SimpleNamespace(readiness=lambda: "login_required")
    runtime = ProjectRuntime(settings, transport=Transport(), engine=unavailable_engine)
    project = tmp_path / "first"
    project.mkdir()
    runtime.store.register(P, "甲项目", str(project))

    async def lifecycle():
        await runtime.start()
        select(runtime.router)
        first = runtime.router.ingest(event("之前的任务"))
        await runtime.stop()
        restarted = ProjectRuntime(settings, transport=Transport(), engine=unavailable_engine)
        await restarted.start()
        try:
            assert restarted.store.get_value("active_project") == P
            followup = restarted.router.ingest(event("新问题"))
            assert followup["project_id"] == P
            assert followup["task_id"] == first["task_id"]
        finally:
            await restarted.stop()

    asyncio.run(lifecycle())


def test_restart_clears_selection_if_primary_directory_is_gone(tmp_path):
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
    )
    unavailable_engine = SimpleNamespace(readiness=lambda: "login_required")
    runtime = ProjectRuntime(settings, transport=Transport(), engine=unavailable_engine)
    project = tmp_path / "first"
    project.mkdir()
    runtime.store.register(P, "甲项目", str(project))

    async def lifecycle():
        await runtime.start()
        select(runtime.router)
        await runtime.stop()
        project.rmdir()
        restarted = ProjectRuntime(settings, transport=Transport(), engine=unavailable_engine)
        await restarted.start()
        try:
            assert restarted.store.get_value("active_project") == ""
            assert restarted.router.ingest(event("问题"))["status"] == "rejected"
        finally:
            await restarted.stop()

    asyncio.run(lifecycle())


def test_changed_source_is_rechecked_before_delivery(gateway):
    store, router, bridge = gateway
    select(router)
    file = ready_file(store, router)
    router.ingest(event("根据文件回答"))
    complete_message(bridge)
    store.file_path(file).write_bytes(b"changed after result")
    transport = Transport()
    OwnerDelivery(store, transport).flush()
    assert not any("原生项目的答案" in item for item in transport.sent)
    assert {r["state"] for r in store.rows("SELECT state FROM outbox WHERE kind='result'")} == {
        "failed"
    }


def test_rejected_conflicting_quote_rolls_back_project_switch(gateway):
    store, router, bridge = gateway
    select(router)
    router.ingest(event("问题"))
    complete_message(bridge)
    with store.transaction() as db:
        db.execute("UPDATE outbox SET state='sent',external_id='original' WHERE kind='result'")
    conflict = router.ingest(
        event("[乙项目] 修改", files=["不得跨项目.txt"], quote={"svr_id": "original"})
    )
    assert conflict["status"] == "rejected"
    assert store.get_value("active_project") == P
    assert not store.rows("SELECT * FROM files")


def test_files_land_directly_in_readable_project_directory(gateway):
    store, router, _ = gateway
    select(router)
    first = ready_file(store, router, "计划.docx", b"first")
    path = store.file_path(first)
    assert path == store.project_dir(P) / "计划.docx"
    assert "甲项目" in str(path)
    assert path.read_bytes() == b"first"
    second = ready_file(store, router, "计划.docx", b"second")
    assert second["name"] == "计划 (2).docx"
    assert path.read_bytes() == b"first"
    assert store.file_path(second).read_bytes() == b"second"
    assert {r["state"] for r in store.rows("SELECT state FROM jobs")} == {"completed"}
    assert not store.rows("SELECT * FROM jobs WHERE kind='file_sync'")
    router.ingest(event("文件列表"))
    assert "已保存到项目目录" in store.rows("SELECT body FROM outbox ORDER BY id DESC")[0]["body"]


def test_local_project_directory_survives_rename_and_refuses_symlinks(gateway, tmp_path):
    store, router, _ = gateway
    select(router)
    file = ready_file(store, router)
    original = store.project_dir(P)
    store.register(P, "项目新名称", str(original))
    assert store.project_dir(P) == original
    assert store.file_content(file) == b"project data"
    select(router, "乙项目")
    qpath = store.project_dir(Q)
    qpath.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    qpath.symlink_to(outside)
    with pytest.raises(ValueError):
        store.project_dir(Q)


def test_local_file_write_does_not_overwrite_operator_file(gateway):
    store, router, _ = gateway
    select(router)
    router.ingest(event(files=["笔记.txt"]))
    file = store.rows("SELECT * FROM files")[0]
    store.file_path(file).write_bytes(b"operator data")
    with pytest.raises(ValueError):
        store.save_file(file, b"downloaded data")
    assert store.file_path(file).read_bytes() == b"operator data"


def test_download_to_selected_project_does_not_need_cloud_upload(tmp_path):
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
        execution_backend="codex_shared",
    )
    runtime = ProjectRuntime(settings, transport=Transport())
    for pid, name in ((P, "甲项目"), (Q, "乙项目")):
        path = tmp_path / "client-projects" / name
        path.mkdir(parents=True)
        runtime.store.register(pid, name, str(path))
    select(runtime.router)
    runtime.router.ingest(event(files=["资料.txt"]))
    runtime.router.ingest(event("总结资料"))

    def download(item):
        select(runtime.router, "乙项目")
        return b"local project content"

    runtime.ilink = SimpleNamespace(media=SimpleNamespace(download=download))
    assert runtime.download_next() is True
    file = runtime.store.rows("SELECT * FROM files")[0]
    assert file["project_id"] == P
    assert runtime.store.file_path(file) == runtime.store.project_dir(P) / "资料.txt"
    assert runtime.store.file_content(file) == b"local project content"
    claim = runtime.bridge.claim(P)
    assert claim["kind"] == "message"
    assert claim["project_directory"] == str(runtime.store.project_dir(P))
    assert runtime.health()["cloud_file_upload_required"] is False
    runtime.store.close()
