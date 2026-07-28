"""Tests for frontend macros (forms, buttons) and base.html structure.

Macros are rendered through a Jinja2 environment pointed at app/templates.
base.html is verified both by direct rendering and via an authenticated TestClient GET.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app.api.dependencies import require_auth
from app.core.security import UserSession
from tests.conftest import MockRedis, make_user_session, serialize_user_session

_env = Environment(loader=FileSystemLoader("app/templates"))
_templates = Jinja2Templates(directory="app/templates")


def _render_macro(template_path: str, macro_name: str, *args, **kwargs) -> str:
    template = _env.get_template(template_path)
    macro = getattr(template.module, macro_name)
    return str(macro(*args, **kwargs))


class TestFormFieldMacro:

    def test_form_field_renders_label_and_input(self) -> None:
        html = _render_macro(
            "macros/forms.html", "form_field", "email", "Email Address", type="email"
        )

        assert '<label for="email">Email Address</label>' in html
        assert '<input type="email" name="email" id="email"' in html

    def test_form_field_required_attribute(self) -> None:
        html = _render_macro("macros/forms.html", "form_field", "name", "Name", required=True)

        assert "required" in html

    def test_form_field_error_state(self) -> None:
        html = _render_macro(
            "macros/forms.html", "form_field", "email", "Email", error="Invalid email"
        )

        assert "<span" in html
        assert "Invalid email" in html
        assert "has-error" in html


class TestBtnMacro:

    def test_btn_primary_variant(self) -> None:
        html = _render_macro("macros/buttons.html", "btn", "Submit", variant="primary")

        assert "<button" in html
        assert "btn-primary" in html

    def test_btn_link_variant(self) -> None:
        html = _render_macro("macros/buttons.html", "btn", "Go", href="/search")

        assert '<a href="/search"' in html
        assert "btn" in html

    def test_btn_htmx_passthrough(self) -> None:
        html = _render_macro(
            "macros/buttons.html", "btn", "Search", hx_post="/search", hx_target="#results"
        )

        assert 'hx-post="/search"' in html
        assert 'hx-target="#results"' in html


class TestBaseHtml:

    def test_base_html_loads_sse_extension(self) -> None:
        session = make_user_session()
        redis = MockRedis({"session:abc123": serialize_user_session(session)})

        app = FastAPI()
        app.state.redis = redis

        @app.get("/")
        async def index(request: Request, user: UserSession = Depends(require_auth)):
            return _templates.TemplateResponse(
                request, "pages/dashboard.html", {"user": user}
            )

        client = TestClient(app)
        client.cookies.set("session_id", "abc123")

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert "<script" in response.text
        assert "htmx.org/dist/ext/sse" in response.text

    def test_base_html_has_head_block(self) -> None:
        env = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader(
                        {
                            "test_child.html": (
                                '{% extends "base.html" %}'
                                '{% block head %}<link rel="stylesheet" href="/test.css">{% endblock %}'
                            )
                        }
                    ),
                    FileSystemLoader("app/templates"),
                ]
            )
        )
        template = env.get_template("test_child.html")

        html = template.render()

        head_section = html.split("</head>")[0]
        assert '<link rel="stylesheet" href="/test.css">' in head_section
