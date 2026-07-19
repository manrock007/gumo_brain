"""Identity providers (Epic E1): OIDC BUILD + SAML/SCIM SCAFFOLD + local fallback.

The engine authenticates through an IdentityProvider interface. LocalProvider
(password) is ALWAYS present and never disableable — the break-glass path.
OIDCProvider implements the authorization-code flow against any OIDC provider
(Okta/Entra/Google) via discovery; SAML/SCIM are scaffolds behind the same
interface for a later pass.

No new hard dependency: the OIDC flow is done directly with httpx + PyJWT
(already a dependency for the GitHub App), with an injected httpx transport +
discovery-doc override so tests never touch the network — the same seam pattern
as analytics.py / githubapp.py.
"""

import json
import logging
import secrets
import time

import httpx
import jwt

log = logging.getLogger("brain.identity")


class NotConfigured(RuntimeError):
    pass


class IdentityProvider:
    name = "base"
    kind = "password"  # password | browser_redirect | provisioning

    def enabled(self, settings) -> bool:
        return False

    def jit_role(self, claims: dict, settings) -> str:
        return "member"


class LocalProvider(IdentityProvider):
    """The password path — always present, never disableable."""

    name = "local"
    kind = "password"

    def enabled(self, settings) -> bool:
        return True


class OIDCProvider(IdentityProvider):
    """OIDC authorization-code flow (BUILD). Transport/discovery seams keep the
    test path off the network."""

    name = "oidc"
    kind = "browser_redirect"

    def __init__(self, transport=None, discovery_override: dict | None = None):
        self._transport = transport
        self._discovery_override = discovery_override

    def enabled(self, settings) -> bool:
        return settings.oidc_configured

    def _discovery(self, settings) -> dict:
        if self._discovery_override is not None:
            return self._discovery_override
        url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        with httpx.Client(transport=self._transport, timeout=15) as client:
            resp = client.get(url)
        resp.raise_for_status()
        return resp.json()

    def authorize_params(self, settings, state: str, nonce: str) -> tuple[str, dict]:
        """(authorization_endpoint, query params). The caller builds the redirect
        and persists (state, nonce) in the single-use txn table."""
        disc = self._discovery(settings)
        params = {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_url,
            "scope": settings.oidc_scopes,
            "state": state,
            "nonce": nonce,
        }
        return disc["authorization_endpoint"], params

    def exchange_and_validate(self, settings, code: str, nonce: str) -> dict:
        """Exchange the code, validate the id_token (nonce, aud, exp), and return
        normalized claims. Raises on any validation failure (fail closed)."""
        disc = self._discovery(settings)
        token_endpoint = disc["token_endpoint"]
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.oidc_redirect_url,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        }
        with httpx.Client(transport=self._transport, timeout=15) as client:
            resp = client.post(token_endpoint, data=data)
        if resp.status_code != 200:
            raise NotConfigured(f"token exchange failed: HTTP {resp.status_code}")
        tok = resp.json()
        id_token = tok.get("id_token")
        if not id_token:
            raise NotConfigured("no id_token in token response")
        # NOTE: signature verification against the provider JWKS is a hardening
        # follow-up; today we validate the binding claims (nonce/aud/exp/iss),
        # which requires the id_token to have come from our authenticated token
        # exchange over TLS. Decode without signature verification but WITH the
        # audience/expiry checks enforced below.
        claims = jwt.decode(id_token, options={"verify_signature": False,
                                               "verify_aud": False})
        if claims.get("nonce") != nonce:
            raise NotConfigured("id_token nonce mismatch")
        aud = claims.get("aud")
        aud_ok = (aud == settings.oidc_client_id
                  or (isinstance(aud, list) and settings.oidc_client_id in aud))
        if not aud_ok:
            raise NotConfigured("id_token audience mismatch")
        if claims.get("exp") and claims["exp"] < time.time():
            raise NotConfigured("id_token expired")
        # reject a token presented before its 'not before' time (with a small
        # skew allowance for clock drift), mirroring the exp check above
        if claims.get("nbf") and claims["nbf"] > time.time() + 60:
            raise NotConfigured("id_token not yet valid (nbf)")
        if settings.oidc_issuer and claims.get("iss") and \
                claims["iss"].rstrip("/") != settings.oidc_issuer.rstrip("/"):
            raise NotConfigured("id_token issuer mismatch")
        return self._normalize(claims, settings)

    def _normalize(self, claims: dict, settings) -> dict:
        groups = claims.get(settings.oidc_role_claim) if settings.oidc_role_claim else None
        if isinstance(groups, str):
            groups = [groups]
        # amendment 5: an email claim is trustworthy for identity linking ONLY
        # when the IdP asserts email_verified. An unverified email must never be
        # used to link/shadow an account (takeover vector) — carry the flag so
        # jit_provision can refuse to use an unverified address.
        ev = claims.get("email_verified")
        email_verified = ev is True or str(ev).lower() == "true"
        return {
            "external_id": str(claims.get("sub") or ""),
            "email": str(claims.get("email") or ""),
            "email_verified": email_verified,
            "username": str(claims.get("preferred_username")
                            or claims.get("email") or claims.get("sub") or ""),
            "groups": list(groups or []),
            "raw": claims,
        }

    def jit_role(self, claims: dict, settings) -> str:
        """Map claims -> instance role. Admin group wins; else the role map on
        the role claim value; else the default. Unknown -> default (member),
        never toward more privilege."""
        groups = claims.get("groups") or []
        if settings.oidc_admin_group and settings.oidc_admin_group in groups:
            return "instance_admin"
        try:
            role_map = json.loads(settings.oidc_role_map or "{}")
        except (ValueError, TypeError):
            role_map = {}
        for g in groups:
            mapped = role_map.get(g)
            if mapped in ("instance_admin", "member", "viewer"):
                return mapped
        default = (settings.oidc_default_role or "member").strip()
        return default if default in ("instance_admin", "member", "viewer") else "member"


class SAMLProvider(IdentityProvider):
    """SCAFFOLD — interface only; enabled() reads SAML_ENABLED, methods 501."""

    name = "saml"
    kind = "browser_redirect"

    def enabled(self, settings) -> bool:
        return bool(getattr(settings, "saml_enabled", False))

    def authorize_params(self, *a, **k):
        raise NotConfigured("SAML is a scaffold — not yet implemented")


class SCIMProvider(IdentityProvider):
    """SCAFFOLD — provisioning stub; create/update/deactivate return 501."""

    name = "scim"
    kind = "provisioning"

    def enabled(self, settings) -> bool:
        return bool(getattr(settings, "scim_enabled", False))

    def create_user(self, *a, **k):
        raise NotConfigured("SCIM is a scaffold — not yet implemented")

    def update_user(self, *a, **k):
        raise NotConfigured("SCIM is a scaffold — not yet implemented")

    def deactivate_user(self, *a, **k):
        raise NotConfigured("SCIM is a scaffold — not yet implemented")


def registry(settings, oidc_transport=None, oidc_discovery=None) -> list[IdentityProvider]:
    """Configured providers; LocalProvider is ALWAYS index 0 (break-glass)."""
    providers: list[IdentityProvider] = [LocalProvider()]
    oidc = OIDCProvider(transport=oidc_transport, discovery_override=oidc_discovery)
    if oidc.enabled(settings):
        providers.append(oidc)
    saml = SAMLProvider()
    if saml.enabled(settings):
        providers.append(saml)
    scim = SCIMProvider()
    if scim.enabled(settings):
        providers.append(scim)
    return providers


def new_state_nonce() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24)
