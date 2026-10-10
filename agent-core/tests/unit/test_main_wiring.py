import pytest

from settleagent.config import Settings
from settleagent.domain.reason_codes import ToolName
from settleagent.main import build_agent, build_app, build_gateway


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in {
        "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
        "API_KEY": "0123456789abcdef",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_gateway_has_an_endpoint_for_every_tool_server(settings: Settings) -> None:
    gw = build_gateway(settings)
    assert set(gw._endpoints) == set(ToolName)  # noqa: SLF001


def test_agent_is_built_from_settings(settings: Settings) -> None:
    assert build_agent(settings) is not None


async def test_app_builds_without_connecting(settings: Settings) -> None:
    app = build_app(settings)
    assert app.consumer.inflight == 0
    await app.engine.dispose()
