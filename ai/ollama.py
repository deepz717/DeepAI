"""
LocalLLM — single clean interface to Ollama.
=============================================
Replaces ad-hoc `requests.post("http://localhost:11434/...")` calls.

Public API:
    llm = get_llm()                              # module singleton
    llm.is_alive(refresh=False)                  -> bool
    llm.vision_available(refresh=False)          -> bool
    llm.list_models()                            -> list[str]
    llm.generate(prompt, ...)                    -> str    (never None)
    llm.chat(messages, ...)                      -> str    (never None)
    llm.vision(images, prompt, ...)              -> str    (never None)
    llm.structured_output(prompt, ...)           -> dict | None   (JSON only)

Thread-safety:
    * A single requests.Session is shared; urllib3's pool is thread-safe
      for concurrent GET/POST. We do not mutate session state.
    * Availability caches are guarded by a lock.

Health-check cache:
    * is_alive() caches for OLLAMA_ALIVE_TTL seconds.
    * On ConnectionError, the cache is invalidated.
    * Timeouts do NOT invalidate (model may just be slow).

Anti-loop hardening:
    * Every generate/chat/vision call sets repeat_penalty=1.3,
      repeat_last_n=256, and a num_predict cap.
    * On HTTP 500 from Ollama (including "token repeat limit reached"),
      we return "" so callers fall back cleanly.

Thinking suppression (regression fix):
    * Qwen3 and other hybrid-reasoning models emit a separate
      `thinking` field, or inline  thinking… blocks, unless the
      request sets `think: false`.
    * generate() and chat() send `think: false` by default. Callers
      may opt in with allow_thinking=True (currently unused).
    * _strip_thinking() is a defense-in-depth post-filter that
      removes any inline  thinking… block that survives — for
      example on older Ollama builds that ignore the flag, or on
      builds whose tokenizer swallows the opening tag.
    * structured_output() is unaffected: json_mode already disables
      thinking on the server side, and _strip_thinking() is a no-op
      on valid JSON.

Model routing (per-task specialization):
    * text_model   → qwen3:8b             (planning, review, reasoning)
    * chat_model   → phi3:mini            (conversational direct answers)
    * code_model   → qwen2.5-coder:1.5b   (all code generation)
    * vision_model → llava:latest         (image/screen analysis)
    * generate()/chat() accept `model=` to override the default for
      a single call. Callers that don't pass `model=` get text_model.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Union

import requests

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


# ── thinking-block regexes ───────────────────────────────────
# Build the tag names from character codes so that no markdown
# renderer, HTML escape, or editor transform can corrupt them.
_TAG_LT   = chr(60)   # '<'
_TAG_GT   = chr(62)   # '>'
_TAG_SLASH = chr(47)  # '/'
_TAG_OPEN  = _TAG_LT + "think" + _TAG_GT             # <think>
_TAG_CLOSE = _TAG_LT + _TAG_SLASH + "think" + _TAG_GT  # </think>

_THINK_BLOCK_RE = re.compile(
    re.escape(_TAG_OPEN) + r".*?" + re.escape(_TAG_CLOSE),
    re.DOTALL | re.IGNORECASE,
)
_THINK_OPEN_RE  = re.compile(re.escape(_TAG_OPEN), re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(re.escape(_TAG_CLOSE), re.IGNORECASE)

_SENTENCE_TERM_RE = re.compile(r"[.!?]\s*$")
_OLLAMA_SPECIAL_TOKEN_RE = re.compile(r"<\|[A-Za-z0-9_:-]+\|>")


def _strip_thinking(text: str) -> str:
    """
    Remove inline  thinking… blocks that some Ollama/Qwen3
    combinations emit inside `response` or `message.content`.

    Handles four cases:

      1. Complete  thinking… blocks
         → removed entirely.

      2. Orphan opening  thinking with no close (num_predict hit
         mid-thinking-block)
         → everything from the tag onwards is dropped.

      3. Orphan closing  tag with no opener, short prefix, ends
         in sentence punctuation
         → the tag is dropped; the prefix is kept.

      4. Orphan closing  tag, long non-terminating prefix
         → the whole prefix is treated as leaked thinking and
           dropped.

    Idempotent, safe on non-Qwen outputs, never touches code fences.
    """
    if not text:
        return text

    cleaned = _THINK_BLOCK_RE.sub("", text)

    # Case 2: orphan opening tag, no close.
    open_match = _THINK_OPEN_RE.search(cleaned)
    if open_match and not _THINK_CLOSE_RE.search(cleaned):
        return cleaned[:open_match.start()].rstrip()

    # Cases 3 and 4: orphan closing tag.
    close_match = _THINK_CLOSE_RE.search(cleaned)
    if close_match and not _THINK_OPEN_RE.search(cleaned):
        prefix = cleaned[:close_match.start()]
        # Case 4: long non-terminating prefix → leaked thinking.
        if (
            len(prefix.strip()) > 40
            and not _SENTENCE_TERM_RE.search(prefix.rstrip())
        ):
            return cleaned[close_match.end():].lstrip()
        # Case 3: keep prefix, drop the stray close tag.
        cleaned = prefix + cleaned[close_match.end():]

    cleaned = _OLLAMA_SPECIAL_TOKEN_RE.sub("", cleaned)
    return cleaned.strip()


def _extract_first_json_object(text: str) -> Optional[str]:
    """Return the first balanced {...} block, or None."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _image_to_b64(image: Union[str, bytes, Any]) -> str:
    """
    Accept path str, raw bytes, PIL.Image, or already-base64 str.
    """
    if isinstance(image, str):
        if image.startswith(("iVBOR", "/9j/", "data:image")):
            return image.split(",", 1)[-1]
        with open(image, "rb") as fh:
            return base64.b64encode(fh.read()).decode()
    if isinstance(image, (bytes, bytearray)):
        return base64.b64encode(bytes(image)).decode()
    if hasattr(image, "save"):
        buf = BytesIO()
        image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    raise TypeError(f"Unsupported image type: {type(image)}")


class _OllamaUnavailable(Exception):
    """Internal marker: request failed because Ollama was unreachable."""


# ─────────────────────────────────────────────────────────────
# LocalLLM
# ─────────────────────────────────────────────────────────────
class LocalLLM:
    """
    Thin wrapper around the Ollama HTTP API.
    """

    def __init__(
        self,
        host: str = config.OLLAMA_HOST,
        text_model: str = config.TEXT_MODEL,
        chat_model: str = getattr(config, "CHAT_MODEL", config.TEXT_MODEL),
        vision_model: str = config.VISION_MODEL,
        code_model: str = getattr(config, "CODE_MODEL", "qwen2.5-coder:1.5b"),
        keep_alive: str = config.OLLAMA_KEEPALIVE,
        alive_ttl: int = config.OLLAMA_ALIVE_TTL,
    ) -> None:
        self.host = host.rstrip("/")
        self.text_model = text_model
        self.chat_model = chat_model
        self.vision_model = vision_model
        self.code_model = code_model
        self.keep_alive = keep_alive
        self.alive_ttl = alive_ttl

        self._session = requests.Session()

        self._cache_lock = threading.Lock()
        self._alive_value: Optional[bool] = None
        self._alive_ts: float = 0.0
        self._vision_value: Optional[bool] = None
        self._vision_ts: float = 0.0

    # ── availability cache ─────────────────────────────────
    def _invalidate_alive_cache(self) -> None:
        with self._cache_lock:
            self._alive_value = None
            self._alive_ts = 0.0
            self._vision_value = None
            self._vision_ts = 0.0

    def _read_alive_cache(self) -> Optional[bool]:
        with self._cache_lock:
            if self._alive_value is None:
                return None
            if (time.time() - self._alive_ts) > self.alive_ttl:
                return None
            return self._alive_value

    def _write_alive_cache(self, value: bool) -> None:
        with self._cache_lock:
            self._alive_value = value
            self._alive_ts = time.time()

    def _write_vision_cache(self, value: bool) -> None:
        with self._cache_lock:
            self._vision_value = value
            self._vision_ts = time.time()

    def is_alive(self, refresh: bool = False) -> bool:
        """Return True if Ollama responds on /api/tags."""
        if not refresh:
            cached = self._read_alive_cache()
            if cached is not None:
                return cached

        try:
            r = self._session.get(
                f"{self.host}/api/tags",
                timeout=config.OLLAMA_PING_TIMEOUT,
            )
            ok = r.status_code == 200
        except Exception:
            ok = False

        self._write_alive_cache(ok)
        if not ok:
            self._write_vision_cache(False)
        return ok

    def vision_available(self, refresh: bool = False) -> bool:
        """True if the vision model is present in Ollama's model list."""
        if not refresh:
            with self._cache_lock:
                if (
                    self._vision_value is not None
                    and (time.time() - self._vision_ts) <= self.alive_ttl
                ):
                    return self._vision_value

        if not self.is_alive(refresh=refresh):
            self._write_vision_cache(False)
            return False

        try:
            r = self._session.get(
                f"{self.host}/api/tags",
                timeout=config.OLLAMA_PING_TIMEOUT,
            )
            models = r.json().get("models", [])
            base = self.vision_model.split(":")[0]
            ok = any(base in m.get("name", "") for m in models)
        except Exception:
            ok = False

        self._write_vision_cache(ok)
        return ok

    def list_models(self) -> List[str]:
        try:
            r = self._session.get(
                f"{self.host}/api/tags",
                timeout=config.OLLAMA_PING_TIMEOUT,
            )
            return [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            return []

    # ── low-level POST ─────────────────────────────────────
    def _post_generate(
        self,
        payload: Dict[str, Any],
        timeout: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Returns response JSON dict, or None on failure.
        Raises _OllamaUnavailable ONLY on ConnectionError, so the caller
        can invalidate the alive cache. HTTP errors (400/500) return None.
        """
        try:
            r = self._session.post(
                f"{self.host}/api/generate",
                json=payload,
                timeout=timeout,
            )
        except requests.Timeout:
            logger.warning("Ollama request timed out after %ss", timeout)
            return None
        except requests.ConnectionError as e:
            self._invalidate_alive_cache()
            raise _OllamaUnavailable(str(e))
        except Exception as e:
            logger.exception("Ollama request failed: %s", e)
            return None

        if r.status_code != 200:
            # Log the reason once, in full, so users can see the real cause
            logger.warning(
                "Ollama HTTP %s: %s", r.status_code, r.text[:300]
            )
            return None

        try:
            return r.json()
        except Exception as e:
            logger.warning("Ollama returned non-JSON: %s", e)
            return None

    # ── generate ───────────────────────────────────────────
    def generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
        json_mode: bool = False,
        repeat_penalty: float = 1.3,
        repeat_last_n: int = 256,
        model: Optional[str] = None,
        stop: Optional[Sequence[str]] = None,
        allow_thinking: bool = False,
    ) -> str:
        """
        Single-shot text generation.
        ALWAYS returns a string. Returns "" on any failure (empty,
        timeout, HTTP error, unavailable). Never returns None.

        model           — override the text model for this call only.
                          When None, defaults to self.text_model.
        stop            — list of strings; generation halts when any
                          is produced.
        allow_thinking  — False (default) sends `think: false` to
                          Ollama for Qwen3-style hybrid reasoning
                          models. Set True to opt back in.
        """
        if not self.is_alive():
            return "❌ Ollama not running. Start it and try again."

        options: Dict[str, Any] = {
            "temperature": temperature,
            "repeat_penalty": repeat_penalty,
            "repeat_last_n": repeat_last_n,
            "num_predict": max_tokens if max_tokens is not None else 1024,
        }
        if stop:
            options["stop"] = list(stop)

        payload: Dict[str, Any] = {
            "model": model or self.text_model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        # Qwen3 and other hybrid reasoning models emit a thinking
        # block unless explicitly told not to. Structured JSON
        # output already disables thinking server-side, so we only
        # need to send `think: false` on non-JSON calls. Sending it
        # unconditionally is harmless on all other models.
        if not allow_thinking:
            payload["think"] = False

        try:
            data = self._post_generate(
                payload, timeout or config.REQUEST_TIMEOUT
            )
        except _OllamaUnavailable:
            return "❌ Ollama not running. Start it and try again."

        if not isinstance(data, dict):
            return ""

        text = data.get("response") or ""
        if not isinstance(text, str):
            return ""

        # Defense-in-depth: strip any inline  thinking… that
        # survived the think:false flag (older Ollama builds).
        return _strip_thinking(text)

    # ── chat ───────────────────────────────────────────────
    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        temperature: float = 0.7,
        timeout: Optional[int] = None,
        json_mode: bool = False,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[Sequence[str]] = None,
        on_chunk=None,
        cancel_event=None,
        request_handle=None,
    ) -> str:
        """Multi-turn chat. ALWAYS returns a string. Never None."""
        if not self.is_alive():
            return "❌ Ollama not running. Start it and try again."

        payload: Dict[str, Any] = {
            "model": model or self.text_model,
            "messages": list(messages),
            "stream": on_chunk is not None,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "repeat_penalty": 1.3,
                "repeat_last_n": 256,
                "num_predict": max_tokens if max_tokens is not None else 1024,
            },
            # Never leak thinking content on chat either.
            "think": False,
        }
        if stop:
            payload["options"]["stop"] = list(stop)
        if json_mode:
            payload["format"] = "json"

        try:
            request_kwargs = {"json": payload, "timeout": timeout or config.REQUEST_TIMEOUT}
            if on_chunk is not None:
                request_kwargs["stream"] = True
            r = self._session.post(f"{self.host}/api/chat", **request_kwargs)
        except requests.ConnectionError:
            self._invalidate_alive_cache()
            return "❌ Ollama not running. Start it and try again."
        except requests.Timeout:
            logger.warning("Ollama chat timed out")
            return ""
        except Exception as e:
            logger.exception("Ollama chat failed: %s", e)
            return ""

        if r.status_code != 200:
            logger.warning(
                "Ollama chat HTTP %s: %s",
                r.status_code, r.text[:200],
            )
            return ""

        if on_chunk is not None:
            if r.status_code != 200:
                logger.warning("Ollama stream HTTP %s", r.status_code)
                return ""
            if request_handle is not None:
                request_handle.set_response(r)
            parts = []
            raw_parts = []
            delivered = ""
            try:
                for raw in r.iter_lines(decode_unicode=True):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if not raw:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        item = json.loads(raw)
                    except (TypeError, ValueError):
                        logger.debug("Ignoring malformed Ollama stream line")
                        continue
                    message = item.get("message") or {}
                    # Ollama may send a reasoning-only Qwen chunk. Only the
                    # content field is ever eligible for the UI.
                    chunk = message.get("content", "")
                    if not isinstance(chunk, str):
                        continue
                    if not chunk:
                        if item.get("done"):
                            clean = _strip_thinking("".join(raw_parts))
                            if clean.startswith(delivered):
                                delta = clean[len(delivered):]
                            else:
                                on_chunk("\0RESET\0")
                                delta = clean
                                delivered = ""
                            if delta:
                                parts.append(delta)
                                delivered += delta
                                on_chunk(delta)
                            break
                        continue
                    raw_parts.append(chunk)
                    clean = _strip_thinking("".join(raw_parts))
                    # Keep a small suffix buffered so tags split between NDJSON
                    # chunks cannot flash in the chat UI. Thinking-only chunks
                    # are ignored above; inline tagged reasoning is removed here.
                    safe = clean if item.get("done") else clean[:-16]
                    if safe.startswith(delivered):
                        delta = safe[len(delivered):]
                    else:  # defensive recovery if the provider revises a prefix
                        delta = "\0RESET\0" + safe
                    if delta:
                        if delta.startswith("\0RESET\0"):
                            on_chunk("\0RESET\0")
                            delta = delta[len("\0RESET\0"):]
                            delivered = ""
                        if delta:
                            parts.append(delta)
                            delivered += delta
                            on_chunk(delta)
                    if item.get("done"):
                        break
            except Exception:
                logger.warning("Ollama stream interrupted after %d chars", sum(map(len, parts)))
                if cancel_event is not None and cancel_event.is_set():
                    return ""
                partial = "".join(parts)
                if partial:
                    notice = "\n\n[Response interrupted by a network error.]"
                    on_chunk(notice)
                    return partial + notice
                return ""
            finally:
                try:
                    r.close()
                except Exception:
                    pass
                if request_handle is not None:
                    request_handle.set_response(None)
            return "".join(parts)

        try:
            body = r.json()
        except Exception:
            return ""
        msg = body.get("message") or {}
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = ""
        content = _strip_thinking(content)
        if content:
            return content

        # Ollama's Qwen3 responses can contain a separate `thinking` field
        # while `content` is empty when generation stops at the token limit.
        # Never expose reasoning as a user answer. Detect this case and retry
        # once with a compact final-answer instruction and a larger budget.
        thinking = msg.get("thinking")
        if isinstance(thinking, str) and thinking.strip() and not json_mode:
            retry_payload = dict(payload)
            retry_payload["messages"] = list(messages) + [{
                "role": "system",
                "content": "Give only the concise final answer to the user. "
                           "Do not include analysis or reasoning.",
            }]
            retry_payload["options"] = dict(payload["options"])
            retry_payload["options"]["num_predict"] = max(
                256, payload["options"].get("num_predict", 0))
            try:
                retry = self._session.post(
                    f"{self.host}/api/chat",
                    json=retry_payload,
                    timeout=timeout or config.REQUEST_TIMEOUT,
                )
                if retry.status_code == 200:
                    retry_body = retry.json()
                    retry_msg = retry_body.get("message") or {}
                    retry_content = retry_msg.get("content") or ""
                    if isinstance(retry_content, str):
                        retry_content = _strip_thinking(retry_content)
                        if retry_content:
                            return retry_content
            except requests.Timeout:
                logger.warning("Ollama chat final-answer retry timed out")
            except Exception:
                logger.exception("Ollama chat final-answer retry failed")
        return ""

    # ── vision ─────────────────────────────────────────────
    def vision(
        self,
        images: Union[str, bytes, Any, Sequence[Union[str, bytes, Any]]],
        prompt: str,
        *,
        timeout: Optional[int] = None,
    ) -> str:
        """Send one or more images to the vision model.
        ALWAYS returns a string. Never None."""
        if not self.is_alive():
            return "❌ Ollama not running."
        if not self.vision_available():
            return f"❌ Vision model '{self.vision_model}' not available."

        if isinstance(images, (list, tuple)):
            image_list = list(images)
        else:
            image_list = [images]

        try:
            encoded = [_image_to_b64(img) for img in image_list]
        except Exception as e:
            return f"❌ Image encoding failed: {e}"

        payload = {
            "model": self.vision_model,
            "prompt": prompt,
            "images": encoded,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0.4,
                "repeat_penalty": 1.3,
                "repeat_last_n": 256,
                "num_predict": 1024,
            },
        }
        try:
            data = self._post_generate(
                payload, timeout or config.VISION_TIMEOUT
            )
        except _OllamaUnavailable:
            return "❌ Ollama not running."

        if not isinstance(data, dict):
            return ""
        text = data.get("response") or ""
        if not isinstance(text, str):
            return ""
        return _strip_thinking(text)

    # ── structured output (JSON validation only) ──────────
    def structured_output(
        self,
        prompt: str,
        *,
        schema_hint: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
        max_retries: int = config.STRUCTURED_MAX_RETRIES,
        timeout: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Ask the LLM for JSON. Validate that it parses to a dict.
        Retry on invalid output. Return None on total failure.

        This does NOT validate against a schema — `schema_hint` is only
        passed to the model as guidance. Callers must handle None.

        Thinking is disabled by json_mode + think:false inside
        generate(). _strip_thinking() is a no-op on valid JSON.
        """
        if not self.is_alive():
            return None

        schema_block = ""
        if schema_hint:
            schema_block = (
                "\nRespond with a single JSON object matching this shape:\n"
                f"{json.dumps(schema_hint, indent=2)}\n"
                "Do not include explanations, markdown fences, or prose."
            )

        base_system = system or (
            "You are a precise JSON generator. "
            "You output only valid JSON. Never wrap output in code fences."
        )

        for attempt in range(1, max_retries + 1):
            raw = self.generate(
                prompt + schema_block,
                system=base_system,
                temperature=config.STRUCTURED_TEMPERATURE,
                timeout=timeout,
                json_mode=True,
                repeat_penalty=1.2,
                repeat_last_n=128,
                max_tokens=512,
            )
            if not raw:
                logger.warning(
                    "structured_output: empty response (attempt %s)",
                    attempt,
                )
                continue

            candidate = _strip_code_fence(raw)
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

            block = _extract_first_json_object(candidate)
            if block:
                try:
                    parsed = json.loads(block)
                    if isinstance(parsed, dict):
                        logger.info(
                            "structured_output: recovered via "
                            "brace-extraction (attempt %s)",
                            attempt,
                        )
                        return parsed
                except json.JSONDecodeError:
                    pass

            logger.warning(
                "structured_output: invalid JSON (attempt %s): %r",
                attempt,
                raw[:200],
            )

        logger.error(
            "structured_output: all %s attempts failed.", max_retries
        )
        return None


# ─────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────
_llm_instance: Optional[LocalLLM] = None
_llm_init_lock = threading.Lock()


def get_llm() -> LocalLLM:
    """Return the process-wide LocalLLM singleton."""
    global _llm_instance
    if _llm_instance is None:
        with _llm_init_lock:
            if _llm_instance is None:
                _llm_instance = LocalLLM()
    return _llm_instance
