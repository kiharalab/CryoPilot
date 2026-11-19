"""Tests for usage and cost, the spending limit, alerts, run reports, and transcript export.

Everything runs offline: real child processes stand in for agent drivers, a
local HTTP server for the webhook, and a fake class for the mail server.

Run from the repository root:
    .venv/bin/python -m pytest app/tests/test_spending_alerts_report.py -q
"""

from __future__ import annotations

import json
import smtplib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cryosparc"
for path in (REPO_ROOT, REPO_ROOT / "reconstruction_agent", REPO_ROOT / "app" / "server" / "drivers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fastapi.testclient import TestClient  # noqa: E402

import llm_client  # noqa: E402
import protocol  # noqa: E402
from app.server import alerts, auth, cryosparc_jobs, pricing, report, state  # noqa: E402
from app.server.main import app  # noqa: E402
from app.server.processes import AgentProcess  # noqa: E402
from app.server.state import processes, store  # noqa: E402

HEADERS = {"Authorization": f"Bearer {auth.TOKEN}"}
SECRET_URL = "https://hooks.slack.com/services/T000/B000/not-a-real-secret"


@pytest.fixture
def client():
    return TestClient(app, headers=HEADERS)


@pytest.fixture
def project():
    return store.create_project(title=f"Report test {time.time_ns()}")


@pytest.fixture
def clean_alerts():
    store.set_setting("alerts", {})
    state.credentials.set(alerts.WEBHOOK_SECRET, None)
    state.credentials.set(alerts.SMTP_SECRET, None)
    yield
    store.set_setting("alerts", {})
    state.credentials.set(alerts.WEBHOOK_SECRET, None)
    state.credentials.set(alerts.SMTP_SECRET, None)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def modeling_session(project, **config):
    return store.create_session(project_id=project["id"], kind="modeling", title="Build aldolase", config={"model": "claude-sonnet-5-5", **config})


# --------------------------------------------------------------- token counts
def test_claude_counts_are_kept_apart_and_the_long_cache_write_is_recognised():
    usage = SimpleNamespace(
        input_tokens=100,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=300,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=200, ephemeral_5m_input_tokens=100),
        output_tokens=40,
    )
    assert llm_client.usage_record(usage, "claude-opus-5-5") == {
        "model": "claude-opus-5-5",
        "input_tokens": 100,
        "cached_tokens": 5000,
        "cache_write_tokens": 100,
        "cache_write_long_tokens": 200,
        "output_tokens": 40,
    }


def test_openai_input_includes_its_cache_counts():
    usage = SimpleNamespace(
        input_tokens=1000,
        input_tokens_details=SimpleNamespace(cached_tokens=600, cache_write_tokens=100),
        output_tokens=50,
    )
    record = llm_client.usage_record(usage, "gpt-6.1-sol")
    assert (record["input_tokens"], record["cached_tokens"], record["cache_write_tokens"], record["output_tokens"]) == (300, 600, 100, 50)
    assert llm_client.usage_record(None, "gpt-5.5") is None


def test_create_response_reports_usage_and_waits_for_the_gate(monkeypatch):
    harness = pytest.importorskip("harness")
    usage = SimpleNamespace(input_tokens=10, input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0), output_tokens=5)
    order = []

    class FakeClient:
        def create(self, **request):
            order.append("request")
            return SimpleNamespace(usage=usage, model="gpt-5.5-2026-04-23")

    monkeypatch.setattr(harness, "client", FakeClient())
    monkeypatch.setattr(harness, "before_model_request", lambda: order.append("gate"))
    records = []
    monkeypatch.setattr(harness, "on_model_usage", records.append)
    harness.create_response(model="gpt-5.5")
    assert order == ["gate", "request"]
    assert records[0]["model"] == "gpt-5.5-2026-04-23"
    assert records[0]["output_tokens"] == 5


# --------------------------------------------------------------------- prices
def test_estimates_use_each_kind_of_token_at_its_own_price():
    record = {"input_tokens": 1_000_000, "cached_tokens": 1_000_000, "cache_write_tokens": 1_000_000, "cache_write_long_tokens": 1_000_000, "output_tokens": 1_000_000}
    # Sonnet 5.5: $2 in, $0.10 cache read, $2.50 and $4 cache writes, $10 out.
    assert pricing.estimate("claude-sonnet-5-5", record) == pytest.approx(2 + 0.1 + 2.5 + 4 + 10)


def test_openai_long_requests_cost_more_and_snapshots_have_the_model_price():
    short = pricing.estimate("gpt-5.5", {"input_tokens": 100_000, "output_tokens": 1000})
    assert short == pytest.approx(0.1 * 5 + 0.001 * 30)
    long = pricing.estimate("gpt-5.5", {"input_tokens": 300_000, "output_tokens": 1000})
    assert long == pytest.approx(0.3 * 10 + 0.001 * 45)
    assert pricing.estimate("gpt-5.5-2026-04-23", {"output_tokens": 1000}) == pytest.approx(0.03)


def test_unknown_models_have_no_estimate_and_ollama_is_free():
    assert pricing.estimate("some-new-model", {"output_tokens": 1000}) is None
    assert pricing.estimate("gemma4:12b-mlx", {"output_tokens": 1000}, provider="ollama") == 0.0


def test_every_model_the_app_suggests_has_a_price():
    source = (REPO_ROOT / "app/frontend/src/features/setup/services.jsx").read_text()
    import re

    lists = re.findall(r"export const (?:OPENAI|CLAUDE)_MODELS = \[(.*?)\]", source)
    suggested = re.findall(r"'([\w.-]+)'", " ".join(lists))
    assert "gpt-6.1-sol" in suggested and "claude-opus-5-5" in suggested
    for model in suggested:
        assert pricing.price_for(model), model


# ------------------------------------------------------------ usage and limit
def usage_event(model="claude-sonnet-5-5", output=1_000_000):
    return {"event": "usage", "model": model, "input_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "cache_write_long_tokens": 0, "output_tokens": output}


def test_usage_adds_up_per_model_and_reaches_the_session_view(project):
    session = modeling_session(project)
    state.on_driver_event(session["id"], usage_event())
    state.on_driver_event(session["id"], usage_event(output=500_000))
    state.on_driver_event(session["id"], usage_event(model="mystery"))
    view = state.session_runtime_view(store.get_session(session["id"]))["usage"]
    assert view["requests"] == 3
    assert view["output_tokens"] == 2_500_000
    assert view["cost_usd"] == pytest.approx(15.0)
    assert view["unpriced_models"] == ["mystery"]
    assert view["over_limit"] is False


def test_reaching_the_limit_pauses_the_agent_and_raising_it_resumes(project, client, monkeypatch):
    session = modeling_session(project, spending_limit_usd=5.0)
    sent = []
    monkeypatch.setattr(processes, "pause", lambda session_id, detail="": sent.append(("pause", detail)))
    monkeypatch.setattr(processes, "resume", lambda session_id: sent.append(("resume", "")))
    state.on_driver_event(session["id"], usage_event(output=100_000))
    assert sent == []
    state.on_driver_event(session["id"], usage_event(output=500_000))
    assert sent[-1][0] == "pause"
    assert "$6.00 of $5.00" in sent[-1][1]

    refused = client.post(f"/api/sessions/{session['id']}/messages", json={"content": "go on"})
    assert refused.status_code == 409
    assert refused.json()["spending_limit"] is True
    assert client.post(f"/api/sessions/{session['id']}/start").status_code == 409

    raised = client.patch(f"/api/sessions/{session['id']}", json={"spending_limit_usd": 20})
    assert raised.status_code == 200
    assert raised.json()["usage"]["limit_usd"] == 20
    assert raised.json()["usage"]["over_limit"] is False
    assert sent[-1][0] == "resume"

    # Lowering it under what has been spent pauses again; no limit lets it go.
    client.patch(f"/api/sessions/{session['id']}", json={"spending_limit_usd": 1})
    assert sent[-1][0] == "pause"
    cleared = client.patch(f"/api/sessions/{session['id']}", json={"spending_limit_usd": None})
    assert cleared.json()["usage"]["limit_usd"] is None
    assert sent[-1][0] == "resume"


@pytest.mark.parametrize("bad", ["abc", -1, 0])
def test_a_bad_limit_is_refused(project, client, bad):
    session = modeling_session(project)
    assert client.patch(f"/api/sessions/{session['id']}", json={"spending_limit_usd": bad}).status_code == 422
    assert client.put("/api/settings", json={"defaults": {"spending_limit_usd": bad}}).status_code == 422


def test_new_sessions_take_the_default_limit_and_changing_it_leaves_them_alone(client):
    assert client.put("/api/settings", json={"defaults": {"spending_limit_usd": "25"}}).json()["defaults"]["spending_limit_usd"] == 25
    try:
        assert state.defaults()["spending_limit_usd"] == 25
    finally:
        assert client.put("/api/settings", json={"defaults": {"spending_limit_usd": ""}}).json()["defaults"]["spending_limit_usd"] is None


def test_sessions_left_working_by_a_stopped_server_are_settled(project):
    running = store.create_session(project_id=project["id"], kind="modeling", title="r", status="running")
    paused = store.create_session(project_id=project["id"], kind="modeling", title="p", status="paused")
    failed = store.create_session(project_id=project["id"], kind="modeling", title="f", status="error")
    store.settle_unfinished_sessions(("running", "paused"))
    assert store.get_session(running["id"])["status"] == "stopped"
    assert store.get_session(paused["id"])["status"] == "stopped"
    assert store.get_session(failed["id"])["status"] == "error"


# ----------------------------------------------------------------- the gate
def test_gate_holds_until_resumed_and_reports_both_changes():
    states = []
    gate = protocol.RunGate(lambda state, detail="": states.append((state, detail)))
    gate.hold()  # an open gate does nothing
    assert states == []
    gate.pause("Spending limit reached")
    released = threading.Event()

    def worker():
        gate.hold()
        released.set()

    threading.Thread(target=worker, daemon=True).start()
    assert wait_for(lambda: states)
    assert states[0] == ("paused", "Spending limit reached")
    assert not released.wait(0.4)
    gate.resume()
    assert released.wait(2)
    assert states[-1][0] == "running"


def test_pause_reaches_the_gate_during_a_turn_without_the_command_queue(monkeypatch):
    import io

    lines = '{"op":"pause","detail":"limit"}\n{"op":"message","content":"hello"}\n{"op":"resume"}\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    gate = protocol.RunGate(lambda *_: None)
    reader = protocol.CommandReader(gate)
    assert reader.next(timeout=2) == {"op": "message", "content": "hello"}
    assert reader.next(timeout=2) is None
    assert gate._open.is_set()


# ------------------------------------------------------------ driver processes
def fake_driver(code):
    events = []
    process = AgentProcess(
        "s1",
        [sys.executable, "-u", "-c", code],
        cwd=Path("."),
        env={"PATH": "/usr/bin:/bin"},
        on_event=lambda _session, event: events.append(event),
        on_log=lambda *_: None,
    )
    return process, events


def test_a_driver_that_dies_is_an_error_not_a_quiet_stop():
    process, events = fake_driver("import os; print('{\"event\": \"status\", \"state\": \"running\"}', flush=True); os._exit(9)")
    assert wait_for(lambda: len(events) == 2)
    assert events[-1]["state"] == "error"
    assert "stopped unexpectedly (exit code 9)" in events[-1]["detail"]


def test_a_driver_the_app_stopped_is_a_plain_stop():
    process, events = fake_driver("import sys, json; print(json.dumps({'event':'status','state':'idle'}), flush=True); sys.stdin.readline(); sys.exit(3)")
    assert wait_for(lambda: events)
    process.shutdown(timeout=3)
    assert wait_for(lambda: len(events) == 2)
    assert events[-1]["state"] == "stopped"


def test_idle_while_a_message_waits_is_not_reported():
    code = (
        "import json, sys\n"
        "emit = lambda **event: print(json.dumps(event), flush=True)\n"
        "sys.stdin.readline()\n"
        "emit(event='status', state='idle', detail='Ready')\n"
        "emit(event='user_turn_accepted')\n"
        "emit(event='status', state='running', detail='Agent is working')\n"
        "emit(event='status', state='idle', detail='Waiting')\n"
    )
    process, events = fake_driver(code)
    process.message_waiting = True
    process.send({"op": "message", "content": "hi"})
    assert wait_for(lambda: sum(event.get("event") == "status" for event in events) >= 3)
    states = [event["state"] for event in events if event.get("event") == "status"]
    assert states[0] == "running"
    assert "idle" in states[1:]


# --------------------------------------------------------------------- alerts
@pytest.mark.parametrize(
    "message,duration,expected",
    [
        ("All done. The final map is J29.", 600, "finished"),
        ("All done.", 30, None),
        ("Map is at 3.1 Å.\n\nShould I run local refinement next?", 30, "question"),
        ("Map is at 3.1 Å.\n\nLet me know which mask you prefer.", 30, "question"),
        ("Did the 2D step work? Yes.\n\nThe run is complete.", 600, "finished"),
        ("", 600, "finished"),
        ("Done.", None, "finished"),
    ],
)
def test_a_final_message_decides_which_alert_a_turn_sends(message, duration, expected):
    assert alerts.turn_end_event(message, duration) == expected


def test_alert_text_leaves_the_message_out_unless_asked():
    alert = alerts.Alert(event="finished", session_id="abc", session="Env run", project="HIV Env", message="Unpublished result", duration=3 * 3600 + 720, cost_usd=8.2)
    subject, text = alerts.render(alert, {"app_url": "http://zebra:8790/"})
    assert subject == "CryoPilot: Env run finished"
    assert "HIV Env" in text and "3 h 12 min" in text and "$8.20" in text
    assert "http://zebra:8790/?session=abc" in text
    assert "Unpublished" not in text
    _subject, with_message = alerts.render(alert, {"include_message": True})
    assert "Unpublished result" in with_message


def test_a_long_question_keeps_its_end():
    alert = alerts.Alert(event="question", session="s", message="x" * 2000 + " Which mask should I use?")
    _subject, text = alerts.render(alert, {"include_message": True})
    assert text.endswith("Which mask should I use?")
    assert len(text) < 700


class Webhook:
    def __init__(self, status=200):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/secret-path"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def webhook():
    server = Webhook()
    yield server
    server.close()


def sender(settings, secrets, results=None, retry_delays=()):
    return alerts.AlertSender(
        settings=lambda: settings,
        secret=lambda key: secrets.get(key, ""),
        record=(results if results is not None else []).append,
        retry_delays=retry_delays,
    )


def test_an_alert_posts_to_the_webhook_and_records_the_result(webhook):
    results = []
    alert_sender = sender({}, {alerts.WEBHOOK_SECRET: webhook.url}, results)
    alert_sender.enqueue(lambda: alerts.Alert(event="failed", session="Env run"))
    alert_sender.wait_until_idle()
    assert webhook.received == [{"text": "CryoPilot: Env run stopped because of an error"}]
    assert results[-1]["ok"] is True and results[-1]["channel"] == "webhook"


def test_a_turned_off_alert_is_not_sent(webhook):
    alert_sender = sender({"events": {"finished": False}}, {alerts.WEBHOOK_SECRET: webhook.url})
    alert_sender.enqueue(lambda: alerts.Alert(event="finished", session="s"))
    alert_sender.enqueue(lambda: None)
    alert_sender.wait_until_idle()
    assert webhook.received == []


def test_without_a_destination_nothing_is_built():
    built = []
    alert_sender = sender({}, {})
    alert_sender.enqueue(lambda: built.append(1))
    alert_sender.wait_until_idle()
    assert built == []


def test_a_failed_webhook_is_retried_then_reported_without_its_address():
    bad = Webhook(status=500)
    try:
        results = []
        failures = []
        alert_sender = sender({}, {alerts.WEBHOOK_SECRET: bad.url}, results, retry_delays=(0.01,))
        alert_sender.enqueue(lambda: alerts.Alert(event="failed", session="s"), on_failure=failures.append)
        alert_sender.wait_until_idle()
        assert len(bad.received) == 2
        assert results[-1]["ok"] is False
        assert results[-1]["error"] == "The webhook answered HTTP 500."
        assert "secret-path" not in failures[0]
    finally:
        bad.close()


def test_an_unreachable_webhook_keeps_later_alerts_working(webhook):
    results = []
    secrets = {alerts.WEBHOOK_SECRET: "http://127.0.0.1:9/nothing-here"}
    alert_sender = sender({}, secrets, results)
    alert_sender.enqueue(lambda: alerts.Alert(event="failed", session="first"))
    alert_sender.wait_until_idle()
    assert results[-1]["ok"] is False
    secrets[alerts.WEBHOOK_SECRET] = webhook.url
    alert_sender.enqueue(lambda: alerts.Alert(event="failed", session="second"))
    alert_sender.wait_until_idle()
    assert "second" in webhook.received[0]["text"]


def test_discord_gets_its_own_field(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b""

    def urlopen(request, timeout, context):
        sent.update(json.loads(request.data))
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", urlopen)
    alerts.send_webhook("https://discord.com/api/webhooks/1/x", "hello")
    assert sent == {"content": "hello"}


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.tls = self.login_args = self.message = None
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def starttls(self, context=None):
        self.tls = context

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.message = message


def test_email_uses_the_saved_server_and_verified_tls(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    settings = {"email": {"host": "smtp.lab.edu", "port": 587, "security": "starttls", "username": "pi@lab.edu", "to": "a@lab.edu, b@lab.edu"}}
    results = []
    alert_sender = sender(settings, {alerts.SMTP_SECRET: "pw"}, results)
    alert_sender.enqueue(lambda: alerts.Alert(event="paused", session="Env\nrun"))
    alert_sender.wait_until_idle()
    client_ = FakeSmtp.instances[0]
    assert (client_.host, client_.port) == ("smtp.lab.edu", 587)
    assert client_.tls.verify_mode.name == "CERT_REQUIRED"
    assert client_.login_args == ("pi@lab.edu", "pw")
    assert client_.message["To"] == "a@lab.edu, b@lab.edu"
    assert client_.message["From"] == "pi@lab.edu"
    assert client_.message["Subject"] == "CryoPilot: Env run paused at its spending limit"
    assert results[-1]["ok"] is True


def test_settings_never_return_a_secret_and_check_what_they_are_given(client, clean_alerts):
    saved = client.put("/api/settings", json={"alerts": {"webhook_url": SECRET_URL, "app_url": "http://zebra:8790", "email": {"host": "smtp.lab.edu", "to": "a@lab.edu", "password": "hunter2"}, "events": {"finished": False}}})
    assert saved.status_code == 200
    assert "not-a-real-secret" not in saved.text and "hunter2" not in saved.text
    view = saved.json()["alerts"]
    assert view["webhook"] == {"configured": True, "host": "hooks.slack.com", "source": "app"}
    assert view["email"]["password_set"] is True and view["email"]["configured"] is True
    assert view["events"] == {"finished": False, "question": True, "failed": True, "paused": True}
    assert view["include_message"] is False
    assert client.get("/api/setup").json()["alerts"]["app_url"] == "http://zebra:8790"

    for bad in ({"webhook_url": "ftp://example.org/x"}, {"email": {"to": "not an address"}}, {"email": {"port": "many"}}, {"email": {"security": "tls13"}}, {"events": {"never": True}}):
        assert client.put("/api/settings", json={"alerts": bad}).status_code == 422, bad

    assert client.put("/api/settings", json={"alerts": {"clear_webhook": True, "email": {"clear": True}}}).json()["alerts"]["webhook"]["configured"] is False
    assert not state.credentials.is_set(alerts.SMTP_SECRET)


def test_alert_secrets_never_reach_the_agents(monkeypatch):
    monkeypatch.setenv("CRYOPILOT_ALERT_WEBHOOK_URL", SECRET_URL)
    monkeypatch.setenv("CRYOPILOT_ALERT_SMTP_PASSWORD", "hunter2")
    env = processes._base_env()
    assert "CRYOPILOT_ALERT_WEBHOOK_URL" not in env and "CRYOPILOT_ALERT_SMTP_PASSWORD" not in env


def test_the_test_button_reports_each_destination(client, clean_alerts, webhook, monkeypatch):
    assert client.post("/api/settings/alerts/test").status_code == 400
    FakeSmtp.instances = []

    class Refusing(FakeSmtp):
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, b"no")

    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    client.put("/api/settings", json={"alerts": {"webhook_url": webhook.url, "email": {"host": "smtp.lab.edu", "to": "a@lab.edu", "username": "pi"}}})
    results = client.post("/api/settings/alerts/test").json()["results"]
    assert results[0] == {"channel": "webhook", "ok": True}
    assert results[1]["ok"] is False
    assert "rejected the user name or password" in results[1]["error"]
    assert "test alert" in webhook.received[0]["text"]


def test_a_finished_run_alerts_with_its_cost_and_a_stop_by_the_user_does_not(project, clean_alerts, webhook, monkeypatch):
    monkeypatch.setattr(alerts, "FINISHED_MINIMUM_SECONDS", 0)
    state.credentials.set(alerts.WEBHOOK_SECRET, webhook.url)
    session = modeling_session(project)
    state.on_driver_event(session["id"], {"event": "status", "state": "running"})
    state.on_driver_event(session["id"], usage_event(output=1_000_000))
    state.on_driver_event(session["id"], {"event": "answer", "text": "The model is done."})
    state.on_driver_event(session["id"], {"event": "turn_end", "outcome": "completed"})
    state.alert_sender.wait_until_idle()
    assert len(webhook.received) == 1
    assert "Build aldolase finished" in webhook.received[0]["text"]
    assert "$10.00" in webhook.received[0]["text"]

    state.on_driver_event(session["id"], {"event": "status", "state": "running"})
    state.on_driver_event(session["id"], {"event": "turn_end", "outcome": "interrupted"})
    state.alert_sender.wait_until_idle()
    assert len(webhook.received) == 1

    state.on_driver_event(session["id"], {"event": "status", "state": "error", "detail": "boom"})
    state.on_driver_event(session["id"], {"event": "status", "state": "paused", "detail": "limit"})
    state.alert_sender.wait_until_idle()
    assert "stopped because of an error" in webhook.received[1]["text"]
    assert "paused at its spending limit" in webhook.received[2]["text"]


def test_a_question_alerts_even_on_a_short_turn(project, clean_alerts, webhook):
    state.credentials.set(alerts.WEBHOOK_SECRET, webhook.url)
    session = modeling_session(project)
    state.on_driver_event(session["id"], {"event": "status", "state": "running"})
    state.on_driver_event(session["id"], {"event": "answer", "text": "Which chain is the Fab?"})
    state.on_driver_event(session["id"], {"event": "turn_end", "outcome": "completed"})
    state.alert_sender.wait_until_idle()
    assert "is waiting for your answer" in webhook.received[0]["text"]


# ------------------------------------------------------------ reconstruction report
def fixture(uid):
    return json.loads((FIXTURES / f"{uid}.json").read_text())


def job_doc(uid, job_type, parents, *, status="completed", counts=None, params=None, title=""):
    outputs = {name: {"type": "particle", "num_items": count} for name, count in (counts or {}).items()}
    return {
        "uid": uid,
        "project_uid": "P61",
        "workspace_uids": ["W1"],
        "spec": {"type": job_type, "params": {}, "outputs": outputs},
        "params_spec": {name: {"value": value} for name, value in (params or {}).items()},
        "title": title,
        "status": status,
        "parents": parents,
        "version": "v5.0.2",
        "started_at": {"$date": "2026-10-14T10:00:00.000Z"},
        "completed_at": {"$date": "2026-10-14T10:30:00.000Z"},
        "deleted": False,
    }


def p61_jobs():
    """J29's real record, its chain, a dead branch, and jobs run on its result."""

    final = fixture("J29")
    final.update({"params_spec": {"refine_symmetry": {"value": "C3"}, "random_seed": {"value": 43}, "random_seed_default": {"value": 1}}, "version": "v5.0.2", "parents": ["J28"]})
    final["spec"]["params"] = {"refine_symmetry": "C3"}
    final["spec"]["outputs"]["particles"] = {"type": "particle", "num_items": 222279}
    final["instance_information"] = {"gpu_info": [{"name": "NVIDIA RTX PRO 6000"}], "platform_node": "zebra"}
    # The trimmed record has no curves; these are shaped like the real ones.
    final["spec"]["outputs"]["volume"]["latest_summary_stats"]["fsc_info_best"].update(
        {"radwns": [0.5, 1.5, 2.5], "fsc_nomask": [1.0, 0.8, 0.1], "fsc_resmask": [1.0, 0.9, 0.2]}
    )
    sharpen = fixture("J32")
    sharpen["spec"]["outputs"]["volume"]["latest_summary_stats"] = {"b_factor": -117.76}
    validation = fixture("J31")
    validation["spec"]["outputs"]["volume"]["latest_summary_stats"] = {"fsc_resolution": 3.098}
    return [
        job_doc("J1", "import_movies", [], counts={"imported_movies": 10524}),
        job_doc("J2", "patch_motion_correction_multi", ["J1"]),
        job_doc("J3", "patch_ctf_estimation_multi", ["J2"]),
        job_doc("J4", "curate_exposures_v2", ["J3"], counts={"exposures_accepted": 9047, "exposures_rejected": 1477}),
        job_doc("J5", "blob_picker_gpu", ["J4"], counts={"particles": 5184160}),
        job_doc("J6", "extract_micrographs_multi", ["J5"], counts={"particles": 752650}, params={"box_size_pix": 480, "bin_size_pix": 320}),
        job_doc("J7", "class_2D_new", ["J6"], counts={"particles": 752248}, params={"class2D_K": 200}),
        job_doc("J8", "select_2D", ["J7"], counts={"particles_selected": 569900}),
        # A branch the agent gave up on, with a better number than it should have.
        job_doc("J12", "homo_abinit", ["J6"], counts={"particles": 100}),
        job_doc("J17", "homo_abinit", ["J8"]),
        job_doc("J25", "nonuniform_refine_new", ["J17"], counts={"particles": 222402}),
        job_doc("J28", "ctf_refine_local", ["J25"], counts={"particles": 222402}),
        job_doc("J40", "queued_job_type", ["J29"], status="building"),
        final,
        validation,
        sharpen,
    ]


def bound_session(project, tmp_path, docs, harness_id="h1"):
    parent = tmp_path / "cryosparc"
    project_dir = parent / "CS-p61"
    for doc in docs:
        job_dir = project_dir / doc["uid"]
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps(doc))
    info = json.loads((FIXTURES / "project.json").read_text())
    info.update({"project_dir": str(project_dir), "description": f"cryopilot-session:{harness_id}"})
    (project_dir / "project.json").write_text(json.dumps(info))
    return store.create_session(
        project_id=project["id"],
        kind="reconstruction",
        title="Env rerun",
        harness_session_id=harness_id,
        config={
            "project_uid": "P61",
            "workspace_uid": "W1",
            "agent": {"cryosparc_url": "http://localhost:61000", "model": "gpt-5.5", "reasoning_effort": "xhigh"},
            "processing_guide": "original",
            "manifest": {
                "project": {"parent_directory": str(parent), "title": "Env"},
                "user_goal": "Best map of Env",
                "acquisition": {"pixel_size_A": 0.83, "accelerating_voltage_kV": 300.0, "spherical_aberration_mm": 2.7, "total_dose_e_per_A2": 50.0},
            },
        },
    )


@pytest.fixture
def no_cache():
    for cache in (cryosparc_jobs._dir_cache, cryosparc_jobs._parsed_jobs):
        cache.clear()
    yield


@pytest.fixture
def no_harness(monkeypatch):
    """No harness session database: the report works from the app's own records."""

    def missing(*_args, **_kwargs):
        raise report.TraceError("no database")

    monkeypatch.setattr(report.trace_reader, "session", missing)
    monkeypatch.setattr(report.trace_reader, "last_assistant_text", missing)


def test_the_report_finds_the_final_map_and_only_its_chain(project, tmp_path, client, no_cache, no_harness):
    session = bound_session(project, tmp_path, p61_jobs())
    state.on_driver_event(session["id"], usage_event(model="gpt-5.5", output=100_000))
    built = client.get(f"/api/sessions/{session['id']}/report").json()
    markdown = built["markdown"]
    assert built["final"]["job_uid"] == "J29"
    assert built["final"]["resolution_A"] == pytest.approx(3.089, abs=0.001)
    assert built["error"] is None
    assert "J29, Non-uniform Refinement: 3.09 Å" in markdown
    # FSC for every mask, from the job's own record.
    assert "| Tight mask | 3.12 | 3.53 |" in markdown
    assert "| No mask | 3.59 | 4.04 |" in markdown
    assert "a cFAR of 0.77" in markdown
    assert built["fsc_curve"]["frequency"]
    assert {"No mask", "Tight mask"} <= {curve["label"] for curve in built["fsc_curve"]["curves"]}
    # Parameters that were set, without the seed CryoSPARC chose by itself.
    assert "box_size_pix=480, bin_size_pix=320" in markdown
    assert "refine_symmetry=C3, random_seed=43" in markdown
    assert "random_seed_default" not in markdown
    # Software and cost.
    assert "| CryoSPARC | v5.0.2 |" in markdown
    assert "gpt-5.5, xhigh reasoning effort" in markdown
    assert "NVIDIA RTX PRO 6000" in markdown
    assert "Estimated total: **$3.00**" in markdown


def test_the_methods_draft_describes_the_chain_and_nothing_else(project, tmp_path, client, no_cache, no_harness):
    session = bound_session(project, tmp_path, p61_jobs())
    methods = client.get(f"/api/sessions/{session['id']}/report").json()["methods"]
    assert "CryoSPARC v5.0.2" in methods
    assert "a pixel size of 0.83 Å, an accelerating voltage of 300 kV, a spherical aberration of 2.7 mm, and a total dose of 50 e⁻/Å²" in methods
    assert "kept 9,047 of 10,524 exposures" in methods
    assert "with a box of 480 pixels binned to 320 pixels (J6), which gave 752,650 particles" in methods
    assert "Non-uniform Refinement (J29) with C3 symmetry and 222,279 particles gave a map at 3.09 Å" in methods
    assert "The FSC was validated (J31, 3.10 Å) and the map was sharpened (J32, B-factor -117.8 Å²)." in methods
    # The abandoned branch is listed as a job but is not part of the method.
    assert "J12" not in methods
    assert "J40" not in methods


def test_final_map_choice_prefers_the_flag_then_the_message_then_the_best(no_cache):
    def job(uid, resolution, **extra):
        return {"uid": uid, "type": "nonuniform_refine_new", "parents": [], "status": "completed", "resolution_A": resolution, "resolution_from": None, "is_final_result": False, "volumes": [{}], **extra}

    jobs = [
        job("J10", 2.9),
        job("J20", 3.1),
        job("J21", None, resolution_from=None),
        job("J22", 3.1, resolution_from="J20"),
        # A validation job measures a resolution of its own, a little better here.
        job("J23", 3.0, type="validation", parents=["J20"]),
    ]
    chosen, reason = report.choose_final_job(jobs, "")
    assert chosen["uid"] == "J10" and "best resolution" in reason
    chosen, reason = report.choose_final_job(jobs, "The final map is J20, not the overfit one.")
    assert chosen["uid"] == "J20" and "last message" in reason
    # A sharpened copy named in the message stands for the refinement it came from.
    chosen, _ = report.choose_final_job(jobs, "Use the sharpened map from J22.")
    assert chosen["uid"] == "J20"
    chosen, _ = report.choose_final_job(jobs, "Validation (J23) confirms 3.0 Å.")
    assert chosen["uid"] == "J20"
    jobs[1]["is_final_result"] = True
    chosen, reason = report.choose_final_job(jobs, "J10 is the one.")
    assert chosen["uid"] == "J20" and "marked as the final result" in reason


def test_a_report_with_no_jobs_or_project_still_builds(project, client):
    session = store.create_session(project_id=project["id"], kind="reconstruction", title="Not started", config={})
    built = client.get(f"/api/sessions/{session['id']}/report").json()
    assert "has not created its CryoSPARC project yet" in built["markdown"]
    assert "no recorded usage" in built["markdown"]
    assert built["methods"] == ""


# ------------------------------------------------------------- modeling report
def modeling_run(session, workspace):
    (workspace / "outputs").mkdir(parents=True)
    model = workspace / "outputs" / "refined.cif"
    model.write_text("data_refined\n")
    (workspace / "outputs" / "final_model.cif").write_text("data_refined\n")
    (workspace / "outputs" / "older.cif").write_text("data_older\n")
    add = lambda kind, **payload: store.append_modeling_event(session["id"], kind, payload)  # noqa: E731
    add("user", text="Build the model")
    add("tool_call", name="refine_phenix", call_id="1", arguments={"model_path": "older.cif", "resolution": 3.05, "macro_cycles": 5, "map_path": "map.mrc"})
    add("tool_output", name="refine_phenix", call_id="1", text=json.dumps({"refined_model": str(model), "command": "/apps/phenix/phenix-2.2-6143/bin/phenix.real_space_refine x"}), images=[])
    add("tool_call", name="validate_model_phenix", call_id="2", arguments={"model_path": str(workspace / "outputs" / "older.cif")})
    add("tool_output", name="validate_model_phenix", call_id="2", text=json.dumps({"model_path": str(workspace / "outputs" / "older.cif"), "CC_mask": 0.5, "molprobity_score": 3.0}), images=[])
    add("tool_call", name="validate_model_phenix", call_id="3", arguments={"model_path": str(model)})
    add("tool_output", name="validate_model_phenix", call_id="3", text=json.dumps({"model_path": str(model), "resolution": 3.05, "CC_mask": 0.7728, "molprobity_score": 1.57, "clashscore": 4.14, "ramachandran_favored_pct": 96.34, "ramachandran_outliers_pct": 0.0, "rotamer_outliers_pct": 1.46}), images=[])
    add("tool_call", name="run_daq", call_id="4", arguments={"model_path": str(model)})
    add("tool_output", name="run_daq", call_id="4", text=json.dumps({"ok": True, "candidates": [{"summary": {"aa": {"count": 1456, "finite": 1456, "mean": 0.7215}}}]}), images=["data:image/png;base64,AAAA"])
    add("answer", text="I chose `outputs/final_model.cif`, the complete tetramer.")


def test_the_modeling_report_ties_numbers_to_the_final_model(project, client, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "MODELING_SANDBOX_ROOT", tmp_path)
    workspace = tmp_path / "workspaces" / "w"
    session = modeling_session(project, workspace="workspaces/w", reasoning_effort="high")
    modeling_run(session, workspace)
    state.on_driver_event(session["id"], usage_event(output=100_000))
    built = client.get(f"/api/sessions/{session['id']}/report").json()
    markdown = built["markdown"]
    assert built["final"]["path"].endswith("outputs/final_model.cif")
    assert "the agent's last message names it" in built["final"]["reason"]
    # final_model.cif is a copy of refined.cif, so that validation counts; the older model's does not.
    assert "| CC_mask | 0.7728 |" in markdown
    assert "| CC_mask | 0.5 |" not in markdown
    assert "not the same file" not in markdown
    assert "| Mean DAQ(AA) | 0.722 |" in markdown
    assert "| Phenix | 2.2-6143 |" in markdown
    assert "macro_cycles=5" in markdown
    methods = built["methods"]
    assert "one round of real-space refinement in Phenix" in methods
    assert "a CC_mask of 0.773, a MolProbity score of 1.57" in methods
    assert "mean DAQ(AA) score was 0.72 over 1456 residues" in methods
    assert "claude-sonnet-5-5 at high reasoning effort" in methods
    assert "Estimated total: **$1.00**" in markdown


def test_numbers_for_another_file_are_called_out(project, client, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "MODELING_SANDBOX_ROOT", tmp_path)
    workspace = tmp_path / "workspaces" / "w"
    session = modeling_session(project, workspace="workspaces/w")
    modeling_run(session, workspace)
    (workspace / "outputs" / "final_model.cif").write_text("data_edited_afterwards\n")
    markdown = client.get(f"/api/sessions/{session['id']}/report").json()["markdown"]
    assert "not the same file as final_model.cif" in markdown


# ----------------------------------------------------------------- transcript
def test_the_modeling_transcript_is_whole_without_images(project, client, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "MODELING_SANDBOX_ROOT", tmp_path)
    session = modeling_session(project, workspace="workspaces/w")
    modeling_run(session, tmp_path / "workspaces" / "w")
    long_output = "x" * 50_000 + " ```inner``` "
    store.append_modeling_event(session["id"], "tool_output", {"name": "bash", "call_id": "9", "text": long_output, "images": []})
    response = client.get(f"/api/sessions/{session['id']}/transcript.md")
    assert response.status_code == 200
    assert 'attachment; filename="build-aldolase-transcript.md"' == response.headers["content-disposition"]
    text = response.text
    assert "### You" in text and "Build the model" in text and "final_model.cif" in text
    assert "x" * 50_000 in text  # nothing clipped
    assert "`` " not in text or "````" in text  # a fence longer than the backticks inside
    assert "base64" not in text
    assert "[1 image left out]" in text
    records = [json.loads(line) for line in client.get(f"/api/sessions/{session['id']}/transcript.jsonl").text.splitlines()]
    assert records[0]["kind"] == "user"
    assert "[image left out]" in json.dumps(records)
    assert "base64" not in json.dumps(records)


def test_the_reconstruction_transcript_has_code_results_and_sub_agents(project, client, monkeypatch):
    items = [
        ("bootstrap", {"role": "developer", "content": "Long instructions the same for every session"}, "2026-10-14T10:00:00+00:00"),
        ("bootstrap", {"role": "user", "content": "Process this dataset"}, "2026-10-14T10:00:01+00:00"),
        ("model", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan the import"}], "encrypted_content": "SECRETBLOB"}, "2026-10-14T10:00:02+00:00"),
        ("model", {"type": "function_call", "name": "python", "call_id": "c1", "arguments": json.dumps({"code": "print('hello')"})}, "2026-10-14T10:00:03+00:00"),
        ("tool", {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"ok": True, "stdout": "hello\n", "stderr": "", "traceback": ""})}, "2026-10-14T10:00:04+00:00"),
        ("tool", {"role": "user", "content": [{"type": "input_text", "text": "An image"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}, "2026-10-14T10:00:05+00:00"),
        ("model", {"role": "assistant", "content": [{"type": "output_text", "text": "Finished the import."}]}, "2026-10-14T10:00:06+00:00"),
    ]
    monkeypatch.setattr(report.trace_reader, "raw_items", lambda _id: iter(items))
    monkeypatch.setattr(report.trace_reader, "recursive_calls", lambda _id: [{"id": 7, "status": "completed", "started_at": "2026-10-14T10:00:03+00:00"}])
    monkeypatch.setattr(report.trace_reader, "raw_recursive_items", lambda _id, _call: iter([("model", {"role": "assistant", "content": "Sub-agent answer"}, "2026-10-14T10:00:04+00:00")]))
    session = store.create_session(project_id=project["id"], kind="reconstruction", title="Env", harness_session_id="h9", config={})
    text = client.get(f"/api/sessions/{session['id']}/transcript.md").text
    assert "Long instructions" not in text and "[left out; they are the same" in text
    assert "### Task" in text and "Process this dataset" in text
    assert "Plan the import" in text and "SECRETBLOB" not in text
    assert "print('hello')" in text and "stdout:" in text and "hello" in text
    assert "[1 image left out]" in text and "base64" not in text
    assert "Finished the import." in text
    assert "## Sub-agent 7: completed" in text and "Sub-agent answer" in text
    records = client.get(f"/api/sessions/{session['id']}/transcript.jsonl").text
    assert "base64" not in records and '"sub_agent": 7' in records
