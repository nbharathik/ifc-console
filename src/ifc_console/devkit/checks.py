"""The headless feature checklist behind `ifc-console dev --check`.

Every check is one real HTTP call against a running console, using the
standard library only, so the list exercises the token middleware, the panel
routes, and the full agent tool loop exactly as a browser would. Nothing here
opens a browser tab.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

TIMEOUT = 120.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        return "ok" if self.ok else "FAIL"


@dataclass
class CheckRun:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", *, skipped: bool = False) -> Check:
        check = Check(name=name, ok=ok, detail=detail, skipped=skipped)
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and not check.skipped]

    @property
    def passed(self) -> bool:
        return not self.failures


class Client:
    """A tiny bearer-token HTTP client over urllib."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        json_body: Any = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        token: str | None = "",
        stream: bool = False,
    ) -> tuple[int, Any]:
        body = raw_body
        headers: dict[str, str] = {}
        auth = self.token if token == "" else token
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
        except OSError as exc:
            return 0, {"error": str(exc)}
        text = payload.decode("utf-8", "replace")
        if stream:
            return status, text
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text


def _png() -> bytes:
    """A 1x1 PNG. Small on purpose: this checks the path, not the pixels."""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def sse_events(text: str) -> Iterator[dict[str, Any]]:
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        try:
            yield json.loads(line[6:])
        except json.JSONDecodeError:
            continue


def _stream_summary(text: str) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    answer = []
    tools: list[str] = []
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    proposals: list[dict[str, Any]] = []
    for event in sse_events(text):
        kind = str(event.get("type"))
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "content":
            answer.append(str(event.get("text") or ""))
        elif kind == "tool_call":
            calls.append(event)
        elif kind == "tool_result":
            tools.append(f"{event.get('name')}{'' if event.get('ok') else ' (failed)'}")
            results.append(event)
        elif kind == "proposal":
            proposals.append(event)
        elif kind == "error":
            errors.append(str(event.get("text") or ""))
    return {
        "kinds": kinds,
        "answer": "".join(answer),
        "tools": tools,
        "calls": calls,
        "results": results,
        "errors": errors,
        "proposals": proposals,
    }


def run_checks(base_url: str, token: str, *, model_id: str = "rehearsal-tools") -> CheckRun:
    """Exercise every panel feature once and report what worked."""
    client = Client(base_url, token)
    run = CheckRun()

    status_code, status = client.request("/api/status")
    run.add(
        "session status",
        status_code == 200 and isinstance(status, dict),
        f"HTTP {status_code}" + (f", model {status.get('model')}" if isinstance(status, dict) else ""),
    )
    run.add(
        "demo model open",
        isinstance(status, dict) and bool(status.get("model")),
        str(status.get("model")) if isinstance(status, dict) else "no status",
    )

    unauth_code, _ = client.request("/api/status", token=None)
    run.add("token required", unauth_code == 401, f"HTTP {unauth_code} without a bearer token")

    shell_code, _ = client.request("/viewer", stream=True)
    run.add("viewer shell", shell_code == 200, f"HTTP {shell_code}")
    chat_code, _ = client.request("/chat", stream=True)
    run.add("chat shell", chat_code == 200, f"HTTP {chat_code}")

    providers_code, providers = client.request("/api/chat/providers")
    ids = (
        [row.get("id") for row in providers.get("providers", [])]
        if isinstance(providers, dict)
        else []
    )
    run.add(
        "providers listed",
        providers_code == 200 and "rehearsal" in ids,
        f"HTTP {providers_code}, {len(ids)} providers: {', '.join(str(i) for i in ids)}",
    )

    models_code, models = client.request(
        "/api/chat/models", method="POST", json_body={"provider": "rehearsal"}
    )
    listed = models.get("models", []) if isinstance(models, dict) else []
    run.add("model list", models_code == 200 and bool(listed), f"{len(listed)} rehearsal models")

    caps_code, caps = client.request("/api/agents/capabilities")
    missing = caps.get("missing", []) if isinstance(caps, dict) else ["route missing"]
    run.add(
        "agent capabilities",
        caps_code == 200 and not missing,
        "all present" if caps_code == 200 and not missing else f"HTTP {caps_code}, missing: {missing}",
    )

    agents_code, agents_payload = client.request("/api/agents")
    agents = agents_payload.get("agents", []) if isinstance(agents_payload, dict) else []
    names = [agent.get("name") for agent in agents]
    run.add(
        "agents listed",
        agents_code == 200 and {"general", "docs", "measurement", "review"} <= set(names),
        ", ".join(str(name) for name in names) or f"HTTP {agents_code}",
    )

    blocks_code, blocks_payload = client.request("/api/agents/blocks")
    blocks = blocks_payload.get("blocks", []) if isinstance(blocks_payload, dict) else []
    run.add(
        "capability blocks",
        blocks_code == 200 and len(blocks) >= 5,
        f"{len(blocks)} blocks",
    )

    for agent_name in ("general", "measurement", "docs", "review"):
        ws_code, ws = client.request(f"/api/agents/workspace?agent={agent_name}")
        ok = (
            ws_code == 200
            and isinstance(ws, dict)
            and bool(ws.get("tools"))
            and bool(ws.get("stages"))
            and bool(ws.get("role"))
        )
        detail = (
            f"{len(ws.get('tools', []))} tools, "
            f"{len([b for b in ws.get('blocks', []) if b.get('available')])} blocks, "
            f"{len(ws.get('examples', []))} examples, "
            f"{len(ws.get('writes', []))} write tools"
            if isinstance(ws, dict) and ws_code == 200
            else f"HTTP {ws_code}"
        )
        run.add(f"workspace: {agent_name}", ok, detail)

    plain_code, plain_ws = client.request("/api/agents/workspace?agent=")
    plain_ok = (
        plain_code == 200
        and isinstance(plain_ws, dict)
        and bool(plain_ws.get("plain"))
        and bool(plain_ws.get("tools"))
        and bool(plain_ws.get("stages"))
    )
    run.add(
        "workspace: plain chat",
        plain_ok,
        f"{len(plain_ws.get('tools', []))} tools" if isinstance(plain_ws, dict) else f"HTTP {plain_code}",
    )

    ws_404, _ = client.request("/api/agents/workspace?agent=nope")
    run.add("workspace rejects an unknown agent", ws_404 == 404, f"HTTP {ws_404}")

    skills_code, skills_ws = client.request("/api/agents/workspace?agent=measurement")
    skills = skills_ws.get("skills", []) if isinstance(skills_ws, dict) else []
    run.add(
        "skills listed and indexed into the prompt",
        skills_code == 200
        and any(row.get("name") == "wall-thickness-check" for row in skills),
        f"{len(skills)} skill(s): " + ", ".join(row.get("name", "?") for row in skills[:4]),
    )

    files_code, files_payload = client.request("/api/agents/files?agent=measurement")
    files = files_payload.get("files", []) if isinstance(files_payload, dict) else []
    indexed = [row for row in files if row.get("indexed")]
    run.add(
        "reference files indexed",
        files_code == 200 and bool(indexed),
        f"{len(indexed)}/{len(files)} indexed"
        + (f"; {files_payload.get('problem')}" if isinstance(files_payload, dict) and files_payload.get("problem") else ""),
    )

    upload_code, upload = client.request(
        "/api/agents/upload?agent=docs&name=rehearsal-note.md",
        method="POST",
        raw_body=b"# Rehearsal note\n\nWall thickness is measured across structural layers.\n",
        content_type="text/markdown",
    )
    run.add(
        "document upload and index",
        upload_code == 200 and isinstance(upload, dict) and bool(upload.get("indexed")),
        f"HTTP {upload_code}"
        + (f", {upload.get('records')} records" if isinstance(upload, dict) and upload.get("indexed") else "")
        + (f", {upload.get('error')}" if isinstance(upload, dict) and upload.get("error") else ""),
    )

    # The image a user drags into the chat must reach the model as pixels, not
    # as a filename. This uploads one and sends it as an attachment.
    image_code, image_upload = client.request(
        "/api/agents/upload?agent=measurement&name=rehearsal-shot.png",
        method="POST",
        raw_body=_png(),
        content_type="image/png",
    )
    attachment = (
        (image_upload.get("attachment") or {}).get("path")
        if isinstance(image_upload, dict)
        else None
    )
    run.add(
        "image upload",
        image_code == 200 and bool(attachment),
        f"HTTP {image_code}, attachment {attachment or '(none)'}",
    )

    custom_code, custom = client.request(
        "/api/agents/custom",
        method="POST",
        json_body={
            "title": "Rehearsal Auditor",
            "description": "Checks the rehearsal scenario end to end.",
            "instructions": "Report the walls and cite the manual page for every value.",
            "blocks": ["ifc-context", "documents", "measurements"],
            "starters": ["List the walls"],
        },
    )
    custom_name = custom.get("agent", {}).get("name") if isinstance(custom, dict) else None
    run.add(
        "custom agent created",
        custom_code == 201 and bool(custom_name),
        str(custom_name or custom),
    )

    _approval_results: dict[bool, dict[str, Any]] = {}

    def approval_check(decision: bool) -> dict[str, Any]:
        """Run something protected, answer the question it stops on.

        Streamed incrementally on purpose: the server is blocked until the
        decision is posted, so a reader that waits for the whole body first
        would wait forever.
        """
        import threading

        client.request(
            "/api/session/mode",
            method="POST",
            json_body={"mode": "edit", "autonomy": "approval", "confirmed": True},
        )
        request = urllib.request.Request(
            f"{base_url}/api/agents/stream",
            data=json.dumps(
                {
                    "provider": "rehearsal",
                    "model": model_id,
                    "prompt": "Measure the interior wall thickness and propose writing it back.",
                    "agent": "measurement",
                }
            ).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        counts = {"asked": 0, "decided": 0, "proposals": 0, "done": False}
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    kind = payload.get("type")
                    if kind == "approval":
                        counts["asked"] += 1
                        threading.Thread(
                            target=client.request,
                            args=("/api/agents/approve",),
                            kwargs={
                                "method": "POST",
                                "json_body": {
                                    "request_id": payload.get("request_id"),
                                    "approved": decision,
                                },
                            },
                            daemon=True,
                        ).start()
                    elif kind == "approval_decided":
                        counts["decided"] += 1
                    elif kind == "proposal":
                        counts["proposals"] += 1
                    elif kind == "done":
                        counts["done"] = True
                        break
        except OSError:
            pass
        _approval_results[decision] = counts
        return counts

    def stream_check(
        label: str,
        agent: str | None,
        prompt: str,
        *,
        expect_tools: tuple[str, ...] = (),
        expect_proposal: bool = False,
        attachments: tuple[str, ...] = (),
        instructions: str = "",
    ) -> dict[str, Any]:
        if agent:
            path = "/api/agents/stream"
            body: dict[str, Any] = {
                "provider": "rehearsal",
                "model": model_id,
                "prompt": prompt,
                "agent": agent,
            }
            if attachments:
                body["attachments"] = list(attachments)
            if instructions:
                body["additional_instructions"] = instructions
        else:
            path = "/api/chat/stream"
            body = {
                "provider": "rehearsal",
                "model": model_id,
                "turns": [{"role": "user", "text": prompt}],
                "tools": True,
            }
        code, text = client.request(path, method="POST", json_body=body, stream=True)
        summary = _stream_summary(text if isinstance(text, str) else "")
        ran = {name.split(" ")[0] for name in summary["tools"]}
        failed_tools = [name for name in summary["tools"] if "failed" in name]
        missing = [name for name in expect_tools if name not in ran]
        ok = (
            code == 200
            and bool(summary["answer"].strip())
            and not summary["errors"]
            and not failed_tools
            and not missing
            and (not expect_proposal or summary["proposals"])
        )
        detail = f"HTTP {code}, {len(summary['answer'])} chars, {len(ran)} tools"
        if missing:
            detail += f", missing: {', '.join(missing)}"
        if failed_tools:
            detail += f", failed: {', '.join(failed_tools)}"
        if expect_proposal:
            detail += f", proposals: {len(summary['proposals'])}"
        if summary["errors"]:
            detail += f", errors: {summary['errors'][0]}"
        run.add(label, ok, detail)
        return summary

    stream_check("plain chat stream", None, "What is in this model?")
    stream_check(
        "general agent stream",
        "general",
        "What is in this model, and what do the documents say about it?",
        expect_tools=("query_elements", "list_project_documents"),
    )
    docs = stream_check(
        "docs agent stream",
        "docs",
        "What does the manual say about wall thickness?",
        expect_tools=("list_project_documents", "get_project_document_page"),
    )
    run.add(
        "pdf page vision path",
        "get_project_document_page" in {name.split(" ")[0] for name in docs["tools"]},
        "the agent rendered a PDF page as an image",
    )
    # From here the runs are expected to complete unattended, so the session
    # is put in auto. The approval path gets its own checks below.
    client.request(
        "/api/session/mode",
        method="POST",
        json_body={"mode": "ask", "autonomy": "auto", "confirmed": True},
    )
    measure = stream_check(
        "measurement agent stream",
        "measurement",
        "Measure the interior wall thickness and propose writing it back.",
        expect_tools=("measure_elements", "get_measurement_recipe"),
        expect_proposal=True,
    )
    first = measure["proposals"][0] if measure["proposals"] else {}
    run.add(
        "ai-marked proposal preview",
        bool(first) and bool(first.get("marked")) and bool(first.get("method")),
        (
            f"pset {first.get('pset')}, method {first.get('method') or '(missing)'}, "
            f"marked {bool(first.get('marked'))}"
            if first
            else "no proposal event reached the panel"
        ),
    )
    run.add(
        "token usage reported",
        bool(measure["kinds"].get("usage")),
        f"{measure['kinds'].get('usage', 0)} usage events",
    )

    for decision, label in ((True, "approved"), (False, "denied")):
        outcome = approval_check(decision)
        run.add(
            f"a protected call can be {label}",
            outcome["asked"] == 1 and outcome["done"] and outcome["decided"] == 1,
            (
                f"asked {outcome['asked']}, decided {outcome['decided']}, "
                f"finished {outcome['done']}, proposals {outcome['proposals']}"
            ),
        )
    run.add(
        "a denied call produces no proposal",
        _approval_results[False]["proposals"] == 0
        and _approval_results[True]["proposals"] >= 1,
        (
            f"approved -> {_approval_results[True]['proposals']} proposal(s), "
            f"denied -> {_approval_results[False]['proposals']}"
        ),
    )
    client.request(
        "/api/session/mode",
        method="POST",
        json_body={"mode": "ask", "autonomy": "auto", "confirmed": True},
    )

    # The panel prints the call arguments and the returned envelope beside each
    # tool, so a run whose events carry neither would render empty cards.
    with_args = [call for call in measure["calls"] if str(call.get("arguments") or "").strip("{} \n")]
    with_preview = [row for row in measure["results"] if str(row.get("preview") or "").strip()]
    run.add(
        "tool calls carry their arguments",
        bool(with_args),
        f"{len(with_args)}/{len(measure['calls'])} calls have arguments",
    )
    run.add(
        "tool results carry a readable preview",
        len(with_preview) == len(measure["results"]) and bool(with_preview),
        f"{len(with_preview)}/{len(measure['results'])} results have a preview",
    )
    oversized = [
        row for row in measure["results"] if len(str(row.get("preview") or "")) > 4000
    ]
    run.add(
        "tool previews stay small enough to render",
        not oversized,
        "no preview over 4000 chars" if not oversized else f"{len(oversized)} oversized previews",
    )
    # The panel opens a failed card and prints the console's message in it, so
    # a failure has to arrive with more than the word "failed".
    failure_code, failure_text = client.request(
        "/api/agents/stream",
        method="POST",
        json_body={
            "provider": "rehearsal",
            "model": model_id,
            "prompt": "Please rehearse a tool failure so the panel can show one.",
            "agent": "general",
        },
        stream=True,
    )
    failure = _stream_summary(failure_text if isinstance(failure_text, str) else "")
    bad = [row for row in failure["results"] if not row.get("ok")]
    run.add(
        "a failed tool explains itself",
        failure_code == 200 and bool(bad) and all(row.get("detail") for row in bad),
        f"{len(bad)} failed call(s)"
        + (f", first: {bad[0].get('summary')}: {bad[0].get('detail', '')[:60]}" if bad else ""),
    )

    if attachment:
        attached = stream_check(
            "image attachment reaches the agent",
            "measurement",
            "What does this photograph show about the wall?",
            attachments=(attachment,),
        )
        received = "image(s) as pixels" in attached["answer"]
        run.add(
            "attached image arrives as pixels",
            received,
            "the model was handed the image content"
            if received
            else "the attachment never became vision input",
        )
    else:
        run.add("image attachment reaches the agent", True, "skipped: no upload", skipped=True)

    instructed = stream_check(
        "standing instructions accepted",
        "measurement",
        "List the walls.",
        instructions="Always report values in millimetres and cite the manual page.",
    )
    run.add(
        "instructions do not break the run",
        bool(instructed["answer"].strip()) and not instructed["errors"],
        "the agent rebuilt with the user's system prompt",
    )

    if custom_name:
        stream_check(
            "custom agent stream",
            custom_name,
            "List the walls you can see.",
            expect_tools=("query_elements",),
        )
    else:
        run.add("custom agent stream", True, "skipped: no custom agent", skipped=True)

    if custom_name:
        removed_code, removed = client.request(
            "/api/agents/custom/delete", method="POST", json_body={"name": custom_name}
        )
        run.add(
            "custom agent deleted",
            removed_code == 200 and isinstance(removed, dict) and removed.get("ok") is True,
            f"HTTP {removed_code}",
        )
        builtin_code, _ = client.request(
            "/api/agents/custom/delete", method="POST", json_body={"name": "measurement"}
        )
        run.add(
            "built-in agents cannot be deleted",
            builtin_code in (400, 403),
            f"HTTP {builtin_code}",
        )

    delete_code, delete_payload = client.request(
        "/api/agents/thread/delete", method="POST", json_body={"thread_id": "panel-doesnotexist"}
    )
    run.add(
        "thread delete route",
        delete_code == 200 and isinstance(delete_payload, dict),
        f"HTTP {delete_code}",
    )

    return run


def render(run: CheckRun) -> str:
    width = max((len(check.name) for check in run.checks), default=10)
    lines = [f"{check.status:<4}  {check.name:<{width}}  {check.detail}" for check in run.checks]
    total = len(run.checks)
    failed = len(run.failures)
    lines.append("")
    lines.append(
        f"{total - failed}/{total} checks passed" + (f", {failed} failed" if failed else "")
    )
    return "\n".join(lines)


__all__ = ["Check", "CheckRun", "Client", "render", "run_checks", "sse_events"]
