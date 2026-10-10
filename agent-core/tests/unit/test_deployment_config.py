"""Deployment files cannot be test-deployed from CI, so check what can go wrong statically:
every referenced file exists, every env var is one the code actually reads, every service
reference resolves. A rename in code that forgets the deployment config fails here."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from settleagent.config import Settings

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
RENDER = yaml.safe_load((ROOT / "render.yaml").read_text())
SETTINGS_ENV = {name.upper() for name in Settings.model_fields}
# variables read by something other than Settings
OTHER_READERS = {
    "ANTHROPIC_BASE_URL",  # the Anthropic SDK
    "PORT",  # Streamlit / uvicorn command lines
    "SETTLE_API_URL",
    "SETTLE_API_KEY",
    "TRACE_URL_TEMPLATE",  # dashboard app.py
    "SETTLE_MCP_PORT",
    "SETTLE_MCP_HOST",
    "SETTLE_MCP_TRANSPORT",
    "SETTLE_DATA_DIR",  # MCP servers
    "PRODUCER_MODE",
    "PRODUCER_LIMIT",
    "PRODUCER_RATE",
    "PRODUCER_FIXTURES",
    "PRODUCER_SEED",
    "PRODUCER_OFFSET",
    "PRODUCER_EXIT_AFTER_REPLAY",
    "KAFKA_TOPIC",  # Java producer
}


def dockerfile_copy_sources(dockerfile: Path) -> list[str]:
    """Source paths of every `COPY` (not `COPY --from`) in a Dockerfile."""
    sources: list[str] = []
    for line in dockerfile.read_text().splitlines():
        m = re.match(r"\s*COPY\s+(?!--from)(?:--\S+\s+)*(.+)", line)
        if m:
            *srcs, _dest = m.group(1).split()
            sources += srcs
    return sources


DOCKERFILES = sorted(ROOT.glob("*/Dockerfile"))


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_every_dockerfile_copies_only_paths_that_exist(dockerfile: Path) -> None:
    """Build context is the repo root; a COPY of a missing path would only fail on a real build."""
    sources = dockerfile_copy_sources(dockerfile)
    assert sources, f"{dockerfile} copies nothing?"
    for src in sources:
        assert (ROOT / src).exists(), (
            f"{dockerfile.parent.name}/Dockerfile: COPY {src} does not exist"
        )


def test_every_service_has_a_dockerfile() -> None:
    assert {p.parent.name for p in DOCKERFILES} == {
        "agent-core",
        "mcp-servers",
        "dashboard",
        "producer-service",
    }


def test_dockerignore_does_not_exclude_files_the_images_need() -> None:
    ignored = (ROOT / ".dockerignore").read_text().split()
    for needed in ("data", "contracts", "agent-core/alembic", "mcp-servers/_shared"):
        assert needed not in ignored
    assert "evals/dataset" in ignored  # large, not needed at runtime
    assert not any(p.startswith("data") for p in ignored)


# ----------------------------------------------------------------------------- compose
def app_services() -> dict[str, Any]:
    """Our own services (profile `app`); kafka/postgres/jaeger configure third-party images."""
    return {n: v for n, v in COMPOSE["services"].items() if "app" in v.get("profiles", [])}


def compose_envs() -> list[tuple[str, str]]:
    return [
        (name, k) for name, svc in app_services().items() for k in (svc.get("environment") or {})
    ]


@pytest.mark.parametrize(("service", "key"), compose_envs())
def test_compose_env_vars_are_ones_the_code_reads(service: str, key: str) -> None:
    assert key in SETTINGS_ENV | OTHER_READERS, f"{service}: nothing reads {key}"


def test_compose_dependencies_and_images_resolve() -> None:
    names = set(COMPOSE["services"])
    for name, svc in COMPOSE["services"].items():
        deps = svc.get("depends_on") or {}
        assert set(deps) <= names, (name, set(deps) - names)
        if "build" in svc:
            assert (ROOT / svc["build"]["dockerfile"]).exists(), name


def test_the_app_profile_wires_the_agent_to_every_mcp_server() -> None:
    env = COMPOSE["services"]["agent"]["environment"]
    for key, svc in (
        ("MCP_SECURITY_MASTER_URL", "mcp-security-master"),
        ("MCP_SSI_STORE_URL", "mcp-ssi-store"),
        ("MCP_HOLIDAY_CALENDAR_URL", "mcp-holiday-calendar"),
        ("MCP_TRADE_STORE_URL", "mcp-trade-store"),
    ):
        host, port = env[key].split(":")
        assert host == svc and svc in COMPOSE["services"]
        assert COMPOSE["services"][svc]["environment"]["SETTLE_MCP_PORT"] == port


def test_inside_the_network_the_kafka_listener_is_not_localhost() -> None:
    for svc in ("agent", "api", "producer"):
        assert COMPOSE["services"][svc]["environment"]["KAFKA_BOOTSTRAP_SERVERS"] == "kafka:29092"


# ------------------------------------------------------------------------------ render
SERVICES = {s["name"]: s for s in RENDER["services"]}


def render_envs() -> list[tuple[str, dict[str, Any]]]:
    return [(n, v) for n, s in SERVICES.items() for v in s.get("envVars", [])]


@pytest.mark.parametrize(
    ("service", "var"), render_envs(), ids=lambda x: x if isinstance(x, str) else x["key"]
)
def test_render_env_vars_are_ones_the_code_reads(service: str, var: dict[str, Any]) -> None:
    spring = var["key"].startswith("SPRING_KAFKA_PROPERTIES_")  # Spring Boot relaxed binding
    assert var["key"] in SETTINGS_ENV | OTHER_READERS or spring, f"{service}: unknown {var['key']}"


def test_render_services_point_at_real_dockerfiles_and_commands() -> None:
    for name, svc in SERVICES.items():
        assert (ROOT / svc["dockerfilePath"]).exists(), name
        assert svc["dockerContext"] == "."
        assert svc["type"] in {"web", "worker", "pserv"}


def test_render_references_resolve() -> None:
    dbs = {d["name"] for d in RENDER["databases"]}
    for name, var in render_envs():
        if "fromDatabase" in var:
            assert var["fromDatabase"]["name"] in dbs
        if "fromService" in var:
            ref = var["fromService"]
            target = SERVICES[ref["name"]]
            assert target["type"] == ref["type"], (name, ref)
            if "envVarKey" in ref:
                assert ref["envVarKey"] in {v["key"] for v in target["envVars"]}


def test_render_private_services_expose_one_port_and_the_agent_reaches_all_four() -> None:
    mcp = {n for n, s in SERVICES.items() if s["type"] == "pserv"}
    assert mcp == {
        "mcp-security-master",
        "mcp-ssi-store",
        "mcp-holiday-calendar",
        "mcp-trade-store",
    }
    for n in mcp:
        ports = [v["value"] for v in SERVICES[n]["envVars"] if v["key"] == "SETTLE_MCP_PORT"]
        assert ports == ["10000"]
    wired = {
        v["fromService"]["name"]
        for v in SERVICES["settleagent-agent"]["envVars"]
        if v["key"].startswith("MCP_")
    }
    assert wired == mcp


def test_render_secrets_are_never_committed() -> None:
    for name, var in render_envs():
        if re.search(r"KEY|PASSWORD|SECRET|TOKEN|JAAS", var["key"]):
            assert "value" not in var, f"{name}.{var['key']} has a literal value"


def test_render_starts_the_api_with_migrations_and_the_cost_ceiling_set() -> None:
    assert "alembic upgrade head" in SERVICES["settleagent-api"]["dockerCommand"]
    for n in ("settleagent-api", "settleagent-agent"):
        ceiling = next(v for v in SERVICES[n]["envVars"] if v["key"] == "COST_CEILING_USD")
        assert 0 < float(ceiling["value"]) <= 25, (
            "hosted default must stay low until evals set a policy"
        )


# ----------------------------------------------------------------------------- .env.example
def test_env_example_documents_every_setting() -> None:
    """CLAUDE.md: `.env.example` documents every variable. A new Settings field without an entry
    (commented-out is fine) would leave operators guessing."""
    text = (ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))
    missing = SETTINGS_ENV - documented
    assert not missing, f"undocumented in .env.example: {sorted(missing)}"


def test_env_example_has_no_real_secrets() -> None:
    for line in (ROOT / ".env.example").read_text().splitlines():
        if re.match(r"(ANTHROPIC_API_KEY|KAFKA_SASL_PASSWORD)=.+", line):
            pytest.fail(f"a secret value is committed in .env.example: {line.split('=')[0]}")
