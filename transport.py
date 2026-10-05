"""Bounded GitHub transport and explicit outcomes; never retry a mutation."""

from collections.abc import Mapping
import hashlib
from http.client import HTTPException
import ipaddress
import json
import re
from urllib import error, parse, request


MAX_RESPONSE = 2 * 1024 * 1024


class GitHubIssuesBlockError(ValueError):
    def __init__(self, message, *, code="validation", status=None, uncertain=False, current=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.uncertain = uncertain
        self.current = current


def revision(issue):
    """Hash the complete issue snapshot, before optional comment enrichment."""
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
        raise GitHubIssuesBlockError("GitHub returned an invalid issue snapshot.", code="response")
    return hashlib.sha256(json.dumps(issue, sort_keys=True, ensure_ascii=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def identifier(value, name="request_id", *, digest=False):
    pattern = r"[0-9a-f]{64}" if digest else r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise GitHubIssuesBlockError(f"Invalid {name}.")
    return value


def resolve_token(context, config):
    """A configured wallet reference has exclusive priority; no silent fallback."""
    reference = config.get("token_ref", "")
    if not reference:
        return config
    resolver = context.services.get("resolve_secret")
    try:
        if not callable(resolver) or not reference.startswith("secret://"):
            raise ValueError()
        token = resolver(reference)
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError()
    except Exception:
        raise GitHubIssuesBlockError("The GitHub wallet secret is unavailable or invalid; no fallback credential was used.", code="secret") from None
    return {**config, "token": token}


def controls(raw, supported):
    raw = raw if isinstance(raw, Mapping) else {}
    actions = raw.get("allowed_actions", "")
    if not isinstance(actions, str):
        raise GitHubIssuesBlockError("Allowed actions must be a comma-separated list.")
    entries = {part.strip() for part in actions.split(",") if part.strip()}
    if entries - set(supported):
        raise GitHubIssuesBlockError("The allowed action list contains an unknown action.")
    mode = raw.get("error_mode", "fail")
    if not isinstance(mode, str) or mode not in {"fail", "result"}:
        raise GitHubIssuesBlockError("Error mode must be fail or result.")
    reference = raw.get("token_ref", "")
    if not isinstance(reference, str) or len(reference) > 512 or any(ord(c) < 33 for c in reference):
        raise GitHubIssuesBlockError("Invalid wallet reference.")
    require = raw.get("require_revision", False)
    if not isinstance(require, bool):
        raise GitHubIssuesBlockError("Require revision must be a boolean.")
    return {"allowed_actions": ",".join(sorted(entries)), "error_mode": mode,
            "token_ref": reference, "require_revision": require}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    try:
        parts = parse.urlsplit(url)
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
            raise ValueError()
        parts.port
        try:
            loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        invalid = (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or (parts.scheme == "http" and not loopback))
    except (ValueError, TypeError):
        invalid = True
    if invalid:
        raise GitHubIssuesBlockError("Use an HTTPS GitHub API base URL (HTTP is permitted only for loopback tests).")


def send(config, url, *, method, payload, api_version):
    validate_url(config["api_base_url"])
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if body and len(body) > 262144:
        raise GitHubIssuesBlockError("GitHub request exceeds 256 KiB.")
    token = config["token"]
    headers = {"Accept": "application/vnd.github+json", "Content-Type": "application/json",
               "User-Agent": "bloxsmith-github-issues-block", "X-GitHub-Api-Version": api_version}
    if token:
        if any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise GitHubIssuesBlockError("Invalid GitHub credential.", code="secret")
        headers["Authorization"] = f"Bearer {token}"
    mutation = method not in {"GET", "HEAD"}
    status = None
    try:
        # Do not forward credentials to a redirect target or inherit a developer's proxy.
        opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
        with opener.open(request.Request(url, data=body, method=method, headers=headers),
                         timeout=config["timeout_sec"]) as response:
            raw = response.read(MAX_RESPONSE + 1)
            status = int(response.status)
            response_headers = response.headers
        if len(raw) > MAX_RESPONSE:
            raise GitHubIssuesBlockError("GitHub response exceeds 2 MiB; no request was retried.",
                                        code="response", status=status, uncertain=mutation)
        try:
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if raw.strip() else {}
            if token:
                data = json.loads(json.dumps(data, ensure_ascii=True).replace(token, "[REDACTED]"))
        except (UnicodeError, ValueError, RecursionError):
            raise GitHubIssuesBlockError("GitHub returned invalid JSON; no request was retried.",
                                        code="response", status=status, uncertain=mutation) from None
    except error.HTTPError as exc:
        with exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
        if token:
            detail = detail.replace(token, "[REDACTED]")
        raise GitHubIssuesBlockError(f"GitHub HTTP {exc.code}: {detail[:800]}", code="http",
                                    status=exc.code, uncertain=mutation and (exc.code >= 500 or exc.code == 408)) from None
    except (error.URLError, OSError, HTTPException):
        raise GitHubIssuesBlockError("GitHub network failure; no request was retried. Check remote state before retrying a write.",
                                    code="network", uncertain=mutation, status=status) from None
    def safe_header(name):
        value = response_headers.get(name, "")[:512]
        return value.replace(token, "[REDACTED]") if token else value
    return {"data": data, "http": {"status": status,
        "rate_limit_remaining": safe_header("X-RateLimit-Remaining"),
        "rate_limit_reset": safe_header("X-RateLimit-Reset"),
        "request_id": safe_header("X-GitHub-Request-Id"),
        "has_next_page": bool(re.search(r'rel="next"', response_headers.get("Link", "")))}}
