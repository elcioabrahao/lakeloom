"""OpenMetadata FQN quoting (FR-SRC-07)."""

from lakeloom.om.fqn import build_fqn


def test_fr_src_07_plain_parts() -> None:
    assert build_fqn("svc", "retail", "sales", "orders") == "svc.retail.sales.orders"


def test_fr_src_07_dotted_part_is_quoted() -> None:
    assert build_fqn("svc", "a.b", "t") == 'svc."a.b".t'


def test_fr_src_07_quote_is_doubled() -> None:
    assert build_fqn("svc", 'we"ird') == 'svc."we""ird"'
