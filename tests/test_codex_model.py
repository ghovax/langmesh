from langchain_core.messages import HumanMessage, SystemMessage

from langmesh.runtime.models.codex import ChatCodexModel


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
    }
    assert payload["tool_choice"] == "auto"
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_codex_websocket_payload_uses_response_create_envelope() -> None:
    payload = ChatCodexModel._websocket_payload({"model": "gpt-5", "stream": True})

    assert payload == {"type": "response.create", "model": "gpt-5", "stream": True}


def test_codex_websocket_url_matches_responses_endpoint() -> None:
    assert ChatCodexModel._websocket_url().startswith("wss://chatgpt.com/")
