"""No settings object prints a credential.

A settings object ends up in log lines, exception messages and Sentry stack
frames (Sentry records a failing frame's locals by ``repr``). Every credential
field in ``settings.py`` was a plain ``str``, so all three printed it: the
Buildium client secret, the Fernet data key, the Stripe and Twilio secrets,
the database URL with its password. Rentvine's pair was found first, by that
connector's merge-gate test; these are the rest. They are all
``Field(repr=False)`` now, and this file pins it three ways:

* every credential field, found by walking ``Settings`` rather than listed by
  hand, gets a sentinel, and no sentinel appears in ``repr``, ``str`` or an
  f-string of its model or of the ``Settings`` around it, while the attribute
  still returns it;
* the plaintext credential bodies the connect routes accept (a handler local,
  so a Sentry frame variable) do the same;
* an AST walk over ``src/helixis`` fails on any pydantic model or dataclass
  that declares a credential-named ``str`` field without ``repr=False``, so a
  new key added next month is caught the day it lands.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from helixis.api.routes.buildium import BuildiumCredentialsInput, SetWebhookSecretRequest
from helixis.api.routes.rentvine import RentvineCredentialsInput
from helixis.settings import Settings

REPO = Path(__file__).resolve().parents[2]

#: A field NAME that says it holds a credential. ``client_id`` is Buildium's
#: header half; it is stored encrypted, like the secret it travels with.
_CREDENTIAL_NAME = re.compile(
    r"secret|password|token|client_id|(^|_)(api|anon|service|data|public|private)_key$"
    r"|^dsn$|^db_url$"
)


def _is_credential(name: str) -> bool:
    # ``*_last4`` is the masked display value the status routes return.
    return bool(_CREDENTIAL_NAME.search(name)) and not name.endswith("_last4")


#: The fields this defect was reported on, so the walk below cannot quietly
#: shrink (a broken pattern would otherwise pass by finding nothing to check).
_MUST_BE_FOUND = {
    "Settings.data_key",
    "BuildiumSettings.client_id",
    "BuildiumSettings.client_secret",
    "BuildiumSettings.webhook_secret",
    "RentvineSettings.api_key",
    "RentvineSettings.api_secret",
    "TwilioSettings.auth_token",
    "ComposioSettings.api_key",
    "BillingSettings.stripe_secret_key",
    "BillingSettings.stripe_webhook_secret",
    "LLMSettings.anthropic_api_key",
    "LLMSettings.voyage_api_key",
    "SupabaseSettings.service_key",
    "SupabaseSettings.db_url",
    "AuthSettings.jwt_secret",
}


def _credential_fields(
    model: type[BaseModel], path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], type[BaseModel], str]]:
    """``(path to the owning model, the model class, field name)`` for every
    credential-named ``str`` field reachable from ``model``."""
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield from _credential_fields(annotation, (*path, name))
        elif annotation is str and _is_credential(name):
            yield path, model, name


def _found() -> list[tuple[tuple[str, ...], type[BaseModel], str]]:
    return list(_credential_fields(Settings))


def _sentinel(index: int) -> str:
    return f"sentinel{index:03d}x7f3a9c"


def _shown(obj: object) -> list[str]:
    return [repr(obj), str(obj), f"{obj}", repr({"s": obj}), repr([obj])]


def _leaks(obj: object, sentinels: list[str]) -> list[str]:
    return sorted({s for text in _shown(obj) for s in sentinels if s in text})


def _owner(settings: Settings, path: tuple[str, ...]) -> Any:
    obj: Any = settings
    for part in path:
        obj = getattr(obj, part)
    return obj


# ── behavioural: the settings tree ──────────────────────────────────────────


def test_the_walk_finds_every_credential_the_task_named() -> None:
    found = {f"{model.__name__}.{name}" for _path, model, name in _found()}
    assert _MUST_BE_FOUND <= found, sorted(_MUST_BE_FOUND - found)


def test_every_settings_credential_field_is_hidden_from_repr() -> None:
    printable = [
        f"{model.__name__}.{name}"
        for _path, model, name in _found()
        if model.model_fields[name].repr is not False
    ]
    assert printable == []


def test_a_settings_object_never_prints_a_credential() -> None:
    fields = _found()
    sentinels = [_sentinel(i) for i in range(len(fields))]
    kwargs: dict[str, Any] = {}
    for (path, _model, name), value in zip(fields, sentinels, strict=True):
        node = kwargs
        for part in path:
            node = node.setdefault(part, {})
        node[name] = value
    settings = Settings(**kwargs)

    assert _leaks(settings, sentinels) == []
    for path in {path for path, _model, _name in fields}:
        owner = _owner(settings, path)
        assert _leaks(owner, sentinels) == [], type(owner).__name__

    # ... and the values themselves are untouched, for readers and for dumps.
    for (path, _model, name), value in zip(fields, sentinels, strict=True):
        assert getattr(_owner(settings, path), name) == value
    dumped = repr(settings.model_dump())
    assert all(s in dumped for s in sentinels)


def test_a_settings_copy_stays_hidden() -> None:
    """``credentials.decrypt_buildium_settings_from`` builds each company's
    ``BuildiumSettings`` with ``model_copy(update=...)``, so the per-company
    decrypted secret lives in a copy, not in a freshly validated model."""
    base = Settings().buildium
    company = base.model_copy(update={"client_id": _sentinel(1), "client_secret": _sentinel(2)})
    assert _leaks(company, [_sentinel(1), _sentinel(2)]) == []
    assert company.client_secret == _sentinel(2)


# ── behavioural: the credential bodies the connect routes accept ────────────


def test_the_connect_route_bodies_never_print_a_credential() -> None:
    bodies = [
        BuildiumCredentialsInput(client_id=_sentinel(1), client_secret=_sentinel(2)),
        SetWebhookSecretRequest(secret=_sentinel(3)),
        RentvineCredentialsInput(api_key=_sentinel(4), api_secret=_sentinel(5)),
    ]
    sentinels = [_sentinel(i) for i in range(1, 6)]
    for body in bodies:
        assert _leaks(body, sentinels) == [], type(body).__name__
    assert bodies[0].client_secret == _sentinel(2)
    assert bodies[1].secret == _sentinel(3)
    assert bodies[2].api_secret == _sentinel(5)


# ── structural: no model in the package declares a printable credential ────

_MODEL_BASES = {"BaseModel", "BaseSettings"}


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_model(cls: ast.ClassDef) -> bool:
    if any(_base_name(b) in _MODEL_BASES for b in cls.bases):
        return True
    for deco in cls.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if _base_name(target) == "dataclass":
            return True
    return False


def _is_str(annotation: ast.expr) -> bool:
    """``str``, ``str | None`` or ``Optional[str]``."""
    if isinstance(annotation, ast.Name):
        return annotation.id == "str"
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return _is_str(ast.parse(annotation.value, mode="eval").body)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_str(annotation.left) or _is_str(annotation.right)
    if isinstance(annotation, ast.Subscript) and _base_name(annotation.value) == "Optional":
        return _is_str(annotation.slice)
    return False


def _hidden(value: ast.expr | None) -> bool:
    """The default is ``Field(..., repr=False)`` or ``field(..., repr=False)``."""
    if not isinstance(value, ast.Call) or _base_name(value.func) not in {"Field", "field"}:
        return False
    return any(
        k.arg == "repr" and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in value.keywords
    )


def _offenders(paths: list[Path], root: Path) -> list[str]:
    out = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef) or not _is_model(cls):
                continue
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and _is_credential(stmt.target.id)
                    and _is_str(stmt.annotation)
                    and not _hidden(stmt.value)
                ):
                    where = f"{path.relative_to(root)}:{stmt.lineno}"
                    out.append(f"{where} {cls.name}.{stmt.target.id}")
    return out


def test_no_model_in_the_package_declares_a_printable_credential() -> None:
    sources = sorted((REPO / "src/helixis").rglob("*.py"))
    assert len(sources) > 100  # the walk really covers the package
    assert _offenders(sources, REPO) == []


def test_the_structural_check_finds_a_planted_offender(tmp_path: Path) -> None:
    """The walk must be able to fail: a guard that finds nothing proves nothing."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from dataclasses import dataclass, field\n"
        "from typing import Optional\n"
        "from pydantic import BaseModel, Field\n"
        "import pydantic_settings\n"
        "class A(BaseModel):\n"
        "    api_key: str = ''\n"
        "    webhook_secret: str | None = Field(default=None)\n"
        "    client_secret: str = Field(default='', repr=False)\n"
        "    webhook_secret_last4: str | None = None\n"
        "    daily_token_budget: int = 0\n"
        "    base_url: str = ''\n"
        "@dataclass(frozen=True)\n"
        "class B:\n"
        "    password: Optional[str] = None\n"
        "    auth_token: str = field(default='', repr=False)\n"
        "class C(pydantic_settings.BaseSettings):\n"
        "    data_key: 'str' = ''\n"
        "class NotAModel:\n"
        "    api_key: str = ''\n",
        encoding="utf-8",
    )
    assert _offenders([planted], tmp_path) == [
        "planted.py:6 A.api_key",
        "planted.py:7 A.webhook_secret",
        "planted.py:14 B.password",
        "planted.py:17 C.data_key",
    ]
