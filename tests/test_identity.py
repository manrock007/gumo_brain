"""Epic E1 — OIDC SSO + JIT provisioning + local fallback; SAML/SCIM scaffolds."""

import time

import httpx
import jwt
import pytest

from app import identity
from app.auth import (SSOConflict, UNUSABLE_PW, is_break_glass, jit_provision,
                      verify_login, hash_password)
from app.config import Settings


DISCOVERY = {
    "authorization_endpoint": "https://idp.example/authorize",
    "token_endpoint": "https://idp.example/token",
}


def _oidc_settings(**over):
    base = dict(oidc_enabled=True, oidc_issuer="https://idp.example",
                oidc_client_id="client-1", oidc_client_secret="shh",
                oidc_redirect_url="https://host/auth/oidc/callback",
                oidc_role_claim="groups", oidc_admin_group="admins",
                oidc_role_map='{"eng": "member"}')
    base.update(over)
    return Settings(**base)


def _id_token(claims):
    return jwt.encode(claims, "secret", algorithm="HS256")


def _token_transport(claims):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id_token": _id_token(claims)})
    return httpx.MockTransport(handler)


def _provider(claims=None):
    transport = _token_transport(claims) if claims else None
    return identity.OIDCProvider(transport=transport, discovery_override=DISCOVERY)


# --- driver / config ---

def test_oidc_configured_fail_closed():
    assert _oidc_settings().oidc_configured is True
    assert Settings(oidc_enabled=True, oidc_issuer="").oidc_configured is False  # partial -> off


def test_registry_local_always_first():
    provs = identity.registry(Settings())
    assert provs[0].name == "local"
    assert len(provs) == 1  # nothing else configured
    provs2 = identity.registry(_oidc_settings())
    assert [p.name for p in provs2] == ["local", "oidc"]


def test_authorize_params_carry_state_nonce():
    endpoint, params = _provider().authorize_params(_oidc_settings(), "st8", "n0nce")
    assert endpoint == DISCOVERY["authorization_endpoint"]
    assert params["state"] == "st8" and params["nonce"] == "n0nce"
    assert params["client_id"] == "client-1"


def test_exchange_validates_nonce_aud_exp():
    s = _oidc_settings()
    claims = {"sub": "u1", "email": "u1@example", "nonce": "N", "aud": "client-1",
              "iss": "https://idp.example", "exp": time.time() + 300, "groups": ["eng"]}
    prov = _provider(claims)
    norm = prov.exchange_and_validate(s, "code", "N")
    assert norm["external_id"] == "u1" and norm["email"] == "u1@example"
    # nonce mismatch -> fail closed
    with pytest.raises(Exception):
        prov.exchange_and_validate(s, "code", "WRONG")


def test_jit_role_mapping():
    s = _oidc_settings()
    prov = _provider()
    assert prov.jit_role({"groups": ["admins"]}, s) == "instance_admin"
    assert prov.jit_role({"groups": ["eng"]}, s) == "member"
    assert prov.jit_role({"groups": ["unknown"]}, s) == "member"  # default


# --- JIT provisioning ---

def test_jit_creates_user_with_mapped_role(store):
    s = _oidc_settings()
    prov = _provider()
    claims = {"external_id": "ext-1", "email": "a@x", "username": "alice",
              "groups": ["admins"]}
    user = jit_provision(store, s, prov, claims)
    assert user["role"] == "instance_admin"
    assert user["auth_provider"] == "oidc"
    assert user["pw_hash"] == UNUSABLE_PW
    # repeat login finds the SAME user by external_id
    again = jit_provision(store, s, prov, claims)
    assert again["id"] == user["id"]


def test_jit_never_adopts_local_account_by_email(store):
    s = _oidc_settings()
    prov = _provider()
    # a local admin exists with username 'alice'
    store.user_create("alice", hash_password("localpw"), role="instance_admin",
                      must_change_pw=False)
    claims = {"external_id": "ext-9", "email": "alice", "username": "alice",
              "groups": ["eng"]}
    with pytest.raises(SSOConflict):
        jit_provision(store, s, prov, claims)
    # the local account's password is untouched (break-glass preserved)
    local = store.user_get("alice")
    assert is_break_glass(local)


def test_role_sync_never_demotes_last_admin(store):
    s = _oidc_settings(oidc_role_sync=True)
    prov = _provider()
    # provision an SSO admin (the only admin)
    admin = jit_provision(store, s, prov, {"external_id": "ext-a", "email": "b@x",
                                           "username": "bob", "groups": ["admins"]})
    assert admin["role"] == "instance_admin"
    # next login maps to member (no admin group) -> would demote the last admin: refused
    again = jit_provision(store, s, prov, {"external_id": "ext-a", "email": "b@x",
                                          "username": "bob", "groups": ["eng"]})
    assert again["role"] == "instance_admin"


def test_sso_only_account_cannot_password_login(store):
    s = _oidc_settings()
    prov = _provider()
    jit_provision(store, s, prov, {"external_id": "ext-s", "email": "c@x",
                                   "username": "carol", "groups": ["eng"]})
    with pytest.raises(Exception):
        verify_login(store, Settings(), "carol", "anything")


def test_local_login_still_works_break_glass(store):
    store.user_create("root", hash_password("s3cret!!"), role="instance_admin",
                      must_change_pw=False)
    user = verify_login(store, Settings(), "root", "s3cret!!")
    assert user["username"] == "root"


def test_saml_scim_inert_scaffolds():
    s = Settings()
    assert identity.SAMLProvider().enabled(s) is False
    assert identity.SCIMProvider().enabled(s) is False
    with pytest.raises(identity.NotConfigured):
        identity.SAMLProvider().authorize_params(s, "s", "n")
    with pytest.raises(identity.NotConfigured):
        identity.SCIMProvider().create_user()
