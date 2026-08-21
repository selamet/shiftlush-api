"""The published contract.

`openapi/v1.yaml` is not a by-product — it is what the frontend's client is
generated from and what a second consumer would integrate against. These tests
guard the parts of it that no other test would notice going wrong, because a
contract can be perfectly valid and still be wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.error_codes import ErrorCode

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi" / "v1.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    if not SPEC_PATH.exists():  # pragma: no cover - only before the first build
        pytest.fail("openapi/v1.yaml is missing. Run `make spec`.")
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


class TestErrorVocabulary:
    def test_every_code_the_api_can_return_is_in_the_contract(self, spec):
        published = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
        declared = {code.value for code in ErrorCode}

        # A code the client has never heard of arrives as a blank message on a
        # screen, for an error nobody can then reproduce from the report.
        assert declared - published == set()

    def test_the_contract_invents_no_codes_of_its_own(self, spec):
        published = set(spec["components"]["schemas"]["ErrorCode"]["enum"])
        declared = {code.value for code in ErrorCode}

        # The other direction matters too: a code left in the contract after
        # being removed from the enum is a branch the client keeps handling for
        # a case that can no longer happen.
        assert published - declared == set()

    def test_the_error_envelope_carries_a_request_id(self, spec):
        envelope = spec["components"]["schemas"]["ErrorResponse"]
        error = envelope["properties"]["error"]

        assert set(error["required"]) == {"code", "request_id"}
        # No `message`: the words live in the client's translation file, so
        # wording changes in one place and a second language does not touch the
        # API.
        assert "message" not in error["properties"]


class TestTheContractItself:
    def test_it_is_generated_in_english(self, spec):
        text = SPEC_PATH.read_text(encoding="utf-8")
        turkish = set("çğıöşüÇĞİÖŞÜ")

        # The application runs in Turkish, which means Django activates that
        # language for management commands too. Left alone, DRF's own translated
        # help strings for paging end up documenting the frontend's generated
        # client.
        offenders = [line for line in text.splitlines() if turkish & set(line)]
        assert offenders == []

    def test_every_path_is_versioned_or_infrastructure(self, spec):
        for path in spec["paths"]:
            # /health and /ready are deliberately outside the version prefix —
            # a load balancer does not negotiate versions — and they are not in
            # the schema at all, so anything here must be versioned.
            assert path.startswith("/api/v1/"), path

    def test_no_endpoint_is_published_without_a_security_scheme(self, spec):
        public = {
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/password-reset",
            "/api/v1/auth/password-reset/confirm",
            "/api/v1/auth/email/verify",
            "/api/v1/invitations/accept",
        }
        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                if method not in {"get", "post", "patch", "put", "delete"}:
                    continue
                if path in public or path.startswith("/api/v1/invitations/verify/"):
                    continue
                # An endpoint published without a scheme reads to a generated
                # client as one that needs no token, and the mistake is only
                # found by the person who gets a 401 they did not expect.
                assert operation.get("security"), f"{method.upper()} {path}"
