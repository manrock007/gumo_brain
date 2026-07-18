"""Secrets provider seam (Epic G2, SCAFFOLD).

A thin indirection over WHERE a secret value comes from, so a deployment can
move sensitive config out of the process environment (Docker/K8s file secrets
today; Vault later) without touching call sites. The default driver reads the
process environment — identical to what pydantic Settings already does — so an
instance that configures nothing keeps working exactly as before.

Also home to the subprocess env ALLOW-LIST (the security core of G2): every
Claude/CLI run gets an explicitly built environment instead of an inherited
``os.environ.copy()`` that would leak every operator secret (Sentry token,
dashboard password, OIDC client secret, the GitHub App private key, …) into
the model's shell.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("brain.secrets")


class NotConfigured(RuntimeError):
    """A provider was asked for a value it cannot serve (scaffold driver)."""


class SecretsProvider:
    """get(name) -> str|None; get_required(name) raises when absent."""

    name = "base"

    def get(self, key: str) -> str | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_required(self, key: str) -> str:
        value = self.get(key)
        if value is None or value == "":
            raise NotConfigured(f"required secret '{key}' is not available from {self.name}")
        return value


class EnvProvider(SecretsProvider):
    """DEFAULT — reads the process environment (what Settings already does)."""

    name = "env"

    def get(self, key: str) -> str | None:
        return os.environ.get(key)


class FileProvider(SecretsProvider):
    """Reads ``{SECRETS_DIR}/{name}`` files (Docker/K8s mounted secrets). Falls
    back to the environment for keys with no matching file, so a partial
    file-secrets deployment still resolves the rest from env."""

    name = "file"

    def __init__(self, base_dir: str):
        self._base = Path(base_dir)

    def get(self, key: str) -> str | None:
        path = self._base / key
        try:
            if path.is_file():
                return path.read_text().rstrip("\n")
        except OSError as e:  # unreadable file — fail closed to env, logged
            log.warning("secrets file %s unreadable: %s", key, e)
        return os.environ.get(key)


class VaultProvider(SecretsProvider):
    """SCAFFOLD — documented interface only; a later pass wires a real client."""

    name = "vault"

    def get(self, key: str) -> str | None:
        raise NotConfigured("the vault secrets provider is a scaffold — set SECRETS_PROVIDER=env|file")


def resolve(settings) -> SecretsProvider:
    """Pick the provider from SECRETS_PROVIDER. Unknown names fail closed to env."""
    name = (getattr(settings, "secrets_provider", "") or "env").strip().lower()
    if name == "file":
        base = (getattr(settings, "secrets_dir", "") or "").strip()
        if not base:
            log.warning("SECRETS_PROVIDER=file but SECRETS_DIR is empty — falling back to env")
            return EnvProvider()
        return FileProvider(base)
    if name == "vault":
        return VaultProvider()
    if name not in ("", "env"):
        log.warning("unknown SECRETS_PROVIDER=%r — falling back to env", name)
    return EnvProvider()


def read_secret(settings, key: str) -> str | None:
    """Resolve one secret VALUE. Supports the ``@/path/to/file`` convention
    (used by github_app_private_key): a value beginning with '@' is a filename
    to read. Otherwise the raw value is returned; an empty raw value is looked
    up through the configured provider (env/file)."""
    raw = (key or "")
    if raw.startswith("@"):
        try:
            return Path(raw[1:]).read_text()
        except OSError as e:
            log.warning("secret file %s unreadable: %s", raw[1:], e)
            return None
    return raw or None


# ---------------------------------------------------------------------------
# subprocess env allow-list (G2 security core, amendment blocker 9)
# ---------------------------------------------------------------------------

# Non-secret runtime plumbing every subprocess legitimately needs. The
# egress/TLS group (proxies + CA bundles) is treated as a NAMED baseline, not
# ad-hoc passthrough (amendment 1): a proxied/CA-pinned enterprise install must
# work out of the box, or the Claude CLI cannot reach the model API and git
# cannot verify TLS — every run would fail. GIT_*/AWS_* CA vars (GIT_SSL_CAINFO,
# AWS_CA_BUNDLE) additionally pass via the prefix catch-all in build_*.
BASE_ENV_ALLOW = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ", "SHELL", "USER",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    "npm_config_https_proxy", "npm_config_http_proxy", "npm_config_proxy", "npm_config_noproxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "CURL_CA_BUNDLE", "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", "NIX_SSL_CERT_FILE",
)

# The subset whose ABSENCE from a built env — while present in os.environ —
# silently breaks egress (model API unreachable, TLS unverifiable). Startup
# self-checks these so a starved var is loud, not a mid-run outage.
EGRESS_CRITICAL = (
    "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "ANTHROPIC_BASE_URL",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
)

# Model auth backends — first-party OAuth/API AND the documented cloud
# backends (Bedrock/Vertex) so a deployment on any supported backend is not
# silently starved mid-run.
AUTH_ENV_ALLOW = (
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    # Bedrock / Vertex
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
    "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS",
)

# gh CLI against GitHub Enterprise, and git identity/config passthrough.
VCS_ENV_ALLOW = (
    "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_API_URL",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_EXEC_PATH", "GIT_TERMINAL_PROMPT",
)

# NEVER pass these through even if an operator adds them to the extra allow-list:
# they are the secrets the allow-list exists to keep OUT of the run.
_HARD_DENY = frozenset({
    "DASHBOARD_PASSWORD", "CTRLLOOP_ADMIN_PASSWORD", "SENTRY_AUTH_TOKEN",
    "SENTRY_CLIENT_SECRET", "OIDC_CLIENT_SECRET", "SCIM_TOKEN", "SLACK_BOT_TOKEN",
    "GITHUB_APP_PRIVATE_KEY", "GITHUB_TOKEN", "ANALYTICS_CONFIG", "CHAT_API_KEY",
    "CLICKUP_TOKEN",  # injected explicitly per run, never inherited ambiently
})


def build_subprocess_env(settings, *, extra: dict | None = None) -> dict:
    """Build an allow-listed environment for a subprocess run. Only the plumbing
    + model-auth + VCS vars pass through from the ambient environment; operators
    widen it with SUBPROCESS_ENV_ALLOWLIST (comma list). Secrets in the hard-deny
    set are never inherited. ``extra`` (run-specific GH_TOKEN, CLICKUP_TOKEN,
    CLAUDE_CONFIG_DIR) is layered on top."""
    allow = set(BASE_ENV_ALLOW) | set(AUTH_ENV_ALLOW) | set(VCS_ENV_ALLOW)
    extra_names = (getattr(settings, "subprocess_env_allowlist", "") or "")
    for name in extra_names.split(","):
        name = name.strip()
        if name and name not in _HARD_DENY:
            allow.add(name)
    env: dict[str, str] = {}
    for name in allow:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    # AWS_* and GIT_* families the operator may rely on but we didn't enumerate
    for name, val in os.environ.items():
        if name in _HARD_DENY:
            continue
        if name.startswith(("AWS_", "GIT_")) and name not in env:
            env[name] = val
    for key, val in (extra or {}).items():
        if val is None:
            env.pop(key, None)
        else:
            env[key] = val
    return env


def egress_selfcheck(settings, *, environ: dict | None = None) -> list[str]:
    """Names of EGRESS_CRITICAL vars present in the ambient environment but
    DROPPED by the allow-list (amendment 1). A non-empty result means a proxied
    /CA-pinned deployment would fail every run — the caller logs it loudly at
    startup. Returns [] when nothing critical is starved."""
    source = os.environ if environ is None else environ
    built = build_subprocess_env(settings)
    return [name for name in EGRESS_CRITICAL
            if source.get(name) and name not in built]


def detect_auth_backend(env: dict) -> str:
    """A loud, non-secret label of which model-auth backend the built env
    carries — logged at startup so a starved var is visible, not silent."""
    if env.get("CLAUDE_CODE_USE_BEDROCK"):
        return "bedrock"
    if env.get("CLAUDE_CODE_USE_VERTEX"):
        return "vertex"
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic-api-key"
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "claude-max-oauth"
    return "none-detected"
