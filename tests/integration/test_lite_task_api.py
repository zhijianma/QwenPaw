# -*- coding: utf-8 -*-
"""Focused HTTP contract tests for the Lite task API."""
# pylint: disable=protected-access

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

import qwenpaw.app.approvals as approvals_package
from qwenpaw.app import task_runtime as task_runtime_app
from qwenpaw.app.approvals.models import ApprovalRequestSummary
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.app.routers import tasks as tasks_router
from qwenpaw.app.task_runtime import ApprovedProposalDispatcher
from qwenpaw.app.exception_handlers import task_api_error_handler
from qwenpaw.app.task_api_errors import TaskApiError
from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalContinuation,
    ApprovalDecisionValue,
    ArtifactRenderDisposition,
    ArtifactRenderResult,
    PlanStep,
    Proposal,
    RiskLevel,
    Run,
    RunnerSignal,
    TaskOrder,
    ToolEffect,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.kernel.proposals import approved_task_order
from qwenpaw.security.tool_guard.approval import (
    ApprovalDecision as RuntimeApprovalDecision,
)
from qwenpaw.tasks.bootstrap import task_service_for_workspace
from qwenpaw.tasks.runner import LocalAgentRunner
from qwenpaw.tasks.side_effects import TaskSideEffectBroker
from qwenpaw.tasks.system_contributions import SYSTEM_CONSOLE_RUNNER_ID


class _ApiReviewSensor:
    @property
    def sensor_id(self) -> str:
        return "task-insights.review-sensor"

    async def propose(self) -> tuple[Proposal, ...]:
        return (
            Proposal(
                source=self.sensor_id,
                objective="Review the API task outcome",
                rationale_summary="The sensor was explicitly polled",
                risk=RiskLevel.LOW,
            ),
        )


def _client(
    monkeypatch,
    tmp_path: Path,
    workspace=None,
    capability_registry=None,
) -> TestClient:
    workspace = workspace or SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )

    async def get_workspace(_request):
        return workspace

    monkeypatch.setattr(
        task_runtime_app,
        "get_agent_for_request",
        get_workspace,
    )
    app = FastAPI()
    if capability_registry is not None:
        app.state.plugin_loader = SimpleNamespace(
            capability_registry=capability_registry,
        )
    app.add_exception_handler(
        TaskApiError,
        task_api_error_handler,
    )
    app.include_router(tasks_router.router, prefix="/api")
    return TestClient(app)


def _assert_artifact_content_contract(
    client: TestClient,
    task_id: str,
    artifact_id: str,
    artifact_path: Path,
    legacy_artifact_path: Path,
    monkeypatch,
) -> None:
    content_path = f"/api/tasks/{task_id}/artifacts/{artifact_id}/content"
    preview = client.get(content_path)
    assert preview.status_code == 200
    assert preview.text == "Task reply"
    assert preview.headers["content-type"].startswith("text/markdown")
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["content-security-policy"] == (
        "default-src 'none'; sandbox"
    )
    assert preview.headers["x-qwenpaw-artifact-renderer"] == (
        "qwenpaw.system.tasks.safe-artifact-renderer"
    )
    assert preview.headers["x-qwenpaw-source-hash"].startswith("sha256:")
    download = client.get(
        content_path,
        params={"disposition": "attachment"},
    )
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith(
        "attachment;",
    )

    legacy_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.replace(legacy_artifact_path)
    legacy_preview = client.get(content_path)
    assert legacy_preview.status_code == 200
    assert legacy_preview.text == "Task reply"

    host = client.app.state.task_application_host
    artifact_application = host._bindings["default"].artifacts
    monkeypatch.setattr(artifact_application, "_max_preview_bytes", 1)
    too_large = client.get(content_path)
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "artifact_preview_too_large"

    other_task = client.post(
        "/api/tasks",
        json={"objective": "Must not read another task artifact"},
    ).json()
    cross_task = client.get(
        f"/api/tasks/{other_task['task_id']}/artifacts/"
        f"{artifact_id}/content",
    )
    assert cross_task.status_code == 404
    assert cross_task.json()["code"] == "artifact_not_found"


def _assert_usage_projection(projection: dict) -> None:
    assert projection["usage"] == {
        "schema": "qwenpaw.kernel-model.v1",
        "input_tokens": 21,
        "output_tokens": 5,
        "cost_micros": 0,
        "cost_unknown": True,
        "tool_calls": 0,
    }
    assert projection["last_sequence"] == 12


def _assert_result_projection(projection: dict, artifacts: dict) -> None:
    """Verify new result fields while preserving legacy artifact fields."""
    assert projection["artifacts"][0]["metadata"]["name"] == ("task-result.md")
    assert projection["artifacts"][0]["preview"] == {
        "schema": "qwenpaw.kernel-model.v1",
        "available": True,
        "registry_generation": projection["active_run"]["registry_generation"],
        "renderer_id": "qwenpaw.system.tasks.safe-artifact-renderer",
        "reason": "",
    }
    assert projection["evidence"][0]["claim"] == "Agent final response"
    assert projection["artifact_registry"][0]["status"] == "ready"
    assert projection["artifact_registry"][0]["event_id"]
    assert (
        projection["artifact_registry"][0]["correlation_id"]
        == projection["active_run"]["correlation_id"]
    )
    assert projection["evidence_registry"][0]["event_id"]
    assert (
        projection["evidence_registry"][0]["correlation_id"]
        == projection["active_run"]["correlation_id"]
    )
    assert projection["verifications"] == []
    assert projection["verification_registry"] == []
    assert (
        projection["result_package"]["artifacts"][0]["artifact_id"]
        == projection["artifacts"][0]["artifact_id"]
    )
    assert artifacts["items"][0]["metadata"]["name"] == "task-result.md"
    assert artifacts["items"][0]["preview"]["available"] is True
    assert artifacts["evidence"][0]["claim"] == "Agent final response"
    assert artifacts["registry"][0]["status"] == "ready"
    assert artifacts["evidence_registry"] == projection["evidence_registry"]
    assert artifacts["verifications"] == []
    assert artifacts["verification_registry"] == []


def test_create_list_cancel_and_replay_events(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)

    created_response = client.post(
        "/api/tasks",
        json={
            "objective": "Prepare a local report",
            "acceptance_criteria": ["Report exists"],
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()

    listed = client.get("/api/tasks").json()
    assert [item["task_id"] for item in listed["items"]] == [
        created["task_id"],
    ]

    cancelled_response = client.post(
        f"/api/tasks/{created['task_id']}/cancel",
    )
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["status"] == "cancelled"

    detail = client.get(f"/api/tasks/{created['task_id']}").json()
    assert detail["task"]["version"] == 2
    events = client.get(
        f"/api/tasks/{created['task_id']}/events",
    ).json()
    assert [item["event_type"] for item in events["items"]] == [
        "task.created",
        "task.cancelled",
    ]


def test_task_and_event_cursors_continue_without_duplicates(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    first_task = client.post(
        "/api/tasks",
        json={"objective": "First cursor task"},
    ).json()
    second_task = client.post(
        "/api/tasks",
        json={"objective": "Second cursor task"},
    ).json()

    first_page = client.get("/api/tasks?limit=1").json()
    second_page = client.get(
        "/api/tasks",
        params={"limit": 1, "cursor": first_page["next_cursor"]},
    ).json()

    returned_ids = {
        first_page["items"][0]["task_id"],
        second_page["items"][0]["task_id"],
    }
    assert returned_ids == {
        first_task["task_id"],
        second_task["task_id"],
    }
    client.post(f"/api/tasks/{first_task['task_id']}/cancel")
    event_page = client.get(
        f"/api/tasks/{first_task['task_id']}/events?limit=1",
    ).json()
    continued = client.get(
        f"/api/tasks/{first_task['task_id']}/events",
        params={"after_sequence": event_page["next_sequence"]},
    ).json()
    assert event_page["items"][0]["event_type"] == "task.created"
    assert continued["items"][0]["event_type"] == "task.cancelled"
    assert continued["next_sequence"] == 2


def test_sse_accepts_query_cursor_and_rejects_conflicting_header(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    task = client.post(
        "/api/tasks",
        json={"objective": "Replay from a cursor"},
    ).json()
    client.post(f"/api/tasks/{task['task_id']}/cancel")
    path = f"/api/tasks/{task['task_id']}/stream"

    replay = client.get(path, params={"after_sequence": 1})

    assert replay.status_code == 200
    assert "id: 2" in replay.text
    assert "id: 1" not in replay.text
    conflict = client.get(
        path,
        params={"after_sequence": 1},
        headers={"Last-Event-ID": "2"},
    )
    assert conflict.status_code == 400
    assert conflict.json()["code"] == "invalid_event_cursor"


def test_workspace_reuses_one_edition_runtime_assembly_across_requests(
    monkeypatch,
    tmp_path,
) -> None:
    original = task_runtime_app.build_deployment_adapter
    calls = []

    def recording_adapter(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        task_runtime_app,
        "build_deployment_adapter",
        recording_adapter,
    )
    client = _client(monkeypatch, tmp_path)
    task = client.post(
        "/api/tasks",
        json={"objective": "Reuse request assembly"},
    ).json()
    assert len(calls) == 1
    calls.clear()

    response = client.get(f"/api/tasks/{task['task_id']}/projection")

    assert response.status_code == 200
    assert not calls


def test_create_preserves_execution_contract(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    contract = {
        "goal": "Prepare a bounded report",
        "acceptance": ["Report exists"],
        "autonomy_level": "l2",
        "budget": {"max_duration_seconds": 300},
    }

    response = client.post(
        "/api/tasks",
        json={
            "objective": contract["goal"],
            "acceptance_criteria": contract["acceptance"],
            "execution_contract": contract,
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["execution_contract"]["goal"] == contract["goal"]
    detail = client.get(f"/api/tasks/{created['task_id']}").json()
    assert (
        detail["task"]["execution_contract"]["budget"]["max_duration_seconds"]
        == 300
    )


def test_create_rejects_incomplete_unattended_contract(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/tasks",
        json={
            "objective": "Publish unattended",
            "execution_contract": {
                "goal": "Publish unattended",
                "autonomy_level": "l3",
            },
        },
    )

    assert response.status_code == 422


def test_invalid_transition_uses_stable_error_code(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    task = client.post(
        "/api/tasks",
        json={"objective": "Cancel exactly once"},
    ).json()
    path = f"/api/tasks/{task['task_id']}/cancel"

    assert client.post(path).status_code == 200
    conflict = client.post(path)

    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith(
        "application/problem+json",
    )
    assert conflict.json()["code"] == "invalid_task_transition"


def test_task_decision_wakes_matching_runtime_approval(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    service = task_service_for_workspace(workspace)
    runtime_service = ApprovalService()

    async def prepare():
        task = await service.create_task(
            objective="Approve a guarded tool",
            agent_id="default",
        )
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Guard", objective=task.objective),),
        )
        await service.start_task(task.task_id, runner_id="runner.local")
        pending = await runtime_service.create_pending_summary(
            session_id=f"task-{task.task_id}",
            root_session_id=f"task-{task.task_id}",
            owner_agent_id="default",
            user_id="local-user",
            channel="console",
            agent_id="default",
            summary=ApprovalRequestSummary(
                source_type="tool_guard",
                name="execute_shell_command",
            ),
        )
        approval = await service.request_approval(
            task.task_id,
            approval_id=UUID(pending.request_id),
            action="tool.execute",
            risk=RiskLevel.HIGH,
            requester=ActorRef(type=ActorType.AGENT, id="default"),
            policy="tool_guard",
            continuation=ApprovalContinuation.RESUME_ON_DECISION,
        )
        return task, approval, pending

    task, approval, pending = asyncio.run(prepare())
    monkeypatch.setattr(
        approvals_package,
        "get_approval_service",
        lambda: runtime_service,
    )
    with _client(monkeypatch, tmp_path, workspace) as client:
        response = client.post(
            f"/api/tasks/{task.task_id}/approvals/"
            f"{approval.approval_id}/decision",
            json={"decision": "denied", "reason": "Do not run it"},
        )

    assert response.status_code == 200
    assert response.json()["decision"] == "denied"
    assert pending.future.result() is RuntimeApprovalDecision.DENIED
    restored = asyncio.run(service.get_task(task.task_id))
    assert restored is not None
    assert restored.status.value == "running"


def test_task_decision_recovers_missing_runtime_before_resolution(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    service = task_service_for_workspace(workspace)

    async def prepare():
        task = await service.create_task(
            objective="Recover a lost approval waiter",
            agent_id="default",
        )
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Guard", objective=task.objective),),
        )
        await service.start_task(task.task_id, runner_id="runner.local")
        approval = await service.request_approval(
            task.task_id,
            action="tool.execute",
            risk=RiskLevel.HIGH,
            requester=ActorRef(type=ActorType.AGENT, id="default"),
            continuation=ApprovalContinuation.RESUME_ON_DECISION,
        )
        return task, approval

    task, approval = asyncio.run(prepare())
    runtime_service = ApprovalService()
    monkeypatch.setattr(
        approvals_package,
        "get_approval_service",
        lambda: runtime_service,
    )
    with _client(monkeypatch, tmp_path, workspace) as client:
        response = client.post(
            f"/api/tasks/{task.task_id}/approvals/"
            f"{approval.approval_id}/decision",
            json={"decision": "approved", "reason": "Continue"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "task_runtime_lost"
    restored = asyncio.run(service.get_task(task.task_id))
    record = asyncio.run(service.get_approval(approval.approval_id))
    assert restored is not None
    assert restored.status.value == "failed"
    assert record is not None
    assert record[1] is not None
    assert record[1].decision is ApprovalDecisionValue.CANCELLED
    assert approval.checkpoint_id is not None


def test_side_effect_retry_requires_explicit_authorization(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    service = task_service_for_workspace(workspace)

    async def prepare():
        task = await service.create_task(
            objective="Recover an uncertain write",
            agent_id="default",
        )
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Write", objective=task.objective),),
        )
        _, run = await service.start_task(
            task.task_id,
            runner_id="runner.local",
        )
        approval = await service.request_approval(
            task.task_id,
            action="file.write",
            risk=RiskLevel.HIGH,
            requester=ActorRef(type=ActorType.AGENT, id="default"),
        )
        await service.decide_approval(
            task.task_id,
            approval.approval_id,
            decision=ApprovalDecisionValue.APPROVED,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            reason="Proceed",
        )
        reservation = await TaskSideEffectBroker(
            service=service,
            task_id=task.task_id,
            run_id=run.run_id,
        ).begin(
            action="Write",
            target="README.md",
            effect=ToolEffect.LOCAL_WRITE,
            idempotency_key="tool:lost:write-1",
            request_hash=hashlib.sha256(b"write").hexdigest(),
        )
        await service.recover_orphaned_task(task.task_id)
        return task, reservation.record

    task, record = asyncio.run(prepare())
    with _client(monkeypatch, tmp_path, workspace) as client:
        listed = client.get(f"/api/tasks/{task.task_id}/side-effects")
        authorized = client.post(
            f"/api/tasks/{task.task_id}/side-effects/"
            f"{record.record_id}/retry",
            json={"reason": "Verified the write did not reach disk"},
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["status"] == "uncertain"
    assert authorized.status_code == 200
    assert authorized.json()["status"] == "failed"
    assert authorized.json()["error_code"] == "RetryAuthorized"


def test_task_approval_query_lists_all_pending_requests(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    service = task_service_for_workspace(workspace)

    async def prepare():
        task = await service.create_task(
            objective="Expose multiple approvals",
            agent_id="default",
        )
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Guard", objective=task.objective),),
        )
        await service.start_task(task.task_id, runner_id="runner.local")
        first = await service.request_approval(
            task.task_id,
            action="tool.first",
            risk=RiskLevel.HIGH,
            requester=ActorRef(type=ActorType.AGENT, id="default"),
            continuation=ApprovalContinuation.RESUME_ON_DECISION,
        )
        second = await service.request_approval(
            task.task_id,
            action="tool.second",
            risk=RiskLevel.MEDIUM,
            requester=ActorRef(type=ActorType.AGENT, id="default"),
            continuation=ApprovalContinuation.RESUME_ON_DECISION,
        )
        return task, first, second

    task, first, second = asyncio.run(prepare())
    client = _client(monkeypatch, tmp_path, workspace)

    response = client.get(
        f"/api/tasks/{task.task_id}/approvals",
        params={"status": "pending"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["approval_id"] for item in items] == [
        str(first.approval_id),
        str(second.approval_id),
    ]
    assert [item["action"] for item in items] == [
        "tool.first",
        "tool.second",
    ]
    assert all(item["status"] == "pending" for item in items)
    assert all(item["decision"] is None for item in items)


def test_create_is_durably_idempotent(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    headers = {"Idempotency-Key": "create-report-1"}

    first = client.post(
        "/api/tasks",
        json={"objective": "Prepare report"},
        headers=headers,
    )
    retry = client.post(
        "/api/tasks",
        json={"objective": "Prepare report"},
        headers=headers,
    )
    conflict = client.post(
        "/api/tasks",
        json={"objective": "Different task"},
        headers=headers,
    )

    assert first.json()["task_id"] == retry.json()["task_id"]
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_cancel_is_durably_idempotent(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    first_task = client.post(
        "/api/tasks",
        json={"objective": "Cancel safely"},
    ).json()
    second_task = client.post(
        "/api/tasks",
        json={"objective": "Detect key reuse"},
    ).json()
    headers = {"Idempotency-Key": "cancel-task-1"}

    path = f"/api/tasks/{first_task['task_id']}/cancel"
    first = client.post(path, headers=headers)
    retry = client.post(path, headers=headers)
    conflict = client.post(
        f"/api/tasks/{second_task['task_id']}/cancel",
        headers=headers,
    )

    assert first.json() == retry.json()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    events = client.get(
        f"/api/tasks/{first_task['task_id']}/events",
    ).json()["items"]
    assert [event["event_type"] for event in events] == [
        "task.created",
        "task.cancelled",
    ]


def test_approved_proposal_is_dispatched_once(monkeypatch, tmp_path) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    service = task_service_for_workspace(workspace)
    proposal = Proposal(
        source="proactive.memory",
        objective="Review the release report",
        rationale_summary="The report is due",
        risk=RiskLevel.MEDIUM,
    )

    async def prepare():
        task = await service.create_task(
            objective=proposal.objective,
            agent_id="default",
            metadata={"proposal": proposal.model_dump(mode="json")},
        )
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Review", objective=task.objective),),
        )
        await service.start_task(task.task_id, runner_id="runner.local-agent")
        approval = await service.request_approval(
            task.task_id,
            action="proposal.execute",
            risk=RiskLevel.MEDIUM,
            requester=ActorRef(
                type=ActorType.SENSOR,
                id="proactive.memory",
            ),
            redacted_arguments={
                "proposal_id": str(proposal.proposal_id),
            },
        )
        return task, approval

    task, approval = asyncio.run(prepare())
    dispatched = []
    monkeypatch.setattr(
        ApprovedProposalDispatcher,
        "schedule",
        lambda _self, _workspace, _service, order: dispatched.append(order),
    )
    client = _client(monkeypatch, tmp_path, workspace)
    path = (
        f"/api/tasks/{task.task_id}/approvals/"
        f"{approval.approval_id}/decision"
    )
    payload = {
        "decision": "approved",
        "reason": "Run this proposal",
    }
    headers = {"Idempotency-Key": "approve-proposal-1"}

    first = client.post(path, json=payload, headers=headers)
    retry = client.post(path, json=payload, headers=headers)

    assert first.status_code == 200
    assert retry.json() == first.json()
    assert len(dispatched) == 1
    assert dispatched[0].approval_ids == (approval.approval_id,)


# pylint: disable-next=too-many-statements
def test_created_task_starts_in_console_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    payloads = []
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class ConsoleChannel:
        async def stream_one(self, payload):
            payloads.append(payload)
            yield (
                'data: {"object":"content","type":"text",'
                '"delta":true,"text":"Task reply"}\n\n'
            )
            yield (
                'data: {"object":"response","status":"completed",'
                '"output":[{"role":"assistant","content":['
                '{"type":"text","text":"Task reply"}]}]}\n\n'
            )
            yield (
                'data: {"type":"turn_usage","usage":'
                '{"prompt_tokens":21,"completion_tokens":5}}\n\n'
            )

    class ChannelManager:
        async def get_channel(self, name):
            return ConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=ChannelManager(),
    )
    with _client(monkeypatch, tmp_path, workspace) as client:
        created = client.post(
            "/api/tasks",
            json={
                "objective": "Execute from the workbench",
                "project_dir": str(project_dir),
            },
        ).json()

        started = client.post(f"/api/tasks/{created['task_id']}/start")

        assert started.status_code == 202
        assert started.json()["task"]["status"] == "running"
        assert started.json()["run"]["strategy_id"] == (
            "qwenpaw.system.tasks.default-strategy"
        )
        started_run = started.json()["run"]
        for _ in range(100):
            detail = client.get(f"/api/tasks/{created['task_id']}").json()
            if detail["task"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert detail["task"]["status"] == "completed"
        assert detail["plan"]["steps"][0]["objective"] == created["objective"]
        assert payloads[0]["meta"]["request_context"]["durable_task"] is True
        request_context = payloads[0]["meta"]["request_context"]
        assert request_context["agent_id"] == "default"
        assert request_context["os_invocation_id"] == (
            started_run["invocation_id"]
        )
        assert request_context["os_correlation_id"] == (
            started_run["correlation_id"]
        )
        assert request_context["runtime_strategy"] == {
            "id": "qwenpaw.system.tasks.default-strategy",
            "parameters": {"mode": "default"},
        }
        assert "approval_level" not in request_context
        assert "_headless_tool_guard" not in request_context
        assert payloads[0]["meta"]["request_context"][
            "session_project_dirs"
        ] == [{"path": str(project_dir), "label": None}]
        events = client.get(
            f"/api/tasks/{created['task_id']}/events",
        ).json()["items"]
        assert [event["event_type"] for event in events] == [
            "task.created",
            "task.planned",
            "run.started",
            "conversation.user",
            "runner.dispatched",
            "runner.generating",
            "runner.responding",
            "conversation.assistant.delta",
            "usage.recorded",
            "conversation.assistant.completed",
            "artifact.produced",
            "run.completed",
        ]
        assert events[3]["payload"]["text"] == created["objective"]
        assert events[4]["payload"]["approval_policy"] == "agent_profile"
        assert {event["invocation_id"] for event in events[2:]} == {
            started_run["invocation_id"],
        }
        assert {event["correlation_id"] for event in events[2:]} == {
            started_run["correlation_id"],
        }
        assert events[7]["payload"]["text"] == "Task reply"
        assert events[9]["payload"] == {
            "content": [
                {
                    "text": "Task reply",
                    "type": "text",
                },
            ],
            "role": "assistant",
            "text": "Task reply",
        }
        projection = client.get(
            f"/api/tasks/{created['task_id']}/projection",
        ).json()
        assert projection["task"]["status"] == "completed"
        assert projection["active_run"]["runner_id"] == (
            "qwenpaw.system.tasks.console-agent"
        )
        assert projection["capabilities"] == [
            {
                "capability_id": "qwenpaw.system.tasks.console-agent",
                "slot": "runner",
                "registry_generation": projection["active_run"][
                    "registry_generation"
                ],
            },
            {
                "capability_id": "qwenpaw.system.tasks.default-strategy",
                "slot": "strategy",
                "registry_generation": projection["active_run"][
                    "registry_generation"
                ],
            },
            {
                "capability_id": (
                    "qwenpaw.system.tasks.safe-artifact-renderer"
                ),
                "slot": "artifact.renderer",
                "registry_generation": projection["artifacts"][0]["preview"][
                    "registry_generation"
                ],
            },
        ]
        assert [
            message["text"] for message in projection["conversation_messages"]
        ] == ["Execute from the workbench", "Task reply"]
        _assert_usage_projection(projection)
        artifacts = client.get(
            f"/api/tasks/{created['task_id']}/artifacts",
        ).json()
        _assert_result_projection(projection, artifacts)
        digest = artifacts["items"][0]["content_hash"].removeprefix(
            "sha256:",
        )
        artifact_path = (
            project_dir
            / ".qwenpaw"
            / "lite"
            / "artifacts"
            / "sha256"
            / digest[:2]
            / digest
        )
        assert artifact_path.read_text(encoding="utf-8") == "Task reply"
        _assert_artifact_content_contract(
            client,
            created["task_id"],
            artifacts["items"][0]["artifact_id"],
            artifact_path,
            (
                tmp_path
                / ".qwenpaw"
                / "lite"
                / "artifacts"
                / "sha256"
                / digest[:2]
                / digest
            ),
            monkeypatch,
        )


def test_suspended_task_resumes_through_console_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    payloads = []
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class ConsoleChannel:
        async def stream_one(self, payload):
            payloads.append(payload)
            yield (
                'data: {"object":"response","status":"completed",'
                '"output":[{"role":"assistant","content":['
                '{"type":"text","text":"Resumed result"}]}]}\n\n'
            )

    class ChannelManager:
        async def get_channel(self, name):
            return ConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=ChannelManager(),
    )
    service = task_service_for_workspace(workspace)

    async def prepare():
        task = await service.create_task(
            objective="Resume a suspended Console task",
            agent_id="default",
            metadata={"workspace_dir": str(project_dir)},
        )
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Resume", objective=task.objective),),
        )
        await service.start_task(
            task.task_id,
            runner_id=SYSTEM_CONSOLE_RUNNER_ID,
        )
        checkpoint = await service.suspend_task(
            task.task_id,
            runner_cursor={"next_step": 2},
        )
        return task, checkpoint

    task, checkpoint = asyncio.run(prepare())
    with _client(monkeypatch, tmp_path, workspace) as client:
        response = client.post(f"/api/tasks/{task.task_id}/resume")
        assert response.status_code == 200
        assert response.json()["run"]["attempt"] == 2
        assert response.json()["run"]["checkpoint_id"] == str(
            checkpoint.checkpoint_id,
        )
        for _ in range(100):
            detail = client.get(f"/api/tasks/{task.task_id}").json()
            if detail["task"]["status"] == "completed":
                break
            time.sleep(0.01)

    assert detail["task"]["status"] == "completed"
    resume_context = payloads[0]["meta"]["request_context"][
        "resume_checkpoint"
    ]
    assert resume_context["checkpoint_id"] == str(checkpoint.checkpoint_id)
    assert resume_context["runner_cursor"] == {"next_step": 2}


def test_task_executes_selected_plugin_runner(monkeypatch, tmp_path) -> None:
    async def execute(
        order: TaskOrder,
        run: Run,
    ):
        yield RunnerSignal(
            event_type="plugin.task-insights.summary",
            payload={
                "objective_length": len(order.objective),
                "generation": run.registry_generation,
            },
        )

    registry = GenerationRegistry()
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "task-insights",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "summary-runner",
                    "slot": "runner",
                    "entrypoint": "task_insights.runner:create_runner",
                },
            ],
        },
    )
    asyncio.run(
        registry.activate(
            manifest,
            lambda _: LocalAgentRunner(
                "task-insights.summary-runner",
                execute,
            ),
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )

    with _client(
        monkeypatch,
        tmp_path,
        workspace,
        capability_registry=registry,
    ) as client:
        created = client.post(
            "/api/tasks",
            json={
                "objective": "Summarize with a plugin",
                "runner_id": "task-insights.summary-runner",
            },
        ).json()
        path = f"/api/tasks/{created['task_id']}"

        started = client.post(f"{path}/start")

        assert started.status_code == 202
        assert started.json()["run"]["runner_id"] == (
            "task-insights.summary-runner"
        )
        generation = started.json()["run"]["registry_generation"]
        assert generation == registry.generation
        for _ in range(100):
            detail = client.get(path).json()
            if detail["task"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert detail["task"]["status"] == "completed"
        events = client.get(f"{path}/events").json()["items"]
        assert events[-2]["event_type"] == ("plugin.task-insights.summary")
        assert events[-2]["payload"] == {
            "objective_length": 23,
            "generation": generation,
        }


def test_plugin_runner_artifact_uses_plugin_renderer(
    monkeypatch,
    tmp_path,
) -> None:
    async def execute_context(order, run, context):
        del run
        yield await context.artifact_emitter.emit(
            kind="plugin.summary",
            media_type="application/x-plugin-summary",
            content=order.objective.encode("utf-8"),
            name="summary.plugin",
            evidence_claim="Plugin summary source",
        )

    class PluginRenderer:
        renderer_id = "rendered-task.summary-renderer"
        priority = 100

        async def health_check(self):
            return True

        def supports(self, artifact, disposition):
            return (
                artifact.kind == "plugin.summary"
                and disposition is ArtifactRenderDisposition.INLINE
            )

        async def render(self, request):
            return ArtifactRenderResult(
                renderer_id=self.renderer_id,
                content=b"Rendered plugin summary",
                media_type="text/plain",
                filename=request.filename,
                disposition=request.disposition,
                source_content_hash=request.artifact.content_hash,
            )

    registry = GenerationRegistry()
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "rendered-task",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "runner",
                    "slot": "runner",
                    "entrypoint": "rendered_task:create_runner",
                },
                {
                    "id": "summary-renderer",
                    "slot": "artifact.renderer",
                    "entrypoint": "rendered_task:create_renderer",
                },
            ],
        },
    )

    def create_contribution(declaration):
        if declaration.contribution_id == "runner":
            return LocalAgentRunner(
                "rendered-task.runner",
                execute_context=execute_context,
            )
        return PluginRenderer()

    asyncio.run(registry.activate(manifest, create_contribution))
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    with _client(
        monkeypatch,
        tmp_path,
        workspace,
        capability_registry=registry,
    ) as client:
        created = client.post(
            "/api/tasks",
            json={
                "objective": "Render through the plugin",
                "runner_id": "rendered-task.runner",
            },
        ).json()
        path = f"/api/tasks/{created['task_id']}"
        assert client.post(f"{path}/start").status_code == 202
        for _ in range(100):
            detail = client.get(path).json()
            if detail["task"]["status"] == "completed":
                break
            time.sleep(0.01)
        projection = client.get(f"{path}/projection").json()
        artifact = projection["artifacts"][0]

        assert artifact["preview"]["renderer_id"] == (
            "rendered-task.summary-renderer"
        )
        content_path = f"{path}/artifacts/{artifact['artifact_id']}/content"
        preview = client.get(content_path)
        download = client.get(
            content_path,
            params={"disposition": "attachment"},
        )

        assert preview.text == "Rendered plugin summary"
        assert preview.headers["x-qwenpaw-artifact-renderer"] == (
            "rendered-task.summary-renderer"
        )
        assert download.content == b"Render through the plugin"
        assert download.headers["x-qwenpaw-artifact-renderer"] == (
            "qwenpaw.system.tasks.safe-artifact-renderer"
        )


def test_missing_selected_runner_fails_without_console_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    client = _client(monkeypatch, tmp_path, workspace)
    created = client.post(
        "/api/tasks",
        json={
            "objective": "Require an unavailable runner",
            "runner_id": "missing.runner",
        },
    ).json()

    response = client.post(f"/api/tasks/{created['task_id']}/start")

    assert response.status_code == 409
    assert response.json()["code"] == "runner_unavailable"
    detail = client.get(f"/api/tasks/{created['task_id']}").json()
    assert detail["task"]["status"] == "planned"
    assert detail["runs"] == []


def test_plugin_sensor_api_creates_approval_gated_task(
    monkeypatch,
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "task-insights",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "review-sensor",
                    "slot": "sensor",
                    "entrypoint": "task_insights.sensor:create_sensor",
                },
            ],
        },
    )
    asyncio.run(
        registry.activate(manifest, lambda _: _ApiReviewSensor()),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    client = _client(
        monkeypatch,
        tmp_path,
        workspace,
        capability_registry=registry,
    )

    response = client.post(
        "/api/tasks/sensors/task-insights.review-sensor/poll",
    )

    assert response.status_code == 202
    task = response.json()["items"][0]
    assert task["status"] == "waiting_approval"
    assert task["source"] == "sensor"
    assert task["metadata"]["sensor_id"] == ("task-insights.review-sensor")
    events = client.get(
        f"/api/tasks/{task['task_id']}/events",
    ).json()["items"]
    assert events[-1]["event_type"] == "approval.requested"


def test_create_rejects_missing_project_directory(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/tasks",
        json={
            "objective": "Use a missing project",
            "project_dir": str(tmp_path / "missing"),
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_project_dir"


def test_task_creation_persists_explicit_approval_level(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/tasks",
        json={
            "objective": "Review every tool call",
            "approval_level": "strict",
        },
    )

    assert response.status_code == 201
    assert response.json()["metadata"]["approval_level"] == "strict"


def test_task_creation_persists_explicit_strategy(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/tasks",
        json={
            "objective": "Use a selected runtime strategy",
            "strategy_id": "plugin.focus-strategy",
        },
    )

    assert response.status_code == 201
    assert response.json()["metadata"]["strategy_id"] == (
        "plugin.focus-strategy"
    )


def test_missing_selected_strategy_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    client = _client(monkeypatch, tmp_path)
    created = client.post(
        "/api/tasks",
        json={
            "objective": "Require an unavailable strategy",
            "strategy_id": "missing.strategy",
        },
    ).json()

    response = client.post(f"/api/tasks/{created['task_id']}/start")

    assert response.status_code == 409
    assert response.json()["code"] == "strategy_unavailable"
    detail = client.get(f"/api/tasks/{created['task_id']}").json()
    assert detail["task"]["status"] == "planned"
    assert detail["runs"] == []


def test_cancel_stops_running_console_task(monkeypatch, tmp_path) -> None:
    class BlockingConsoleChannel:
        async def stream_one(self, _payload):
            yield "data: {}\n\n"
            await asyncio.Event().wait()

    class ChannelManager:
        async def get_channel(self, name):
            return BlockingConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=ChannelManager(),
    )
    with _client(monkeypatch, tmp_path, workspace) as client:
        created = client.post(
            "/api/tasks",
            json={"objective": "Cancel the live task"},
        ).json()
        path = f"/api/tasks/{created['task_id']}"
        assert client.post(f"{path}/start").status_code == 202

        cancelled = client.post(f"{path}/cancel")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        supervisor = client.app.state.lite_task_runtime_supervisor
        assert not supervisor.contains(UUID(created["task_id"]))


def test_console_runtime_timeout_becomes_durable_failure(
    monkeypatch,
    tmp_path,
) -> None:
    class BlockingConsoleChannel:
        async def stream_one(self, _payload):
            yield (
                'data: {"object":"content","type":"text",'
                '"delta":true,"text":"Working"}\n\n'
            )
            await asyncio.Event().wait()

    class ChannelManager:
        async def get_channel(self, name):
            return BlockingConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=ChannelManager(),
    )
    monkeypatch.setattr(
        task_runtime_app,
        "DEFAULT_STREAM_TASK_TIMEOUT_SECONDS",
        0.01,
    )

    with _client(monkeypatch, tmp_path, workspace) as client:
        created = client.post(
            "/api/tasks",
            json={"objective": "Fail a stalled Console stream"},
        ).json()
        path = f"/api/tasks/{created['task_id']}"

        assert client.post(f"{path}/start").status_code == 202
        for _ in range(100):
            detail = client.get(path).json()
            if detail["task"]["status"] == "failed":
                break
            time.sleep(0.01)

        assert detail["task"]["status"] == "failed"
        assert detail["runs"][-1]["status"] == "failed"
        events = client.get(f"{path}/events").json()["items"]
        assert events[-1]["event_type"] == "run.failed"
        assert events[-1]["payload"] == {
            "error_summary": "TaskExecutionTimeoutError",
        }
        supervisor = client.app.state.lite_task_runtime_supervisor
        assert not supervisor.contains(UUID(created["task_id"]))


async def test_approved_proposal_uses_console_agent_runtime(tmp_path) -> None:
    payloads = []

    class ConsoleChannel:
        async def stream_one(self, payload):
            payloads.append(payload)
            yield "data: {}\n\n"

    class ChannelManager:
        async def get_channel(self, name):
            return ConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=ChannelManager(),
    )
    service = task_service_for_workspace(workspace)
    proposal = Proposal(
        source="proactive.memory",
        objective="Inspect the local report",
        rationale_summary="The report is ready",
        risk=RiskLevel.MEDIUM,
    )
    task = await service.create_task(
        objective=proposal.objective,
        agent_id="default",
        metadata={"proposal": proposal.model_dump(mode="json")},
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Inspect", objective=task.objective),),
    )
    await service.start_task(task.task_id, runner_id="runner.local-agent")
    approval = await service.request_approval(
        task.task_id,
        action="proposal.execute",
        risk=proposal.risk,
        requester=ActorRef(
            type=ActorType.SENSOR,
            id="proactive.memory",
        ),
        redacted_arguments={"proposal_id": str(proposal.proposal_id)},
    )
    decision = await service.decide_approval(
        task.task_id,
        approval.approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Approved",
    )
    order = approved_task_order(
        proposal,
        approval,
        decision,
        task_id=task.task_id,
    )

    await ApprovedProposalDispatcher().execute(
        workspace,
        service,
        order,
    )

    stored = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    assert stored is not None
    assert stored.status.value == "completed"
    assert payloads[0]["meta"]["request_context"] == {
        "task_id": str(task.task_id),
        "approved_proposal": True,
        "durable_task": True,
        "agent_id": "default",
        "task_ledger_workspace_dir": str(tmp_path),
    }
    assert [event.event_type for event in events][-2:] == [
        "runner.dispatched",
        "run.completed",
    ]
