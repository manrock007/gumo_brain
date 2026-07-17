"""Dynamic project context (docs/ENGINE.md §10): the repo map, canonical
project, product name and business context are configuration — DB overrides >
env > code defaults — and every prompt is briefed from them."""

import json

import pytest

from app.config import (
    DEFAULT_BUSINESS_CONTEXT,
    DEFAULT_PRODUCT_NAME,
    RepoTarget,
    Settings,
    validate_repo_map,
)
from app.db import JobStore
from app.feature_prompts import build_bootstrap_prompt, build_stage_prompt
from app.prompts import build_fix_prompt, build_task_plan_prompt


# ---------- validate_repo_map ----------


def test_validate_repo_map_normalizes():
    cleaned = validate_repo_map({"api": {"repo": "acme/api"}})
    assert cleaned == {"api": {"repo": "acme/api", "base": "main",
                               "setup_cmd": None, "test_cmd": None, "allow": []}}


@pytest.mark.parametrize("bad", [
    {},                                        # empty
    [],                                        # not a dict
    {"a": "nope"},                             # entry not a dict
    {"a": {"repo": "no-slash"}},               # repo not owner/name
    {"a": {"repo": "o/r", "allow": "Bash"}},   # allow not a list
    {"": {"repo": "o/r"}},                     # empty slug
])
def test_validate_repo_map_rejects(bad):
    with pytest.raises(ValueError):
        validate_repo_map(bad)


# ---------- Settings.apply_runtime_overrides ----------


def test_overrides_apply_and_resolve(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    applied = s.apply_runtime_overrides({
        "product_name": "Acme",
        "business_context": "Acme builds rockets.",
        "repo_map": {"api": {"repo": "acme/api", "base": "dev"}},
        "memory_canonical_project": "api",
    })
    assert set(applied) == {"product_name", "business_context", "repo_map",
                            "memory_canonical_project"}
    assert s.product_name == "Acme"
    assert s.repo_for_project("api").repo == "acme/api"
    assert s.repo_for_project("api").base == "dev"
    assert s.repo_for_project("gumo") is None  # old map fully replaced
    assert s.target_for_repo("acme/api") is not None


def test_overrides_atomic_on_failure(tmp_path):
    """A canonical slug missing from the new map rejects the WHOLE payload."""
    s = Settings(data_dir=str(tmp_path))
    with pytest.raises(ValueError):
        s.apply_runtime_overrides({
            "product_name": "Acme",
            "repo_map": {"api": {"repo": "acme/api"}},  # canonical 'gumo' not in it
        })
    assert s.product_name == DEFAULT_PRODUCT_NAME
    assert s.repo_for_project("gumo") is not None


def test_overrides_skip_none_and_empty(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    s.apply_runtime_overrides({"product_name": None, "memory_canonical_project": "  "})
    assert s.product_name == DEFAULT_PRODUCT_NAME
    assert s.memory_canonical_project == "gumo"
    # …but an EMPTY business context is a deliberate choice, honored
    s.apply_runtime_overrides({"business_context": ""})
    assert s.business_context == ""


def test_overrides_survive_restart(tmp_path):
    """PUT persists to app_config; a fresh process re-applies at startup."""
    store = JobStore(str(tmp_path / "t.db"))
    s = Settings(data_dir=str(tmp_path))
    applied = s.apply_runtime_overrides({
        "product_name": "Acme",
        "repo_map": {"api": {"repo": "acme/api"}},
        "memory_canonical_project": "api",
    })
    for key, value in applied.items():
        store.config_set(key, json.loads(value) if key == "repo_map" else value)

    s2 = Settings(data_dir=str(tmp_path))
    s2.apply_runtime_overrides(store.config_all())
    assert s2.product_name == "Acme"
    assert s2.memory_canonical_project == "api"
    assert s2.repo_for_project("api").repo == "acme/api"

    store.config_clear()
    assert store.config_all() == {}


# ---------- prompts are briefed from the context ----------

TARGET = RepoTarget(repo="acme/api", base="main")
JOB = {"issue_id": "feat-1", "title": "Do the thing", "project": "api",
       "request": "Build the thing."}


def test_stage_prompt_carries_context():
    prompt = build_stage_prompt(
        target=TARGET, branch="brain/feat-1", job=JOB, stage=0,
        memory_context="", artifact_names=[], inline_artifacts={},
        guidance_entries=[], canonical_project="api",
        product_name="Acme", business_context="Acme builds rockets.",
    )
    assert "Acme Engine's build agent" in prompt
    assert "## Business context" in prompt
    assert "Acme builds rockets." in prompt
    assert "Gumo" not in prompt


def test_stage_prompt_defaults_stay_gumo():
    prompt = build_stage_prompt(
        target=TARGET, branch="b", job=JOB, stage=0,
        memory_context="", artifact_names=[], inline_artifacts={},
        guidance_entries=[],
    )
    assert "Gumo Engine's build agent" in prompt
    assert "## Business context" not in prompt  # default arg is empty


def test_business_context_is_capped():
    prompt = build_stage_prompt(
        target=TARGET, branch="b", job=JOB, stage=0,
        memory_context="", artifact_names=[], inline_artifacts={},
        guidance_entries=[], business_context="x" * 10000,
    )
    assert "truncated" in prompt
    assert "x" * 5000 not in prompt


def test_fix_and_task_prompts_carry_context():
    issue = {"title": "boom", "url": "u", "id": "1", "project": "api",
             "culprit": "c", "times_seen": 1, "users_affected": 1}
    prompt = build_fix_prompt(target=TARGET, branch="b", issue=issue, stacktrace="tb",
                              clickup_task_id=None, product_name="Acme",
                              business_context="Acme builds rockets.")
    assert "for the Acme platform" in prompt
    assert "Acme builds rockets." in prompt

    task = {"title": "t", "url": "", "id": "task-1", "project": "api"}
    prompt = build_task_plan_prompt(target=TARGET, branch="b", task=task, request="r",
                                    clickup_task_id=None, product_name="Acme",
                                    business_context="Acme builds rockets.")
    assert "for the Acme platform" in prompt
    assert "Acme builds rockets." in prompt


def test_business_block_states_memory_precedence():
    """docs/ENGINE.md §10: the precedence note is stated in the prompt itself,
    so it survives an operator replacing the default text."""
    from app.prompts import business_block

    block = business_block("Acme builds rockets.")
    assert "takes precedence" in block


def test_shepherd_prompt_carries_context():
    from app.prompts import build_shepherd_prompt

    prompt = build_shepherd_prompt(target=TARGET, pr_url="http://pr", branch="b",
                                   findings=[], product_name="Acme",
                                   business_context="Acme builds rockets.")
    assert "Acme Engine's PR shepherd" in prompt
    assert "Acme builds rockets." in prompt


def test_bootstrap_prompt_carries_context():
    prompt = build_bootstrap_prompt(target=TARGET, branch="b", project="api",
                                    is_canonical=True, run=1, product_name="Acme",
                                    business_context="Acme builds rockets.")
    assert "Acme Engine's product memory" in prompt
    assert "Acme builds rockets." in prompt


def test_default_business_context_describes_defaults():
    """The shipped default stays the Gumo setup — swap it via /api/context."""
    s = Settings()
    assert s.business_context == DEFAULT_BUSINESS_CONTEXT
    assert "manrock007/gumoserver" in s.business_context
