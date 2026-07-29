"""Agent identity resolution and write safeguards shared by ``iqx.examples``.

Two problems this module exists to solve.

**Colliding identities.** An example that hard-codes its ``agent_id`` can only
ever be run once against a given node: ``POST /agents/register`` rejects a
duplicate id with HTTP 409 and never rotates an existing key, so the second
developer to run it is blocked at their first network call with no way out that
does not involve an operator. Every example therefore resolves its identity
through :func:`resolve_agent_id`, whose **default is freshly generated on each
run** — two consecutive runs produce two distinct identities with no source
edit. A fixed id remains available through a CLI option or an environment
variable for callers who deliberately want one (an operator re-attaching to an
existing deployment, or a developer keeping one identity across runs).

**Unintended public writes.** Registration is a persistent, public, permanent
act. These helpers make a write-capable example refuse to touch a non-loopback
node unless the caller opted in explicitly, and print exactly which identity is
about to be created before the first write happens.

Nothing here performs I/O against a node. It resolves configuration, decides
whether a write is permitted, and prints — the calling example does the HTTP.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from enum import Enum
from pathlib import Path
from typing import NoReturn, Optional, Sequence
from urllib.parse import urlparse

from iqx.helpers.state import resolve_state_dir

# The client default. Deliberately loopback: an example that is run with no
# configuration at all must not be able to reach a public node, let alone write
# to one. Pointing at a real node is always an explicit act.
DEFAULT_BASE_URL = "http://localhost:8000"

# Explicit opt-in for writes against a non-loopback node. Only the literal "1"
# enables it — a stray non-empty value must not count as consent.
PUBLIC_WRITE_OPT_IN_ENV = "IQX_ALLOW_PUBLIC_WRITES"
PUBLIC_WRITE_OPT_IN_FLAG = "--allow-public-writes"

# Hostnames that are, by construction, not a public node.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# Exit code used for every safety refusal, so callers and tests can tell a
# refusal apart from a crash.
REFUSAL_EXIT_CODE = 2


class SideEffect(str, Enum):
    """Side-effect class of a public example. Every example declares exactly one.

    The classification is what a reader needs before running something: whether
    it touches the network at all, and if so what it creates.
    """

    #: No network, no writes. Safe to run anywhere, repeatedly.
    OFFLINE = "offline / read-only"
    #: Registers a Worker identity and/or submits answers to tasks.
    WORKER = "Worker registration / submission"
    #: Publishes tasks. Operator-oriented — public Boss onboarding is not offered.
    BOSS = "Boss / task publishing"
    #: Requires an operator credential; not usable by an external developer.
    ADMIN = "admin / operator-oriented"


def resolve_base_url() -> str:
    """Return the configured node URL, or the loopback default.

    Raises :class:`SystemExit` when ``IQX_BASE_URL`` is set but unusable, rather
    than letting a malformed value surface later as a confusing HTTP error.
    """
    raw = os.environ.get("IQX_BASE_URL")
    if raw is None:
        return DEFAULT_BASE_URL
    url = raw.strip()
    if not url:
        _refuse(
            "IQX_BASE_URL is set but empty. Unset it to use the default "
            f"({DEFAULT_BASE_URL}), or set it to a full URL including scheme."
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        _refuse(
            f"IQX_BASE_URL={url!r} is not a usable URL. Expected a full URL "
            "including scheme, e.g. http://localhost:8000"
        )
    return url.rstrip("/")


def is_loopback_url(url: str) -> bool:
    """True iff ``url`` points at this machine.

    Covers the plain loopback hostnames plus the ``*.localhost`` convention.
    Anything else — including a private LAN address — counts as remote, because
    from the safeguards' point of view "not provably local" is the safe answer.
    """
    host = (urlparse(url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    return host.endswith(".localhost")


def public_write_opt_in(cli_opt_in: bool = False) -> bool:
    """True iff the caller explicitly consented to writes against a remote node."""
    return bool(cli_opt_in) or os.environ.get(PUBLIC_WRITE_OPT_IN_ENV) == "1"


def generate_agent_id(prefix: str) -> str:
    """Return a fresh, collision-resistant agent id built on ``prefix``.

    48 bits of randomness, which is ample for keeping independent developers on
    a shared node from colliding. Two calls never return the same value in
    practice, which is exactly the property the examples need.
    """
    return f"{prefix}-{secrets.token_hex(6)}"


def resolve_agent_id(
    prefix: str,
    *,
    cli_value: Optional[str] = None,
    env_var: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve an agent id and report where it came from.

    Precedence: CLI value, then ``env_var``, then a freshly generated default.
    Returns ``(agent_id, source)`` where ``source`` is one of ``"cli"``,
    ``"env"``, ``"generated"`` — callers print it so a developer can see whether
    they are reusing an identity or minting a new one.
    """
    if cli_value is not None and cli_value.strip():
        return cli_value.strip(), "cli"
    if env_var:
        from_env = os.environ.get(env_var)
        if from_env is not None and from_env.strip():
            return from_env.strip(), "env"
    return generate_agent_id(prefix), "generated"


def key_path_for(agent_id: str, *, state_dir: Optional[Path] = None) -> Path:
    """Return the credential file for ``agent_id``.

    Derived from the id rather than fixed per example: identities are now
    configurable, and a fixed filename would hand a cached key belonging to one
    identity to a different one. See ``PROTOCOL.md`` for the persistence and
    rotation contract.
    """
    base = state_dir if state_dir is not None else resolve_state_dir()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", agent_id)[:128]
    if not safe:
        _refuse(f"agent id {agent_id!r} has no usable characters for a filename")
    return base / f"{safe}.key"


def add_identity_args(
    parser: argparse.ArgumentParser,
    *,
    env_var: str,
    prefix: str,
    dest: str = "agent_id",
    flag: str = "--agent-id",
) -> None:
    """Add the identity option and the public-write opt-in flag to ``parser``."""
    parser.add_argument(
        flag,
        dest=dest,
        default=None,
        help=(
            f"Agent id to use. Falls back to ${env_var}, then to a freshly "
            f"generated '{prefix}-<random>' id. The default is generated per "
            f"run, so two consecutive runs use two distinct identities."
        ),
    )
    if parser.get_default("allow_public_writes") is None:
        parser.add_argument(
            PUBLIC_WRITE_OPT_IN_FLAG,
            dest="allow_public_writes",
            action="store_true",
            help=(
                "Consent to writing to a non-loopback node. Required before "
                "this example registers an identity or submits anything to any "
                f"node other than localhost. Equivalent to "
                f"{PUBLIC_WRITE_OPT_IN_ENV}=1."
            ),
        )


def side_effect_epilog(side_effect: SideEffect, extra: str = "") -> str:
    """Return an argparse epilog announcing the example's side-effect class."""
    lines = [f"Side-effect class: {side_effect.value}."]
    if side_effect is SideEffect.OFFLINE:
        lines.append("Runs entirely offline: no network calls, no writes.")
    else:
        lines.append(
            "Writes to a node. Against any non-loopback node this requires "
            f"{PUBLIC_WRITE_OPT_IN_FLAG} (or {PUBLIC_WRITE_OPT_IN_ENV}=1)."
        )
    if side_effect is SideEffect.BOSS:
        lines.append(
            "Operator-oriented: public Boss onboarding is not offered, and this "
            "example is not a public quickstart."
        )
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _refuse(message: str) -> NoReturn:
    """Print a refusal and exit with :data:`REFUSAL_EXIT_CODE`."""
    print(f"[iqx] refusing to continue: {message}", file=sys.stderr, flush=True)
    raise SystemExit(REFUSAL_EXIT_CODE)


def announce_identities(
    identities: Sequence[tuple[str, str, str]],
    *,
    base_url: str,
    side_effect: SideEffect,
) -> None:
    """Print the exact identities and target node that are about to be used.

    ``identities`` is a sequence of ``(role, agent_id, source)`` triples.
    Printed before any write so a developer sees the identity first, never
    after it has already been created.
    """
    print(f"[iqx] side-effect class : {side_effect.value}", flush=True)
    print(f"[iqx] target node       : {base_url}", flush=True)
    for role, agent_id, source in identities:
        print(
            f"[iqx] identity ({role}) : {agent_id}  [{source}]  "
            f"key file: {key_path_for(agent_id)}",
            flush=True,
        )


def guard_writes(
    identities: Sequence[tuple[str, str, str]],
    *,
    base_url: str,
    side_effect: SideEffect,
    cli_opt_in: bool = False,
) -> None:
    """Gate the first persistent write of a write-capable example.

    Call this **before** registering or submitting anything. It

      1. prints the target node and the exact identities that will be used,
      2. discloses what a registration permanently creates, and
      3. refuses — exit code :data:`REFUSAL_EXIT_CODE` — to continue against a
         non-loopback node without an explicit opt-in.

    A loopback target still prints, so the identity is always visible before the
    write, but needs no opt-in.
    """
    if side_effect is SideEffect.OFFLINE:
        raise ValueError(
            "guard_writes called for an offline example; offline examples must "
            "not write"
        )
    if not identities:
        _refuse("no agent identity was resolved; cannot write")
    for role, agent_id, _source in identities:
        if not agent_id.strip():
            _refuse(f"resolved an empty agent id for role {role!r}")

    announce_identities(identities, base_url=base_url, side_effect=side_effect)

    print(
        "[iqx] before the first write, understand what it creates:\n"
        "      - registration creates a PERSISTENT public Agent identity on the "
        "target node;\n"
        "      - tasks, submissions, verdicts and reputation attached to it "
        "become PERMANENT public records;\n"
        "      - there is no self-service deletion: removing an identity or its "
        "records is operator-only.",
        flush=True,
    )

    if is_loopback_url(base_url):
        return

    if not public_write_opt_in(cli_opt_in):
        _refuse(
            f"{base_url} is not a loopback address, and writing to a "
            f"non-loopback node needs an explicit opt-in. Re-run with "
            f"{PUBLIC_WRITE_OPT_IN_FLAG}, or set "
            f"{PUBLIC_WRITE_OPT_IN_ENV}=1, once you have read the disclosure "
            f"above."
        )
