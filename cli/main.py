"""
mcp-cli — SkillfulMCP catalog management CLI.

All commands communicate with the MCP server via HTTP.
Set MCP_SERVER_URL and MCP_ADMIN_KEY in your environment (or .env file).

Usage examples:
  mcp-cli skill add --id customer-insights --name "Customer Insights" \\
      --description "Retrieves CRM data" --version 1.0.0 --skillset sales-assistant

  mcp-cli agent add --id agent-123 --name "chatbot" \\
      --skillsets sales-assistant --scope read,execute

  mcp-cli token issue --agent-id agent-123 --expires-in 3600
"""

import json
import os
from pathlib import Path
from typing import Optional

import httpx
import typer
import yaml
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(name="mcp-cli", help="SkillfulMCP catalog management CLI", no_args_is_help=True)
skill_app = typer.Typer(help="Manage skills", no_args_is_help=True)
agent_app = typer.Typer(help="Manage agents", no_args_is_help=True)
token_app = typer.Typer(help="Issue JWT tokens", no_args_is_help=True)
catalog_app = typer.Typer(help="Import/export catalog data", no_args_is_help=True)
superadmin_app = typer.Typer(
    help="Platform superadmin operations (password rotation, etc.)",
    no_args_is_help=True,
)

app.add_typer(skill_app, name="skill")
app.add_typer(agent_app, name="agent")
app.add_typer(token_app, name="token")
app.add_typer(catalog_app, name="catalog")
app.add_typer(superadmin_app, name="superadmin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return os.environ.get("MCP_SERVER_URL", "http://localhost:8000")


def _admin_headers() -> dict:
    return {"X-Admin-Key": os.environ.get("MCP_ADMIN_KEY", "")}


def _handle_error(resp: httpx.Response) -> None:
    if not resp.is_success:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Error {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# skill commands
# ---------------------------------------------------------------------------

@skill_app.command("add")
def skill_add(
    id: str = typer.Option(..., "--id", help="Unique skill identifier"),
    name: str = typer.Option(..., "--name", help="Human-readable name"),
    version: str = typer.Option(..., "--version", help="Semver version (e.g. 1.0.0)"),
    description: str = typer.Option("", "--description", help="Short description"),
    skillset: Optional[str] = typer.Option(None, "--skillset", help="Skillset to associate"),
    metadata: Optional[str] = typer.Option(None, "--metadata", help="JSON metadata string"),
):
    """Create or update a skill record."""
    payload = {
        "id": id,
        "name": name,
        "version": version,
        "description": description,
        "metadata": json.loads(metadata) if metadata else {},
        "skillset_ids": [skillset] if skillset else [],
    }
    with httpx.Client(base_url=_base_url()) as client:
        resp = client.post("/skills", json=payload, headers=_admin_headers())
        if resp.status_code == 409:
            # Already exists — upsert
            upsert_payload = {
                "name": name,
                "version": version,
                "description": description,
                "metadata": json.loads(metadata) if metadata else {},
            }
            resp = client.put(f"/skills/{id}", json=upsert_payload, headers=_admin_headers())
        _handle_error(resp)
    typer.echo(f"Skill {id!r} v{version} saved.")


@skill_app.command("delete")
def skill_delete(
    id: str = typer.Option(..., "--id"),
    version: Optional[str] = typer.Option(None, "--version", help="Delete specific version only"),
):
    """Delete a skill (all versions, or a specific version)."""
    params = {"version": version} if version else {}
    with httpx.Client(base_url=_base_url()) as client:
        resp = client.delete(f"/skills/{id}", params=params, headers=_admin_headers())
        _handle_error(resp)
    typer.echo(f"Skill {id!r} deleted.")


# ---------------------------------------------------------------------------
# agent commands
# ---------------------------------------------------------------------------

@agent_app.command("add")
def agent_add(
    id: str = typer.Option(..., "--id", help="Unique agent identifier"),
    name: str = typer.Option(..., "--name", help="Human-readable name"),
    skillsets: Optional[str] = typer.Option(None, "--skillsets", help="Comma-separated skillset ids"),
    skills: Optional[str] = typer.Option(None, "--skills", help="Comma-separated explicit skill ids"),
    scope: str = typer.Option("read", "--scope", help="Comma-separated scopes (read, execute)"),
):
    """Register or update an agent."""
    payload = {
        "id": id,
        "name": name,
        "skillsets": skillsets.split(",") if skillsets else [],
        "skills": skills.split(",") if skills else [],
        "scope": [s.strip() for s in scope.split(",")],
    }
    with httpx.Client(base_url=_base_url()) as client:
        resp = client.post("/agents", json=payload, headers=_admin_headers())
        if resp.status_code == 409:
            # Already exists — update
            update_payload = {
                "name": name,
                "skillsets": payload["skillsets"],
                "skills": payload["skills"],
                "scope": payload["scope"],
            }
            resp = client.put(f"/agents/{id}", json=update_payload, headers=_admin_headers())
        _handle_error(resp)
    typer.echo(f"Agent {id!r} registered.")


@agent_app.command("delete")
def agent_delete(
    id: str = typer.Option(..., "--id"),
):
    """Remove an agent."""
    with httpx.Client(base_url=_base_url()) as client:
        resp = client.delete(f"/agents/{id}", headers=_admin_headers())
        _handle_error(resp)
    typer.echo(f"Agent {id!r} deleted.")


# ---------------------------------------------------------------------------
# token commands
# ---------------------------------------------------------------------------

@token_app.command("issue")
def token_issue(
    agent_id: str = typer.Option(..., "--agent-id"),
    expires_in: int = typer.Option(3600, "--expires-in", help="Token lifetime in seconds"),
):
    """Issue a signed JWT for an agent and print it to stdout."""
    with httpx.Client(base_url=_base_url()) as client:
        resp = client.post(
            "/token",
            json={"agent_id": agent_id, "expires_in": expires_in},
            headers=_admin_headers(),
        )
        _handle_error(resp)
    typer.echo(resp.json()["access_token"])


# ---------------------------------------------------------------------------
# catalog commands
# ---------------------------------------------------------------------------

@catalog_app.command("import")
def catalog_import(
    file: Path = typer.Option(..., "--file", "-f", help="JSON or YAML catalog file"),
    upsert: bool = typer.Option(False, "--upsert", help="Update existing records instead of failing"),
):
    """
    Bulk-import skillsets, skills, and agents from a JSON/YAML file.

    File format:
      skillsets: [{id, name, description}]
      skills:    [{id, name, version, description, metadata, skillset_ids}]
      agents:    [{id, name, skillsets, skills, scope}]
    """
    raw = file.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) if file.suffix in (".yaml", ".yml") else json.loads(raw)

    imported = {"skillsets": 0, "skills": 0, "agents": 0}

    with httpx.Client(base_url=_base_url()) as client:
        for ss in data.get("skillsets", []):
            resp = client.post("/skillsets", json=ss, headers=_admin_headers())
            if resp.status_code == 409 and upsert:
                resp = client.put(f"/skillsets/{ss['id']}", json=ss, headers=_admin_headers())
            _handle_error(resp)
            imported["skillsets"] += 1

        for skill in data.get("skills", []):
            resp = client.post("/skills", json=skill, headers=_admin_headers())
            if resp.status_code == 409 and upsert:
                body = {k: skill[k] for k in ("name", "version", "description", "metadata") if k in skill}
                resp = client.put(f"/skills/{skill['id']}", json=body, headers=_admin_headers())
            _handle_error(resp)
            imported["skills"] += 1

        for agent in data.get("agents", []):
            resp = client.post("/agents", json=agent, headers=_admin_headers())
            if resp.status_code == 409 and upsert:
                resp = client.put(f"/agents/{agent['id']}", json=agent, headers=_admin_headers())
            _handle_error(resp)
            imported["agents"] += 1

    typer.echo(
        f"Import complete: {imported['skillsets']} skillsets, "
        f"{imported['skills']} skills, {imported['agents']} agents."
    )


# ---------------------------------------------------------------------------
# Superadmin rotation (Wave 9.x, item K)
# ---------------------------------------------------------------------------
#
# Per spec §10, rotating the platform superadmin means rotating
# `MCP_SUPERADMIN_PASSWORD_HASH` + restarting the catalog. This
# subcommand centralizes the hash-generation step so operators don't
# have to remember the inline `python -c "..."` snippet, and can
# optionally write the new value directly to a local `.env` file.


@superadmin_app.command("hash-password")
def superadmin_hash_password(
    password: Optional[str] = typer.Option(
        None,
        "--password",
        help=(
            "New superadmin password. When omitted, prompts securely "
            "(recommended — avoids putting the plaintext in shell history)."
        ),
    ),
):
    """Generate a bcrypt hash suitable for MCP_SUPERADMIN_PASSWORD_HASH.

    Prints the hash to stdout for the operator to copy into their
    secrets manager / Kubernetes Secret / .env. Use `--rotate-env` on
    `superadmin rotate` to edit a local .env in one step.
    """
    from mcp_server.pwhash import hash_password as _hash

    if password is None:
        password = typer.prompt(
            "New superadmin password", hide_input=True, confirmation_prompt=True
        )
    hashed = _hash(password)
    typer.echo(hashed)


@superadmin_app.command("rotate")
def superadmin_rotate(
    password: Optional[str] = typer.Option(
        None,
        "--password",
        help="New password. Prompts securely when omitted.",
    ),
    env_file: Path = typer.Option(
        Path(".env"),
        "--env-file",
        help="Path to the .env file to rewrite in place.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print the new hash + the diff against the env file "
            "without writing to disk."
        ),
    ),
):
    """Rewrite MCP_SUPERADMIN_PASSWORD_HASH in a local .env file.

    Does NOT signal the running catalog process — you must restart
    it (or send the new hash to your orchestrator's secret surface)
    for the rotation to take effect. The spec calls out the restart
    requirement as intentional friction; see spec §2.3.
    """
    from mcp_server.pwhash import hash_password as _hash

    if password is None:
        password = typer.prompt(
            "New superadmin password", hide_input=True, confirmation_prompt=True
        )
    new_hash = _hash(password)

    if not env_file.exists():
        typer.echo(
            f"env file {env_file} does not exist; creating it.",
            err=True,
        )
        existing_lines: list[str] = []
    else:
        existing_lines = env_file.read_text().splitlines()

    new_lines: list[str] = []
    replaced = False
    for line in existing_lines:
        if line.startswith("MCP_SUPERADMIN_PASSWORD_HASH="):
            new_lines.append(f"MCP_SUPERADMIN_PASSWORD_HASH={new_hash}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"MCP_SUPERADMIN_PASSWORD_HASH={new_hash}")

    if dry_run:
        typer.echo("DRY RUN — no changes written.")
        typer.echo(f"New hash: {new_hash}")
        typer.echo(f"Would {'update' if replaced else 'append'} {env_file}")
        return

    env_file.write_text("\n".join(new_lines) + "\n")
    action = "updated" if replaced else "appended"
    typer.echo(f"{action} MCP_SUPERADMIN_PASSWORD_HASH in {env_file}")
    typer.echo(
        "Restart the catalog process for the new password to take "
        "effect. (Docker: `docker compose restart catalog`. Helm: "
        "rotate the Secret + trigger a rolling restart.)"
    )


@superadmin_app.command("show-email")
def superadmin_show_email():
    """Print the hardcoded superadmin pseudo-email.

    Hardcoded to `superadmin@skillfulmcp.com` (spec §2.3). This
    command exists so ops tooling can look it up without grepping
    the source.
    """
    from mcp_server.users import SUPERADMIN_EMAIL

    typer.echo(SUPERADMIN_EMAIL)
