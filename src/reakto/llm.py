"""Chat client for a model server on this machine only (Ollama, llama.cpp, LM Studio).

Adapted from sorto: the URL must resolve to loopback, proxy variables are
ignored and redirects are not followed, so mail content can never leave the
machine through this client.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx


class LLMError(RuntimeError):
    pass


class NotLocalError(ValueError):
    """The configured LLM endpoint is not on this machine."""


def ensure_local_url(url: str) -> str:
    """Refuse any endpoint that does not resolve only to loopback addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NotLocalError(f"LLM URL must be http(s): {url!r}")
    host = parsed.hostname
    if not host:
        raise NotLocalError(f"LLM URL has no host: {url!r}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise NotLocalError(f"cannot resolve LLM host {host!r}: {e}") from e
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise NotLocalError(f"LLM host {host!r} did not resolve")
    for addr in addrs:
        if not ipaddress.ip_address(addr.split("%", 1)[0]).is_loopback:
            raise NotLocalError(
                f"LLM host {host!r} resolves to {addr}, which is not this machine. "
                "reakto only talks to a local model (127.0.0.1 / ::1 / localhost)."
            )
    return url


def extract_json_object(text: str) -> dict:
    if not text:
        raise ValueError("empty LLM response")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(s[start : end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("LLM response is not a JSON object")


@dataclass
class Completion:
    content: str
    reasoning: str
    seconds: float
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LocalLLM:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        temperature: float = 0.3,
        top_p: float = 0.95,
        timeout_sec: float = 1800.0,
        max_retries: int = 1,
    ):
        self.base_url = ensure_local_url(base_url.rstrip("/"))
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.timeout_sec = timeout_sec
        self.max_retries = max(0, int(max_retries))
        self._raw: bool | None = None
        self._num_ctx: int | None = None

    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        # trust_env=False: never route through HTTP(S)_PROXY to another host.
        return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)

    def _native_url(self, path: str) -> str:
        p = urlparse(self.base_url)
        return urlunparse((p.scheme, p.netloc, path, "", "", ""))

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_sec, connect=5.0)
        with self._client(timeout) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": "Bearer local", "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code == 400 and "response_format" in body:
            body = {k: v for k, v in body.items() if k != "response_format"}
            return self._post(body)
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        think: bool = False,
        max_tokens: int = 1200,
        json_mode: bool = True,
        temperature: float | None = None,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "top_p": self.top_p,
            "max_tokens": max_tokens,
            # Qwen 3.x thinks by default; "none" turns it off on Ollama's /v1.
            "reasoning_effort": "high" if think else "none",
        }
        if json_mode and not think:
            # A JSON grammar would also constrain the thinking, so only without it.
            body["response_format"] = {"type": "json_object"}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            try:
                data = self._post(body)
                msg = data["choices"][0]["message"]
                usage = data.get("usage") or {}
                content = str(msg.get("content") or "")
                reasoning = str(msg.get("reasoning") or msg.get("reasoning_content") or "")
                if not content.strip() and reasoning:
                    raise LLMError("the model used its whole token budget thinking")
                return Completion(
                    content=content,
                    reasoning=reasoning,
                    seconds=time.monotonic() - t0,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                )
            except (httpx.HTTPError, LLMError, KeyError, IndexError, TypeError, ValueError) as e:
                last = e
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 2**attempt))
        raise LLMError(str(last) if last else "LLM request failed")

    # -- Ollama raw ChatML path ------------------------------------------------
    # Qwen 3.6 is a hybrid (recurrent) model: llama.cpp cannot reuse a cached
    # prefix, only restore a checkpoint taken at the END of an earlier prompt.
    # Rendering ChatML ourselves lets a warm-up prompt be an exact prefix of every
    # real prompt (system prompt + the fixed start of the user message), so its
    # checkpoint saves re-reading the ~1.5k-token system prompt for each mail.

    def uses_raw_chatml(self) -> bool:
        if self._raw is None:
            self._raw = False
            if "qwen" in self.model.lower():
                try:
                    with self._client(httpx.Timeout(8.0, connect=3.0)) as client:
                        r = client.post(self._native_url("/api/show"), json={"model": self.model})
                    self._raw = r.status_code == 200
                except httpx.HTTPError:
                    self._raw = False
        return self._raw

    @staticmethod
    def chatml(system: str, user: str, *, think: bool) -> str:
        head = f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
        return head + ("<think>\n" if think else "<think>\n\n</think>\n\n")

    @staticmethod
    def chatml_prefix(system: str, user_prefix: str) -> str:
        return f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user_prefix}"

    def _generate(self, prompt: str, *, max_tokens: int, json_mode: bool, temperature: float | None) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "raw": True,
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "top_p": self.top_p,
                "num_predict": max_tokens,
            },
        }
        if json_mode:
            body["format"] = "json"
        timeout = httpx.Timeout(self.timeout_sec, connect=5.0)
        with self._client(timeout) as client:
            resp = client.post(self._native_url("/api/generate"), json=body)
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def context_window(self) -> int | None:
        """num_ctx baked into an Ollama model tag (e.g. 16384 for qwen3.5:9b-16k)."""
        if self._num_ctx is None:
            self._num_ctx = 0
            try:
                with self._client(httpx.Timeout(8.0, connect=3.0)) as client:
                    r = client.post(self._native_url("/api/show"), json={"model": self.model})
                if r.status_code == 200:
                    for line in str(r.json().get("parameters") or "").splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[0] == "num_ctx":
                            self._num_ctx = int(parts[1])
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        return self._num_ctx or None

    def warm_prefix(self, system: str, user_prefix: str) -> float:
        """Process the shared prompt prefix once so later mails restore it from a checkpoint."""
        t0 = time.monotonic()
        self._generate(self.chatml_prefix(system, user_prefix), max_tokens=1, json_mode=False, temperature=None)
        return time.monotonic() - t0

    def complete_chat(
        self, system: str, user: str, *, think: bool = False, max_tokens: int = 1200,
        temperature: float | None = None,
    ) -> Completion:
        """system + one user turn; raw ChatML on Ollama/Qwen, /v1 chat otherwise."""
        if not self.uses_raw_chatml():
            return self.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                think=think, max_tokens=max_tokens, temperature=temperature,
            )
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            try:
                # No JSON grammar while thinking: it would constrain the thoughts too.
                data = self._generate(self.chatml(system, user, think=think), max_tokens=max_tokens,
                                      json_mode=not think, temperature=temperature)
                text = str(data.get("response") or "")
                reasoning = ""
                if think:
                    if "</think>" not in text:
                        raise LLMError("the model used its whole token budget thinking")
                    reasoning, text = text.split("</think>", 1)
                return Completion(
                    content=text.strip(), reasoning=reasoning.strip(), seconds=time.monotonic() - t0,
                    prompt_tokens=int(data.get("prompt_eval_count") or 0),
                    completion_tokens=int(data.get("eval_count") or 0),
                )
            except (httpx.HTTPError, LLMError, KeyError, TypeError, ValueError) as e:
                last = e
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 2**attempt))
        raise LLMError(str(last) if last else "LLM request failed")

    def list_models(self) -> list[str]:
        timeout = httpx.Timeout(8.0, connect=3.0)
        with self._client(timeout) as client:
            r = client.get(self._native_url("/api/tags"))
            if r.status_code < 400:
                return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            r = client.get(f"{self.base_url}/models")
        if r.status_code >= 400:
            raise LLMError(f"models HTTP {r.status_code}")
        return [str(i["id"]) for i in r.json().get("data") or [] if i.get("id")]

    def health(self) -> tuple[bool, str]:
        try:
            models = self.list_models()
        except Exception as e:  # noqa: BLE001 - any failure means "not usable"
            return False, f"{self.base_url}: {e}"
        if self.model not in models:
            return False, f"{self.base_url}: model {self.model!r} not available ({len(models)} models)"
        return True, f"{self.base_url}: {self.model} ok"


def probe_url(candidates: list[str], model: str) -> str | None:
    """First candidate server (all loopback) that has *model*."""
    for url in candidates:
        try:
            ok, _ = LocalLLM(base_url=url, model=model, max_retries=0).health()
        except NotLocalError:
            continue
        if ok:
            return url
    return None
