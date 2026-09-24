from urllib.parse import parse_qs, urlparse

from core.speech import XfyunASRConfig, XfyunSpeechManager, XfyunStreamingSession


def configured():
    return XfyunASRConfig(
        app_id="demo-app",
        api_key="demo-key",
        api_secret="demo-secret",
    )


def test_auth_url_uses_expected_secure_iat_endpoint_and_signed_fields():
    session = XfyunStreamingSession(configured())
    parsed = urlparse(session._auth_url())
    query = parse_qs(parsed.query)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "iat-api.xfyun.cn"
    assert parsed.path == "/v2/iat"
    assert set(query) == {"authorization", "date", "host"}
    assert query["host"] == ["iat-api.xfyun.cn"]
    assert "demo-secret" not in session._auth_url()


def test_dynamic_correction_results_replace_prior_fragments():
    session = XfyunStreamingSession(configured())
    session._apply_result({"sn": 1, "ws": [{"cw": [{"w": "平顶山"}]}]})
    session._apply_result({"sn": 2, "ws": [{"cw": [{"w": "港"}]}]})
    assert session.text == "平顶山港"
    session._apply_result(
        {
            "sn": 3,
            "pgs": "rpl",
            "rg": [1, 2],
            "ws": [{"cw": [{"w": "平顶山港"}]}],
        }
    )
    assert session.text == "平顶山港"


def test_manager_opens_streams_and_finishes_bounded_session():
    created = []

    class FakeSession:
        text = ""

        def __init__(self, config):
            self.config = config
            self.opened = False
            self.closed = False
            created.append(self)

        def open(self):
            self.opened = True

        def send_audio(self, pcm):
            self.text += pcm.decode("utf-8")
            return self.text

        def finish(self):
            self.closed = True
            return self.text

        def close(self):
            self.closed = True

    manager = XfyunSpeechManager(configured(), session_factory=FakeSession)
    session_id = manager.start()
    assert len(session_id) == 32
    assert created[0].opened
    assert manager.chunk(session_id, "航次".encode()) == "航次"
    assert manager.finish(session_id) == "航次"
    assert created[0].closed
