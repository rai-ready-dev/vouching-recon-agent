import sys
from pathlib import Path

import pytest

pytest.importorskip("openai")
sys.path.insert(0, str(Path(__file__).parent))
from azure_stub import AzureStub  # noqa: E402

from vra.llm import FoundryChat, MeteredChat, UsageTracker  # noqa: E402
from vra.web import foundry_settings  # noqa: E402
from vra.web.app import foundry_hint  # noqa: E402

KEY = "K" * 32


@pytest.fixture
def stub():
    s = AzureStub(key=KEY, deployment="gpt-prod")
    yield s
    s.close()


def classic(stub, **kw):
    return FoundryChat(deployment="gpt-prod", endpoint=stub.url, api_key=kw.pop("key", KEY), api_version="2024-10-21", timeout=10, **kw)


def v1(stub, **kw):
    return FoundryChat(deployment="gpt-prod", base_url=stub.url + "/openai/v1/", api_key=kw.pop("key", KEY), timeout=10, **kw)


def test_classic_route_sends_what_azure_expects(stub):
    c = classic(stub)
    assert c.chat("be brief", "ping", json_mode=True) == "OK" and c.pop_usage() == (7, 3) and c.pop_usage() is None
    r = stub.requests[0]
    assert r["path"] == "/openai/deployments/gpt-prod/chat/completions" and r["query"]["api-version"] == ["2024-10-21"]
    assert r["headers"]["api-key"] == KEY and r["body"]["response_format"] == {"type": "json_object"}
    assert r["body"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_v1_route_uses_the_deployment_as_model_and_the_key(stub):
    assert v1(stub).chat("s", "u") == "OK"
    r = stub.requests[0]
    assert r["path"] == "/openai/v1/chat/completions" and r["body"]["model"] == "gpt-prod" and r["headers"]["api-key"] == KEY


def test_images_are_sent_as_data_uris(stub):
    classic(stub).chat("ocr", "transcribe", images=[("image/png", b"\x89PNG\r\n")])
    content = stub.requests[0]["body"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "transcribe"} and content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_wrong_key_wrong_deployment_and_their_plain_language_hints(stub):
    with pytest.raises(Exception) as bad:
        classic(stub, key="X" * 32).chat("s", "u")
    assert bad.value.status_code == 401 and "SAME resource" in foundry_hint(bad.value)
    with pytest.raises(Exception) as missing:
        FoundryChat(deployment="nope", endpoint=stub.url, api_key=KEY, timeout=10).chat("s", "u")
    assert missing.value.status_code == 404 and "deployment NAME" in foundry_hint(missing.value)


def test_json_mode_rejected_by_the_deployment_is_remembered():
    s = AzureStub(key=KEY, reject_json_mode=True)
    try:
        c = classic(s)
        assert c.chat("s", "u", json_mode=True) == "OK"
        assert c.chat("s", "u", json_mode=True) == "OK"
        assert ["response_format" in r["body"] for r in s.requests] == [True, False, False]     # one failed try, then never again
    finally:
        s.close()


def test_throttling_is_retried_by_the_agent_after_the_sdk_gives_up():
    s = AzureStub(key=KEY, throttle=3)                                                         # SDK makes 3 attempts per call
    try:
        t = UsageTracker()
        m = MeteredChat(classic(s), t, "extract", sleep=lambda x: None)
        assert m.chat("s", "u") == "OK"
        sm = t.summary()
        assert sm["retries"] == 1 and sm["errors"] == 0 and sm["tokens_in"] == 7 and sm["tokens_out"] == 3
    finally:
        s.close()


def test_a_foundry_resource_that_only_serves_the_v1_route_is_discovered_by_probing():
    """The reported situation: a valid key, an address that answers 401 on the classic route, success on the v1 route."""
    s = AzureStub(key=KEY, classic=False, v1=True)
    try:
        styles = [{"label": "classic", "endpoint": s.url, "base_url": ""}, {"label": "v1", "endpoint": "", "base_url": s.url + "/openai/v1/"}]
        res = foundry_settings.probe({"endpoint": s.url, "deployment": "gpt-prod", "api_key": KEY}, styles=styles, per_try_seconds=10)
        assert [r["ok"] for r in res] == [False, True] and res[0]["status"] == 401 and res[1]["reply"] == "OK"
        # a wrong key fails every style, and tells us so
        res = foundry_settings.probe({"endpoint": s.url, "deployment": "gpt-prod", "api_key": "X" * 32}, styles=styles, per_try_seconds=10)
        assert not any(r["ok"] for r in res) and len(res) == 2
    finally:
        s.close()


def test_candidate_styles_cover_the_hosts_a_foundry_resource_answers_on():
    st = foundry_settings.candidate_styles("https://myres.services.ai.azure.com", "")
    labels = " | ".join(x["label"] for x in st)
    assert "Classic Azure OpenAI at myres.services.ai.azure.com" in labels and "OpenAI-compatible (v1) at myres.openai.azure.com" in labels
    assert any(x["base_url"] == "https://myres.services.ai.azure.com/openai/v1/" for x in st)
    assert foundry_settings.candidate_styles("", "") == []
