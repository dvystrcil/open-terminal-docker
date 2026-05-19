"""
title: Git Sync Filter
author: custom
version: 1.3
license: MIT
description: >
  A lightweight filter that adds /sync_up and /sync_down slash commands
  to any Open WebUI model. Attaches to multiple models simultaneously.

  /sync_up   — stage all changes, commit, and push to GitHub
  /sync_down — pull latest changes from GitHub
  /sync_pr   — create a branch, commit, push, and open a pull request

  Works with the bible_bridge.py service running in the open-terminal pod.
  The bridge handles the actual git operations; this filter just routes
  the slash commands to the correct bridge endpoint and returns the result.

  Attach this filter to any model that needs git sync access:
    - Dual Model: Reasoner + Coder  (for code repos)
    - Fiction Writing Assistant     (for story bible repos)
    - Any future models

  Per-model project routing: set `model_project_map` to a
  {model_id: project_name} dict so /sync_up in the fiction assistant
  commits the bible while /sync_up in the coding assistant commits the
  code project. Models not in the map fall back to `sync_project`.

  Install:
    Admin Panel > Settings > Pipelines > upload this file.
    Then attach to any model in Workspace > Models > Filters.

  Prompts to create in Open WebUI (Workspace > Prompts):
    /sync_up   → "sync up: commit and push all changes to GitHub"
    /sync_down → "sync down: pull latest changes from GitHub"
    /sync_pr   → "sync pr: create a branch, commit, and open a pull request"

requirements: requests
"""

import logging
import time
import threading
from typing import Dict, Optional

import requests
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync_filter")

# ── Trigger phrases ───────────────────────────────────────────────────────────

INTERNAL_PREFIXES = (
    "### Task:\nSuggest",
    "### Task:\nGenerate",
    "### Task:\nCreate a concise",
    "### Task:\nAnalyze",
)

SYNC_UP_PHRASES = (
    "sync up:",
    "sync up ",
    "/sync_up",
)

SYNC_DOWN_PHRASES = (
    "sync down:",
    "sync down ",
    "/sync_down",
)

SYNC_PR_PHRASES = (
    "sync pr:",
    "sync pr ",
    "/sync_pr",
)


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Pipeline:

    class Valves(BaseModel):
        pipelines: list[str] = Field(
            default=["dual-model-reasoner-coder-n8n", "fiction-writer"],
            description=(
                "Model IDs this filter applies to. Add any model that should "
                "have /sync_up and /sync_down available."
            ),
        )
        bridge_url: str = Field(
            default="http://open-terminal.open-terminal.svc.cluster.local:8765",
            description="URL of the bible_bridge.py service in the open-terminal pod.",
        )
        bridge_token: str = Field(
            default="",
            description=(
                "Shared secret token for bridge auth. "
                "Must match BRIDGE_TOKEN env var on the bridge."
            ),
        )
        sync_project: str = Field(
            default="",
            description=(
                "Fallback git project name (subdirectory under BIBLE_ROOT on the bridge), "
                "e.g. 'standing-patrol'. Used when the active model is not listed in "
                "model_project_map. Required if model_project_map is empty — "
                "/sync_up, /sync_down, and /sync_pr all refuse to run with no project resolved."
            ),
        )
        model_project_map: Dict[str, str] = Field(
            default_factory=dict,
            description=(
                "Per-model project overrides as {model_id: project_name}. "
                "When /sync_* runs, the filter looks up the active model in this dict "
                "and uses that project; falls back to sync_project if not present. "
                "Example: "
                "{\"fiction-writer\": \"standing-patrol\", "
                "\"dual-model-reasoner-coder-n8n\": \"homelab\"}."
            ),
        )
        sync_commit_message: str = Field(
            default="auto",
            description=(
                "Custom commit message for /sync_up and /sync_pr. "
                "Set to 'auto' (default) or leave empty to use an "
                "auto-generated timestamped message."
            ),
        )
        sync_pr_base_branch: str = Field(
            default="main",
            description=(
                "Base branch that pull requests target (default: main). "
                "The new branch created by /sync_pr will be merged into this."
            ),
        )
        sync_pr_title: str = Field(
            default="auto",
            description=(
                "Default pull request title for /sync_pr. "
                "Set to 'auto' (default) or leave empty to use an "
                "auto-generated title (the commit message)."
            ),
        )
        sync_actor: str = Field(
            default="auto",
            description=(
                "Identifier passed to bible_bridge as the `actor` field. "
                "The bridge uses it to set the commit author "
                "(dvystrcil-<actor> <actor@dvystrcil.local>) so forensics "
                "can tell which agent made each commit. Per dvystrcil/"
                "homelab#125. Set to 'auto' (default) or leave empty to "
                "let the bridge fall back to its platform default 'owui'. "
                "Override per-model: this filter is attached to fiction-writer "
                "(set to 'fiction') and dual-model-reasoner-coder-n8n "
                "(set to 'dmf') for direct attribution."
            ),
        )

    def __init__(self):
        self.type = "filter"
        self.name = "Git Sync Filter"
        self.valves = self.Valves()

    async def on_startup(self):
        log.info("[sync] Git Sync Filter starting up v1.3")
        log.info(f"[sync] Bridge: {self.valves.bridge_url}")
        log.info(f"[sync] Fallback project: {self.valves.sync_project or '(unset)'}")
        if self.valves.model_project_map:
            log.info(f"[sync] Per-model map: {self.valves.model_project_map}")
        else:
            log.info("[sync] Per-model map: (empty — all models use fallback)")
        log.info(f"[sync] Auth: {'enabled' if self.valves.bridge_token else 'disabled'}")

    async def on_shutdown(self):
        log.info("[sync] Git Sync Filter shutting down")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.valves.bridge_token:
            h["Authorization"] = "Bearer " + self.valves.bridge_token
        return h

    def _url(self, path: str) -> str:
        return self.valves.bridge_url.rstrip("/") + path

    def _is_internal(self, message: str) -> bool:
        return any(message.startswith(p) for p in INTERNAL_PREFIXES)

    def _get_last_user_message(self, messages: list) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    return " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                return content
        return ""

    def _is_sync_up(self, message: str) -> bool:
        lower = message.lower().strip()
        return any(lower.startswith(p) or lower == p.strip() for p in SYNC_UP_PHRASES)

    def _is_sync_down(self, message: str) -> bool:
        lower = message.lower().strip()
        return any(lower.startswith(p) or lower == p.strip() for p in SYNC_DOWN_PHRASES)

    def _is_sync_pr(self, message: str) -> bool:
        lower = message.lower().strip()
        return any(lower.startswith(p) or lower == p.strip() for p in SYNC_PR_PHRASES)

    # ── Sentinel-aware valve resolution ──────────────────────────────────────
    # OWUI's admin UI rejects literally-empty string valves on save. To keep
    # the schema "empty means use the default" while satisfying the UI, three
    # valves (sync_commit_message, sync_pr_title, sync_actor) accept "auto"
    # as a sentinel that the runtime treats identically to "". See
    # dvystrcil/open-terminal-docker#34.
    _SENTINEL_EMPTY = ("auto",)

    @classmethod
    def _valve_value(cls, raw: str) -> str:
        """Return the valve's effective value; '' if raw is empty or sentinel."""
        stripped = (raw or "").strip()
        if stripped.lower() in cls._SENTINEL_EMPTY:
            return ""
        return stripped

    def _resolve_project(self, body: dict) -> str:
        """
        Pick the project for this request. Per-model override in
        model_project_map wins; otherwise fall back to sync_project.
        """
        model_id = (body.get("model") or "").strip()
        if model_id and model_id in self.valves.model_project_map:
            mapped = self.valves.model_project_map[model_id].strip()
            if mapped:
                return mapped
        return self.valves.sync_project.strip()

    def _no_project_warning(self) -> str:
        return (
            "WARNING: no project resolved for this model. "
            "Add this model to the model_project_map valve, "
            "or set the sync_project valve as a fallback."
        )

    # ── Git operations ────────────────────────────────────────────────────────

    def _sync_up(self, project: str) -> str:
        if not project:
            return self._no_project_warning()
        try:
            commit_msg = self._valve_value(self.valves.sync_commit_message) or (
                "sync: manual sync-up " + time.strftime("%Y-%m-%d %H:%M")
            )
            payload = {"project": project, "commit_message": commit_msg}
            actor = self._valve_value(self.valves.sync_actor)
            if actor:
                payload["actor"] = actor
            resp = requests.post(
                self._url("/bible/sync"),
                headers=self._headers(),
                json=payload,
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("ok", False)
            status = data.get("status", "no status returned")
            if ok:
                return "SUCCESS: " + project + " synced up — " + status
            else:
                return "FAILED: " + project + " sync up — " + status
        except Exception as e:
            log.warning("[sync] sync_up failed: " + str(e))
            return "FAILED: sync up error — " + str(e)

    def _sync_down(self, project: str) -> str:
        if not project:
            return self._no_project_warning()
        try:
            resp = requests.post(
                self._url("/bible/pull"),
                headers=self._headers(),
                json={"project": project},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("ok", False)
            output = data.get("output", "no output")
            if ok:
                return "SUCCESS: " + project + " synced down — " + (output or "already up to date")
            else:
                return "FAILED: " + project + " sync down — " + output
        except Exception as e:
            log.warning("[sync] sync_down failed: " + str(e))
            return "FAILED: sync down error — " + str(e)

    def _sync_pr(self, project: str) -> str:
        """
        Create a branch, stage all changes, commit, push, and open a PR via gh CLI
        on the bridge host. The bridge exposes a /bible/pr endpoint for this.
        """
        if not project:
            return self._no_project_warning()
        try:
            commit_msg = self._valve_value(self.valves.sync_commit_message) or (
                "sync: " + time.strftime("%Y-%m-%d %H:%M")
            )
            pr_title = self._valve_value(self.valves.sync_pr_title) or commit_msg
            branch = "fwf/" + time.strftime("%Y%m%d-%H%M%S")
            payload = {
                "project": project,
                "branch": branch,
                "commit_message": commit_msg,
                "pr_title": pr_title,
                "base_branch": self.valves.sync_pr_base_branch,
            }
            actor = self._valve_value(self.valves.sync_actor)
            if actor:
                payload["actor"] = actor
            resp = requests.post(
                self._url("/bible/pr"),
                headers=self._headers(),
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("ok", False)
            status = data.get("status", "no status returned")
            pr_url = data.get("pr_url", "")
            if ok:
                result = "SUCCESS: PR created for " + project + " — " + status
                if pr_url:
                    result += "\nPR URL: " + pr_url
                return result
            else:
                return "FAILED: PR creation for " + project + " — " + status
        except Exception as e:
            log.warning("[sync] sync_pr failed: " + str(e))
            return "FAILED: sync pr error — " + str(e)

    # ── Filter entrypoints ────────────────────────────────────────────────────

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        user_message = self._get_last_user_message(messages)

        if self._is_internal(user_message):
            return body

        if self._is_sync_up(user_message):
            project = self._resolve_project(body)
            log.info("[sync] SYNC UP triggered (model=%s project=%s)",
                     body.get("model", "?"), project or "(unresolved)")
            result = self._sync_up(project)
            log.info("[sync] SYNC UP result: " + result)
            messages.append({
                "role": "system",
                "content": (
                    "The user ran /sync_up. Report this result to the user "
                    "exactly as written and nothing else:\n\n" + result
                )
            })
            body["messages"] = messages
            return body

        if self._is_sync_down(user_message):
            project = self._resolve_project(body)
            log.info("[sync] SYNC DOWN triggered (model=%s project=%s)",
                     body.get("model", "?"), project or "(unresolved)")
            result = self._sync_down(project)
            log.info("[sync] SYNC DOWN result: " + result)
            messages.append({
                "role": "system",
                "content": (
                    "The user ran /sync_down. Report this result to the user "
                    "exactly as written and nothing else:\n\n" + result
                )
            })
            body["messages"] = messages
            return body

        if self._is_sync_pr(user_message):
            project = self._resolve_project(body)
            log.info("[sync] SYNC PR triggered (model=%s project=%s)",
                     body.get("model", "?"), project or "(unresolved)")
            result = self._sync_pr(project)
            log.info("[sync] SYNC PR result: " + result)
            messages.append({
                "role": "system",
                "content": (
                    "The user ran /sync_pr. Report this result to the user "
                    "exactly as written and nothing else:\n\n" + result
                )
            })
            body["messages"] = messages
            return body

        return body

    async def outlet(self, body: dict, user: Optional[dict] = None) -> dict:
        return body
