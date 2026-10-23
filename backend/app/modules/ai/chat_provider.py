"""Bounded, single-attempt Chat Completions against the fixed provider endpoint."""
import json
import time

import httpx

URL = "https://api.deepseek.com/chat/completions"


class ProviderFailure(Exception):
    def __init__(self, code):
        self.code = code


def create_client():
    return httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False)


def stream_chat(secret, model, messages, *, max_tokens=4096):
    deadline = time.monotonic() + 180
    finished = False
    total_bytes = 0
    try:
        with create_client() as client:
            with client.stream("POST", URL, headers={"Authorization": "Bearer " + secret}, json={
                "model": model, "messages": messages, "stream": True,
                "stream_options": {"include_usage": True}, "max_tokens": max_tokens,
                "thinking": {"type": "disabled"},
            }) as response:
                if response.status_code != 200:
                    code = {401: "credential_rejected", 403: "credential_rejected",
                            402: "provider_balance", 429: "provider_rate_limited"}.get(response.status_code, "provider_unavailable")
                    raise ProviderFailure(code)
                # Check every transport chunk, including keep-alives; do not buffer to a fixed size.
                pending = b""
                for chunk in response.iter_bytes():
                    total_bytes += len(chunk)
                    if time.monotonic() > deadline:
                        raise ProviderFailure("provider_timeout")
                    if total_bytes > 2_000_000:
                        raise ProviderFailure("provider_invalid_response")
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        line = line.rstrip(b"\r")
                        if len(line) > 65536:
                            raise ProviderFailure("provider_invalid_response")
                        # Heartbeats also let the worker observe cancellation and renew its lease.
                        if not line.startswith(b"data:"):
                            yield {}
                            continue
                        raw = line[5:].strip()
                        if raw == b"[DONE]":
                            if not finished:
                                raise ProviderFailure("provider_incomplete")
                            return
                        data = json.loads(raw)
                        if not isinstance(data, dict) or "error" in data:
                            raise ProviderFailure("provider_invalid_response")
                        event = {}
                        choices = data.get("choices", [])
                        if not isinstance(choices, list):
                            raise ProviderFailure("provider_invalid_response")
                        if choices:
                            choice = choices[0]
                            content = choice.get("delta", {}).get("content")
                            if content is not None:
                                if not isinstance(content, str):
                                    raise ProviderFailure("provider_invalid_response")
                                if "\x00" in content:
                                    raise ProviderFailure("provider_invalid_response")
                                content.encode("utf-8")
                                event["delta"] = content
                            reason = choice.get("finish_reason")
                            if reason:
                                if reason not in {"stop", "length"}:
                                    raise ProviderFailure("provider_incomplete")
                                finished = True
                                event["finish"] = reason
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            event["usage"] = {key: value for key, value in usage.items()
                                              if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                              and type(value) is int and 0 <= value <= 10_000_000}
                        yield event
                    if len(pending) > 65536:
                        raise ProviderFailure("provider_invalid_response")
        # A transport EOF without [DONE] is not proof of success.
        raise ProviderFailure("provider_incomplete")
    except httpx.TimeoutException:
        raise ProviderFailure("provider_timeout") from None
    except httpx.HTTPError:
        raise ProviderFailure("provider_unreachable") from None
    except (ValueError, UnicodeError, TypeError, AttributeError, IndexError):
        raise ProviderFailure("provider_invalid_response") from None
