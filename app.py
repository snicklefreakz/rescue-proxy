#!/usr/bin/env python3
"""
Recreate the Remnawave user "rescue" and publish its connection key
(for example, a vless:// URI) to a GitHub Gist and SourceCraft repository.

Install dependency:
    python3 -m pip install 'remnawave-api>=3.4.5,<3.5'

Copy .env.example to .env and set the panel, GitHub, and SourceCraft credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from remnawave import RemnawaveSDK
    from remnawave.enums import TrafficLimitStrategy
    from remnawave.exceptions import NotFoundError
    from remnawave.models import CreateUserBodyDto
except ImportError as exc:
    print("Missing dependency. Install it with: python3 -m pip install 'remnawave-api>=3.4.5,<3.5'", file=sys.stderr)
    raise SystemExit(1) from exc


ENV_FILE = Path(".env")


def load_dotenv(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        key = key.removeprefix("export ").strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def required_int_env(name: str) -> int:
    value = require_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def gist_id_from_url(url: str) -> str:
    match = re.search(r"gist\.github\.com/(?:[^/]+/)?([0-9a-fA-F]+)", url)
    if match:
        return match.group(1)

    if re.fullmatch(r"[0-9a-fA-F]+", url):
        return url

    raise RuntimeError("GITHUB_GIST_URL must be a GitHub Gist URL or raw gist id")


def github_request(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "rescue-proxy",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API failed with HTTP {exc.code}: {error_body}") from exc

    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def update_gist(gist_url: str, filename: str, content: str, token: str) -> str:
    gist_id = gist_id_from_url(gist_url)
    updated = github_request(
        "PATCH",
        f"/gists/{gist_id}",
        token,
        {"files": {filename: {"content": content}}},
    )
    return updated.get("html_url", gist_url)


def _run_git(
    args: list[str], cwd: Path | None, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is required to publish to SourceCraft") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown git error").strip()
        raise RuntimeError(f"SourceCraft git command failed: {detail}") from exc


def publish_sourcecraft(repo_url: str, filename: str, content: str) -> str:
    """Commit content to a file in SourceCraft and push it to the default branch."""
    relative_file = Path(filename)
    if relative_file.is_absolute() or ".." in relative_file.parts or not relative_file.name:
        raise RuntimeError("SOURCECRAFT_FILE must be a relative file path inside the repository")

    git_env = os.environ.copy()
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    token = os.getenv("SOURCECRAFT_TOKEN")

    with tempfile.TemporaryDirectory(prefix="rescue-sourcecraft-") as temp_dir:
        if token:
            # Supply the PAT without placing it in the clone URL, process arguments, or git config.
            askpass = Path(temp_dir) / "askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' git ;;\n"
                "  *) printf '%s\\n' \"$SOURCECRAFT_TOKEN\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            git_env["GIT_ASKPASS"] = str(askpass)

        checkout = Path(temp_dir) / "repo"
        _run_git(["clone", "--depth=1", repo_url, str(checkout)], None, git_env)

        destination = checkout / relative_file
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        _run_git(["add", "--", relative_file.as_posix()], checkout, git_env)

        changed = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=checkout, env=git_env
        ).returncode
        if changed == 0:
            return repo_url
        if changed != 1:
            raise RuntimeError("Could not inspect staged SourceCraft changes")

        _run_git(
            [
                "-c",
                f"user.name={require_env('SOURCECRAFT_GIT_NAME')}",
                "-c",
                f"user.email={require_env('SOURCECRAFT_GIT_EMAIL')}",
                "commit",
                "-m",
                require_env("SOURCECRAFT_COMMIT_MESSAGE"),
            ],
            checkout,
            git_env,
        )
        _run_git(["push", "origin", "HEAD"], checkout, git_env)

    return repo_url


def get_model_items(response: Any, field_name: str) -> list[Any]:
    if isinstance(response, list):
        return response
    return list(getattr(response, field_name, []))


async def pick_connection_key(remnawave: RemnawaveSDK, user_id: int) -> str:
    preferred_protocol = require_env("RESCUE_CONNECTION_PROTOCOL").strip().lower()
    response = await remnawave.subscriptions.get_connection_keys_by_user_id(user_id)
    enabled_keys = list(getattr(response, "enabled_keys", []))

    if not enabled_keys:
        raise RuntimeError("Remnawave returned no enabled connection keys for rescue user")

    preferred_prefix = f"{preferred_protocol}://"
    for key in enabled_keys:
        if key.lower().startswith(preferred_prefix):
            return key

    # Fallback to first enabled key when preferred protocol is unavailable.
    return enabled_keys[0]


async def find_internal_squad_uuid(remnawave: RemnawaveSDK, squad_name: str) -> str:
    response = await remnawave.internal_squads.get_internal_squads()
    squads = get_model_items(response, "internal_squads")
    matches = [squad for squad in squads if squad.name == squad_name]

    if not matches:
        available = ", ".join(squad.name for squad in squads) or "none"
        raise RuntimeError(
            f"Internal squad {squad_name!r} not found. Available internal squads: {available}"
        )

    if len(matches) > 1:
        raise RuntimeError(f"Found multiple internal squads named {squad_name!r}")

    return str(matches[0].uuid)


def build_create_user_request(username: str, internal_squad_uuid: str) -> CreateUserBodyDto:
    days = required_int_env("RESCUE_DAYS")
    if days <= 0:
        raise RuntimeError("RESCUE_DAYS must be greater than 0")

    traffic_strategy = require_env("RESCUE_TRAFFIC_LIMIT_STRATEGY")
    traffic_limit_bytes = required_int_env("RESCUE_TRAFFIC_LIMIT_BYTES")
    if traffic_limit_bytes < 0:
        raise RuntimeError("RESCUE_TRAFFIC_LIMIT_BYTES must be 0 or greater")

    try:
        traffic_limit_strategy = TrafficLimitStrategy[traffic_strategy]
    except KeyError as exc:
        valid = ", ".join(member.name for member in TrafficLimitStrategy)
        raise RuntimeError(
            f"Invalid RESCUE_TRAFFIC_LIMIT_STRATEGY={traffic_strategy!r}. Valid values: {valid}"
        ) from exc

    kwargs: dict[str, Any] = {
        "username": username,
        "expire_at": datetime.now(UTC) + timedelta(days=days),
        "description": require_env("RESCUE_DESCRIPTION"),
        "traffic_limit_strategy": traffic_limit_strategy,
        "traffic_limit_bytes": traffic_limit_bytes,
        "active_internal_squads": [internal_squad_uuid],
    }

    external_squad_uuid = os.getenv("RESCUE_EXTERNAL_SQUAD_UUID")
    if external_squad_uuid:
        kwargs["external_squad_uuid"] = external_squad_uuid

    return CreateUserBodyDto(**kwargs)


async def delete_existing_user(remnawave: RemnawaveSDK, username: str) -> None:
    try:
        user = await remnawave.users.get_user_by_username(username)
    except NotFoundError:
        return

    await remnawave.users.delete_user(user.id)
    print(f"Deleted existing user: {username} ({user.id})")


async def recreate_rescue_user() -> str:
    load_dotenv()

    remnawave = RemnawaveSDK(
        base_url=require_env("REMNAWAVE_URL").rstrip("/"),
        token=require_env("REMNAWAVE_TOKEN"),
    )
    username = require_env("RESCUE_USERNAME")
    internal_squad_name = require_env("RESCUE_INTERNAL_SQUAD_NAME")
    display_name = require_env("RESCUE_DISPLAY_NAME")
    require_env("RESCUE_CONNECTION_PROTOCOL")
    gist_url_config = require_env("GITHUB_GIST_URL")
    gist_filename = require_env("GITHUB_GIST_FILE")
    github_token = require_env("GITHUB_TOKEN")
    sourcecraft_repo_url = require_env("SOURCECRAFT_REPO_URL")
    sourcecraft_filename = require_env("SOURCECRAFT_FILE")
    sourcecraft_browse_url = require_env("SOURCECRAFT_BROWSE_URL")
    require_env("SOURCECRAFT_GIT_NAME")
    require_env("SOURCECRAFT_GIT_EMAIL")
    require_env("SOURCECRAFT_COMMIT_MESSAGE")

    internal_squad_uuid = await find_internal_squad_uuid(remnawave, internal_squad_name)
    request = build_create_user_request(username, internal_squad_uuid)

    # Complete all local and read-only validation before removing the current
    # rescue account, so configuration failures do not leave it unavailable.
    await delete_existing_user(remnawave, username)
    user = await remnawave.users.create_user(request)

    raw_connection_key = await pick_connection_key(remnawave, user.id)
    if not raw_connection_key:
        raise RuntimeError("Remnawave SDK returned an empty connection key")

    # Force a stable display name after '#' in the URI.
    if "#" in raw_connection_key:
        base, _hash, _name = raw_connection_key.partition("#")
        connection_key = f"{base}#{display_name}"
    else:
        connection_key = f"{raw_connection_key}#{display_name}"

    published_content = f'`{connection_key}`'
    gist_url = update_gist(
        gist_url=gist_url_config,
        filename=gist_filename,
        content=published_content,
        token=github_token,
    )
    publish_sourcecraft(
        repo_url=sourcecraft_repo_url,
        filename=sourcecraft_filename,
        content=published_content,
    )
    print(f"Created user: {username} ({user.id})")
    print(f"Updated gist: {gist_url}")
    print(f"Updated SourceCraft repository: {sourcecraft_browse_url}")

    return connection_key


def main() -> int:
    try:
        asyncio.run(recreate_rescue_user())
    except Exception as exc:
        traceback.print_exc()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
