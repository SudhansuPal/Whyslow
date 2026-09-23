"""Secret redaction for process command lines.

Command lines are untrusted input and frequently carry credentials (API keys,
passwords, tokens, connection strings). Every command line is passed through
`redact_argv` before it is stored or displayed; environment variables are never
read at all.

The rules are deliberately greedy: masking a harmless argument costs nothing,
leaking a secret is the bug that matters.
"""

from __future__ import annotations

import re
from typing import Iterable

MASK = "[REDACTED]"
MAX_CMDLINE_CHARS = 1024

# Name fragments that make a flag or key=value key sensitive. Matched
# case-insensitively against the separator-less key, so "--db-password",
# "DB_PASSWORD" and "dbPassword" all hit "password".
_SENSITIVE_KEY_FRAGMENTS = (
    "pass", "pwd", "secret", "token", "key", "auth", "credential", "cred",
    "session", "cookie", "signature", "sig", "salt", "otp", "private",
    "cert", "dsn", "conn",
)
_SENSITIVE_KEY_RE = re.compile("|".join(_SENSITIVE_KEY_FRAGMENTS), re.IGNORECASE)

# "--flag=value", "--flag", "-flag".
_FLAG_RE = re.compile(r"^(--?[A-Za-z][A-Za-z0-9_.-]*)(?:=(.*))?$", re.DOTALL)

_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.DOTALL)
# scheme://user:password@host  (postgres://, mongodb+srv://, redis://, https://, amqp://, ...)
_URL_USERPASS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]*):([^/\s@]+)@")
# scheme://<token>@host  (e.g. https://ghp_xxx@github.com/...)
_URL_USER_TOKEN_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]{16,})@")
_AUTH_SCHEME_RE = re.compile(r"(?i)(?<![A-Za-z])(bearer|basic|digest|token|apikey)(\s+|%20)[A-Za-z0-9._~+/=-]{6,}")
# key=value / key: value where the key looks sensitive. Value runs to a delimiter.
_KV_RE = re.compile(
    r"""(?ix)
    (?P<key>[A-Za-z0-9_.-]*(?:""" + "|".join(_SENSITIVE_KEY_FRAGMENTS) + r""")[A-Za-z0-9_.-]*)
    (?P<sep>\s*(?:=|:(?!//))\s*)
    (?P<val>"[^"]*"|'[^']*'|[^\s&;,"']+)
    """
)
# Well-known credential shapes, matched anywhere.
_KNOWN_TOKEN_RES = [
    re.compile(p)
    for p in (
        r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b",          # AWS access key id
        r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",                         # GitHub tokens
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bglpat-[A-Za-z0-9_-]{20,}\b",                           # GitLab
        r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",                      # Slack
        r"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{20,}\b",         # Anthropic / OpenAI style
        r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b",        # Stripe
        r"\bAIza[0-9A-Za-z_-]{35}\b",                              # Google API key
        r"\bnpm_[A-Za-z0-9]{36}\b",                                # npm
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT
    )
]
_HEX_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")
_B64_RE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/_=-])")


def _looks_like_secret_blob(s: str) -> bool:
    """High-entropy heuristic for base64/base64url blobs; excludes file paths."""
    if s.startswith("/") or "//" in s:
        return False
    digits = sum(c.isdigit() for c in s)
    return digits >= 2 and any(c.islower() for c in s) and any(c.isupper() for c in s)


def is_sensitive_key(name: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(name.lstrip("-")))


def redact_text(text: str) -> str:
    """Mask secrets inside a single free-form string."""
    text = _PEM_RE.sub(MASK, text)
    text = _URL_USERPASS_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", text)
    text = _URL_USER_TOKEN_RE.sub(lambda m: f"{m.group(1)}{MASK}@", text)
    text = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    for rx in _KNOWN_TOKEN_RES:
        text = rx.sub(MASK, text)
    text = _KV_RE.sub(lambda m: f"{m.group('key')}{m.group('sep')}{MASK}", text)
    text = _HEX_RE.sub(MASK, text)
    text = _B64_RE.sub(lambda m: MASK if _looks_like_secret_blob(m.group(0)) else m.group(0), text)
    return text


def redact_argv(argv: Iterable[str]) -> list[str]:
    """Mask secrets in an argv list, including `--password VALUE` split across args."""
    out: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            mask_next = False
            if not arg.startswith("-"):
                out.append(MASK)
                continue
        m = _FLAG_RE.match(arg)
        if m and is_sensitive_key(m.group(1)):
            if m.group(2) is None:
                out.append(arg)
                mask_next = True  # value is (probably) the next argument
            else:
                out.append(f"{m.group(1)}={MASK}")
            continue
        out.append(redact_text(arg))
    return out


def _quote(arg: str) -> str:
    return f'"{arg}"' if (not arg or any(c.isspace() for c in arg)) else arg


def redact_cmdline(argv: Iterable[str] | None, mode: str) -> str | None:
    """Return the storable/displayable form of a command line, or None.

    mode "name_only" never returns arguments. Output is truncated to
    MAX_CMDLINE_CHARS *after* redaction so truncation can't split a secret
    past the masking rules.
    """
    if mode == "name_only" or not argv:
        return None
    text = " ".join(_quote(a) for a in redact_argv(argv))
    if len(text) > MAX_CMDLINE_CHARS:
        text = text[: MAX_CMDLINE_CHARS - 1] + "…"
    return text
