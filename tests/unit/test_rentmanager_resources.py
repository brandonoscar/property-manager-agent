"""Every Rent Manager read asks for the fields it maps, with their embeds.

Rent Manager returns an embedded field only when the embed is ALSO named in
``fields`` (Overview p.6), and ``Balance``, ``OpenBalance`` and
``SecurityDepositHeld`` come back empty without their embed: a reader would
take the empty value as $0 (rule 14). The lease term is the same trap: the
help scrape marks ``StartDate``/``EndDate`` "Requires Embed (LeaseRenewals)".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.rentmanager import mapper
from helixis.rentmanager.resources import LEASES, RESOURCES, TENANTS, UNITS, Resource

MAPPER = Path(mapper.__file__)


@pytest.mark.parametrize("name", list(RESOURCES))
def test_every_embed_is_also_in_fields(name: str) -> None:
    resource = RESOURCES[name]
    assert set(resource.embeds) <= set(resource.fields), name


@pytest.mark.parametrize("name", list(RESOURCES))
def test_every_field_that_needs_an_embed_has_it(name: str) -> None:
    resource = RESOURCES[name]
    for field_name, embed in resource.needs_embed.items():
        assert field_name in resource.fields and embed in resource.embeds, (name, field_name)


def test_the_fields_that_read_as_zero_without_their_embed_are_declared_so() -> None:
    """The design's quirk row, and the lease term the help scrape adds."""
    assert TENANTS.needs_embed == {
        "Balance": "Balance",
        "SecurityDepositHeld": "SecurityDepositHeld",
    }
    assert LEASES.needs_embed == {"StartDate": "LeaseRenewals", "EndDate": "LeaseRenewals"}
    assert UNITS.needs_embed == {"CurrentMarketRent": "CurrentMarketRent"}


@pytest.mark.parametrize(
    ("fields", "embeds", "needs", "why"),
    [
        (("ID", "Balance"), ("Balance", "Contacts"), {}, "an embed missing from fields"),
        (("ID", "Balance"), (), {"Balance": "Balance"}, "a field read without its embed"),
        (("ID",), (), {"Balance": "Balance"}, "a needed field not asked for"),
        (("Name",), (), {}, "the key not asked for"),
        (("ID", "ID"), (), {}, "a field named twice"),
    ],
)
def test_a_declaration_that_breaks_the_rule_cannot_be_built(
    fields: tuple[str, ...], embeds: tuple[str, ...], needs: dict[str, str], why: str
) -> None:
    with pytest.raises(ValueError):
        Resource(path="/X", key="ID", fields=fields, embeds=embeds, needs_embed=needs)


def test_the_query_names_the_fields_and_the_embeds() -> None:
    assert TENANTS.query() == {
        "fields": ",".join(TENANTS.fields),
        "embeds": ",".join(TENANTS.embeds),
    }
    no_embeds = Resource(path="/X", key="ID", fields=("ID", "Name"))
    assert no_embeds.query() == {"fields": "ID,Name"}


def _string_keys_read_by(function: ast.FunctionDef) -> set[str]:
    """``obj.get("X")`` / ``obj["X"]`` keys a mapper function reads off ``obj``."""
    keys: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "obj"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


#: Each mapper function and the read whose records it maps.
_MAPPED_BY = {
    "map_property": "properties",
    "map_unit": "units",
    "map_tenant": "tenants",
    "map_owner": "owners",
    "map_vendor": "vendors",
    "map_gl_account": "gl_accounts",
    "map_bill": "bills",
    "bill_is_reversed": "bills",
    "map_work_order": "work_orders",
    "map_lease": "leases",
    "lease_status": "leases",
    "lease_unit_id": "leases",
    "lease_move_out": "leases",
    "lease_move_in": "leases",
    "lease_tenant_id": "leases",
    "tenant_balance": "tenants",
    "tenant_deposit": "tenants",
    "aging": "tenants",
}


def test_every_field_a_mapper_reads_is_one_the_read_asks_for() -> None:
    """Both sides (rule 15): a field the mapper reads and the query never asks
    for would arrive as nothing on every row, and map to "unknown" forever."""
    tree = ast.parse(MAPPER.read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert set(_MAPPED_BY) <= set(functions), "a mapper was renamed; re-point this test"
    checked = 0
    for name, read in _MAPPED_BY.items():
        asked = set(RESOURCES[read].fields)
        for key in _string_keys_read_by(functions[name]):
            checked += 1
            assert key in asked, f"{name} reads {key!r}, which {read} never asks for"
    assert checked > 30, "the walk found almost nothing; it is pointed at nothing"
