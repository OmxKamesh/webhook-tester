"""Small, provider-neutral failure drills against an explicit test adapter contract."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url, allow_remote=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Expected an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Credentials and fragments are not allowed in URLs")
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.lower() == "localhost"
    if not local and not allow_remote:
        raise ValueError("Non-loopback target requires --allow-remote (isolated staging only)")
    if not local and parsed.scheme != "https":
        raise ValueError("Remote staging requires HTTPS")
    return url


def request(url, payload=None, timeout=2, delay_ms=0):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    if delay_ms:
        headers["X-Chaos-Delay-Response-Ms"] = str(delay_ms)
    req = urllib.request.Request(url, data=data, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Readback exceeds 1 MiB limit")
            try:
                body = json.loads(raw) if raw else None
            except (ValueError, UnicodeError):
                raise ValueError("Response is not valid JSON") from None
            return {"status": response.status, "body": body,
                    "timed_out": False, "elapsed_ms": round((time.monotonic()-started)*1000)}
    except (TimeoutError, socket.timeout):
        return {"status": None, "timed_out": True, "error": "transport timeout"}
    except urllib.error.URLError as exc:
        timeout_error = isinstance(exc.reason, (TimeoutError, socket.timeout))
        return {"status": getattr(exc, "code", None), "timed_out": timeout_error,
                "error": "transport timeout" if timeout_error else "HTTP or transport failure"}

    except (http.client.HTTPException, OSError):
        return {"status": None, "timed_out": False, "error": "HTTP or transport failure"}


def read_url(base, entity):
    parsed = urllib.parse.urlsplit(base)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "entity_id" for key, _ in query):
        raise ValueError("Readback URL must not already include entity_id")
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query + [("entity_id", entity)])))


def safe_response(response, allowed_ids, include_body=False):
    """Reports retain contract evidence, never arbitrary response/exception text."""
    result = {key: response[key] for key in ("status", "timed_out", "elapsed_ms", "error") if key in response}
    body = response.get("body")
    if include_body and isinstance(body, dict):
        ids = body.get("applied_event_ids")
        state = body.get("state")
        count = body.get("effect_count")
        result["body"] = {
            "effect_count": count if type(count) is int else None,
            "state": state if state in ("created", "fulfilled", None) else "<unexpected state>",
            "applied_event_ids": [item if isinstance(item, str) and item in allowed_ids else "<unexpected event id>" for item in ids] if isinstance(ids, list) else None,
        }
    return result


def run_suite(target, readback, *, allow_remote=False, settle_timeout=2.0):
    validate_url(target, allow_remote)
    validate_url(readback, allow_remote)
    if not 0 < settle_timeout <= 60:
        raise ValueError("settle timeout must be greater than 0 and at most 60 seconds")
    run_id = uuid.uuid4().hex
    results = []
    for name in ("duplicate", "reordered", "concurrent", "timeout_after_write"):
        entity = f"chaos-{run_id}-{name}"
        def event(seq=1):
            return {"id": f"{entity}-e{seq}", "entity_id": entity, "sequence": seq,
                    "state": "created" if seq == 1 else "fulfilled", "test": True}
        delivery = []
        before_retry = None
        if name == "duplicate":
            delivery = [request(target, event()) for _ in range(3)]
        elif name == "reordered":
            delivery = [request(target, event(seq)) for seq in (2, 1)]
        elif name == "concurrent":
            with ThreadPoolExecutor(max_workers=8) as pool:
                delivery = list(pool.map(lambda _: request(target, event()), range(16)))
        else:
            delivery.append(request(target, event(), timeout=0.1, delay_ms=400))
            # Establish commit before retry: timeout alone is not proof of a write.
            before_retry = request(read_url(readback, entity))
            delivery.append(request(target, event()))
        expected = {"effect_count": 2 if name == "reordered" else 1,
                    "state": "fulfilled" if name == "reordered" else "created",
                    "applied_event_ids": sorted([event(seq)["id"] for seq in ((1, 2) if name == "reordered" else (1,))])}
        deadline = time.monotonic() + settle_timeout
        observed = None
        while True:
            read = request(read_url(readback, entity))
            observed = read.get("body")
            matches = isinstance(observed, dict) and type(observed.get("effect_count")) is int and all(observed.get(k) == v for k, v in expected.items())
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        checks = {"readback_matches": matches,
                  "delivery_acknowledged": all(isinstance(d.get("status"), int) and d["status"] in range(200, 300) for d in delivery[(1 if name == "timeout_after_write" else 0):])}
        if name == "timeout_after_write":
            committed = before_retry.get("body")
            checks["timeout_observed"] = delivery[0]["timed_out"]
            checks["write_observed_before_retry"] = isinstance(committed, dict) and type(committed.get("effect_count")) is int and committed.get("effect_count") == 1
        results.append({"scenario": name, "entity_id": entity, "passed": all(checks.values()),
                        "checks": checks, "expected": expected,
                        "observed": safe_response(read, expected["applied_event_ids"], True).get("body"),
                        "deliveries": [safe_response(d, []) for d in delivery],
                        "readback": safe_response(read, expected["applied_event_ids"], True),
                        "pre_retry_readback": safe_response(before_retry, expected["applied_event_ids"], True) if before_retry else None})
    return {"schema_version": 1, "run_id": run_id, "passed": all(r["passed"] for r in results),
            "scenarios": results}
