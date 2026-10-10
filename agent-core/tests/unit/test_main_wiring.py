import pytest

from settleagent.config import Settings
from settleagent.domain.reason_codes import ToolName
from settleagent.main import build_agent, build_app, build_gateway
from settleagent.observability.telemetry import NOOP


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
    assert build_agent(settings, NOOP) is not None


async def test_app_builds_without_connecting(settings: Settings) -> None:
    app = build_app(settings, NOOP)
    assert app.consumer.inflight == 0
    await app.engine.dispose()


# ---------------------------------------------------------- hosted-deployment settings
from settleagent.config import normalize_database_url, normalize_mcp_url  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),  # already right
    ],
)
def test_database_urls_from_hosting_platforms_get_the_right_driver(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("trade-store:10000", "http://trade-store:10000/mcp"),  # private-service discovery
        ("http://localhost:8104/mcp", "http://localhost:8104/mcp"),
        ("http://localhost:8104", "http://localhost:8104/mcp"),
        ("https://mcp.example.com/", "https://mcp.example.com/mcp"),
    ],
)
def test_mcp_urls_accept_a_bare_host_and_port(raw: str, expected: str) -> None:
    assert normalize_mcp_url(raw) == expected


def test_settings_apply_the_normalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
        "DATABASE_URL": "postgres://u:p@h/db",
        "API_KEY": "0123456789abcdef",
        "MCP_TRADE_STORE_URL": "mcp-trade-store:10000",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+psycopg://u:p@h/db"
    assert s.mcp_trade_store_url == "http://mcp-trade-store:10000/mcp"


def test_kafka_client_config_is_plaintext_unless_security_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "KAFKA_BOOTSTRAP_SERVERS": "b:9092",
        "DATABASE_URL": "postgresql://u:p@h/db",
        "API_KEY": "0123456789abcdef",
    }
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    assert Settings(_env_file=None).kafka_client_config() == {"bootstrap.servers": "b:9092"}
    for k, v in {
        "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
        "KAFKA_SASL_MECHANISM": "PLAIN",
        "KAFKA_SASL_USERNAME": "key",
        "KAFKA_SASL_PASSWORD": "secret",
    }.items():
        monkeypatch.setenv(k, v)
    conf = Settings(_env_file=None).kafka_client_config()
    assert conf["security.protocol"] == "SASL_SSL" and conf["sasl.mechanism"] == "PLAIN"
    assert (conf["sasl.username"], conf["sasl.password"]) == ("key", "secret")


def test_the_sasl_password_is_never_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "KAFKA_BOOTSTRAP_SERVERS": "b:9092",
        "DATABASE_URL": "postgresql://u:p@h/db",
        "API_KEY": "0123456789abcdef",
        "KAFKA_SASL_PASSWORD": "hunter2-secret",
    }.items():
        monkeypatch.setenv(k, v)
    assert "hunter2-secret" not in repr(Settings(_env_file=None))
