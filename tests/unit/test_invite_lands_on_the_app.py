"""The invite redirect names the APP host, in both places that configure it.

`_invite_redirect_url` is already unit-tested — but those tests hand the
function a value, so they pass whatever the shipped config says. The thing that
actually breaks a real invite is the CONFIG drifting, and the 2026-09-15 domain
swap is exactly the shape of change that does it: `occupella.com` stopped being
the app and became the marketing site, so the string that was right became the
one that sends new teammates to a page with no auth client on it.

⚠ **TWO FILES SET THIS AND EITHER CAN BE EDITED ALONE.** `config/production.yaml`
ships in the image; `render.yaml` sets the same value as an env var, which
outranks the YAML (gotcha 6). Whether the Render Blueprint is even linked is
open (gotcha 80) — so the honest defence is that BOTH say the same thing, and
then it does not matter which one the deployed process reads.

⚠ **AND THE FAILURE IS SILENT AT EVERY LAYER.** GoTrue does not reject a
`redirect_to` it dislikes; it falls back to the project's Site URL. The email
sends, the link works, and the invitee lands somewhere that cannot sign them in.
`helixis-onboarding/src/lib/authRescue.ts` is the fallback for that on the
marketing side; this is the guard on ours.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The host the web app is served from. Not a guess: `frontend/index.html`
# declares `<link rel="canonical" href="https://app.occupella.com/">` and the
# auth email templates link their legal pages there.
APP_HOST = "https://app.occupella.com"

# The marketing site. An invite pointed here is the exact defect this file
# exists to catch — it renders a landing page and ignores the fragment.
MARKETING_HOSTS = (
    "https://occupella.com",
    "https://www.occupella.com",
    "https://setup.occupella.com",
)


def _production_yaml() -> dict:
    return yaml.safe_load((ROOT / "config" / "production.yaml").read_text(encoding="utf-8"))


def _render_env() -> dict[str, str]:
    """Every `key: / value:` pair in render.yaml, flattened.

    Read as YAML rather than grepped so a re-indent or a reordered service
    cannot quietly change what this test is looking at.
    """
    doc = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for service in doc.get("services", []):
        for entry in service.get("envVars", []) or []:
            key, value = entry.get("key"), entry.get("value")
            if key and isinstance(value, str):
                out[key] = value
    return out


def test_production_yaml_points_invites_at_the_app():
    api = _production_yaml().get("api", {})
    assert api.get("public_app_url", "").rstrip("/") == APP_HOST, (
        "config/production.yaml no longer sends invites to the app. An invitee "
        "landing anywhere else cannot set a password."
    )


def test_render_yaml_agrees_with_it():
    """The env var outranks the YAML, so the two disagreeing is the live value.

    Whichever one the deployed process reads, they have to say the same thing —
    that is what makes the unanswered Blueprint-linkage question (gotcha 80)
    stop mattering for this one setting.
    """
    env = _render_env().get("HELIXIS_API__PUBLIC_APP_URL", "")
    assert env.rstrip("/") == APP_HOST, (
        f"render.yaml sets HELIXIS_API__PUBLIC_APP_URL to {env!r}, not {APP_HOST!r}. "
        "An env var beats the YAML, so this is the value that ships."
    )


@pytest.mark.parametrize("host", MARKETING_HOSTS)
def test_the_invite_target_is_never_the_marketing_site(host):
    """The specific wrong answer, named.

    ⚠ Asserting only "it equals the app host" would pass a future rename to a
    fourth host that is also wrong. Naming the marketing hosts states WHY the
    obvious-looking value is the defect: since the swap they serve a page with
    no auth client, so the fragment GoTrue appends is simply ignored.
    """
    api = _production_yaml().get("api", {})
    env = _render_env().get("HELIXIS_API__PUBLIC_APP_URL", "")
    for source, value in (("production.yaml", api.get("public_app_url", "")), ("render.yaml", env)):
        assert value.rstrip("/") != host, (
            f"{source} sends invites to {host}, which is the marketing site. "
            "It has no Supabase client, so the invite fragment is dropped and "
            "the invitee sees the landing page."
        )


def test_the_app_host_is_an_allowed_origin_in_both_places():
    """An invite that lands on a host CORS refuses cannot call the API either.

    Two separate settings have to name it, and they are edited independently.
    """
    yaml_origins = [
        str(o).rstrip("/") for o in _production_yaml().get("api", {}).get("cors_origins", [])
    ]
    assert APP_HOST in yaml_origins, f"{APP_HOST} missing from production.yaml cors_origins"

    raw = _render_env().get("HELIXIS_API__CORS_ORIGINS", "[]")
    render_origins = [str(o).rstrip("/") for o in json.loads(raw)]
    assert APP_HOST in render_origins, f"{APP_HOST} missing from render.yaml CORS_ORIGINS"


def test_the_auth_email_templates_link_the_app_host():
    """The legal links in the invite email have to resolve.

    ⚠ They are `/legal/*.html` STATIC files, which ship with the app and do NOT
    exist on the marketing site — it serves /terms and /privacy as React routes
    instead. So a template pointing at the marketing host 404s, in an email a
    new teammate reads before they trust the product.
    """
    templates = sorted((ROOT / "docs" / "deployment" / "auth-emails").rglob("*.html"))
    assert templates, "no auth email templates found — has the directory moved?"

    for path in templates:
        for url in re.findall(
            r"https://[a-z.]*occupella\.com/legal/[^\"'\s<)]*", path.read_text(encoding="utf-8")
        ):
            assert url.startswith(APP_HOST + "/legal/"), (
                f"{path.name} links {url}, but /legal/*.html only ships on {APP_HOST}."
            )
