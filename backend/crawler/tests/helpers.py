from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(domain: str, name: str) -> str:
    return (FIXTURES / domain / name).read_text()
