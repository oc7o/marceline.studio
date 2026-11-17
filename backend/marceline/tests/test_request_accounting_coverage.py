"""Every outgoing request is counted (issue #74): every PoliteClient built outside tests has an
observer, and nothing but the one documented exception talks to httpx directly."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
# httpx used directly, on purpose: DataForSEO is counted by hand with `record_request`; the
# delivery client only talks to our own development/production hosts over Tailscale (ADR 0028),
# which is no site's budget.
# `crawler/llm.py` is the third (ADR 0042, issue #22): the crawler's LLM provider is a paid API
# with a credential, like DataForSEO, and no site's server. Counted by hand as `kind=llm_api`.
# `marceline/uploads/relay.py` is the fourth (issue #59, ADR 0044 §9): the public host forwarding a
# customer's chunk to development's Tailscale-only listener — our own host, no site's server and no
# budget, and the one place that must hand httpx a *body generator* rather than bytes, which is the
# whole point of it (`core/http.py` has no streaming upload and should not grow one for this).
DIRECT_HTTPX_CLIENTS = {
    "crawler/discovery/dataforseo.py",
    "crawler/llm.py",
    "delivery/client.py",
    "marceline/uploads/relay.py",
}


def source_files() -> list[Path]:
    return [
        path
        for path in BACKEND.rglob("*.py")
        if not {".venv", "tests", "migrations"} & set(path.relative_to(BACKEND).parts)
    ]


def calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called == name:
                found.append(node)
    return found


def test_every_polite_client_has_a_request_observer() -> None:
    missing = []
    constructed = 0
    for path in source_files():
        for call in calls(ast.parse(path.read_text()), "PoliteClient"):
            constructed += 1
            if "observer" not in {keyword.arg for keyword in call.keywords}:
                missing.append(f"{path.relative_to(BACKEND)}:{call.lineno}")
    assert constructed >= 6
    assert missing == []


def test_no_other_code_opens_its_own_httpx_client() -> None:
    offenders = []
    for path in source_files():
        relative = str(path.relative_to(BACKEND))
        if relative == "core/http.py" or relative in DIRECT_HTTPX_CLIENTS:
            continue
        tree = ast.parse(path.read_text())
        for name in ("Client", "AsyncClient", "get", "post", "request", "stream"):
            for call in calls(tree, name):
                func = call.func
                if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "httpx":
                    offenders.append(f"{relative}:{call.lineno} httpx.{name}")
    assert offenders == []
