from langchain_core.messages import HumanMessage, SystemMessage

from langmesh.runtime.models.codex import ChatCodexModel, _codex_user_agent


def test_codex_payload_has_chatgpt_lineage_metadata() -> None:
    model = ChatCodexModel(model="gpt-5", session_id="session-123")

    payload = model._build_payload(
        [SystemMessage(content="Be concise."), HumanMessage(content="Hello")],
        stream=True,
    )

    assert payload["store"] is False
    assert payload["client_metadata"] == {
        "session_id": "session-123",
        "thread_id": "session-123",
        "x-codex-window-id": "session-123:0",
    }
    assert payload["tool_choice"] == "auto"
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_codex_websocket_payload_uses_response_create_envelope() -> None:
    payload = ChatCodexModel._websocket_payload({"model": "gpt-5", "stream": True})

    assert payload == {"type": "response.create", "model": "gpt-5", "stream": True}


def test_codex_websocket_url_matches_responses_endpoint() -> None:
    assert ChatCodexModel._websocket_url().startswith("wss://chatgpt.com/")


def test_codex_user_agent_has_codex_cli_identity(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", raising=False)
    assert _codex_user_agent().startswith("codex_cli_rs/0.152.1 (")
