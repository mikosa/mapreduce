import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from db import KnowledgeDB


class DualLLMClient:
    """
    Manages two LLM tiers:
      1. `local`  -> llama-server (Mistral) running locally on the laptop.
                     Used for 95%+ of calls (Map phase, targeted chunk extraction,
                     and category-level Reduce when running locally).
      2. `large`  -> Larger external API model with strict per-run budget cap.
                     Used sparingly for Entity Resolution, final Dossier synthesis,
                     or complex dynamic topic synthesis when enabled.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        db: KnowledgeDB,
        force_local_only: bool = False,
        use_large_override: Optional[bool] = None,
        mock_mode: bool = False,
    ):
        self.config = config
        self.db = db
        self.force_local_only = force_local_only
        self.use_large_override = use_large_override
        self.mock_mode = mock_mode

        self.local_cfg = config.get("local_llm", {})
        self.large_cfg = config.get("large_llm", {})

        self.large_calls_made_this_run = 0
        self.max_large_calls = int(self.large_cfg.get("max_calls_per_run", 10))

    def can_use_large_model(self, stage_flag_key: str) -> bool:
        """
        Determines whether a given sparse stage (`use_for_entity_resolution`,
        `use_for_reduce`, `use_for_query_synthesis`) should use the Large API Model.
        """
        if self.force_local_only:
            return False

        large_enabled = bool(self.large_cfg.get("enabled", False))
        if self.use_large_override is True:
            large_enabled = True
        elif self.use_large_override is False:
            return False

        if not large_enabled:
            return False

        if not self.large_cfg.get(stage_flag_key, True):
            return False

        if self.large_calls_made_this_run >= self.max_large_calls:
            print(
                f"  [Budget Guard] Large model call limit ({self.max_large_calls}) reached for this run. Falling back to local Mistral."
            )
            return False

        api_key = self._get_large_api_key()
        if not api_key and not self.mock_mode:
            print(
                "  [Budget Guard] Large model enabled but no API key found. Falling back to local Mistral."
            )
            return False

        return True

    def _get_large_api_key(self) -> str:
        env_name = self.large_cfg.get("api_key_env", "LARGE_LLM_API_KEY")
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        return (self.large_cfg.get("api_key") or "").strip()

    # -------------------------------------------------------------------------
    # Core Chat & Structured JSON Calls
    # -------------------------------------------------------------------------
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Dict[str, Any],
        schema_name: str = "extraction",
        purpose: str = "map_extract",
        prefer_large: bool = False,
    ) -> Dict[str, Any]:
        """
        Calls `llama-server` (or the large model) with grammar-constrained JSON schema
        (`response_format: {"type": "json_schema", ...}`) so Mistral is guaranteed to
        emit valid JSON matching `json_schema`.
        """
        tier = "large" if prefer_large else "local"
        if self.mock_mode:
            res = self._mock_json_response(purpose, user_prompt)
            self.db.log_llm_call(
                tier=f"{tier}-mock",
                model="mock-mistral",
                purpose=purpose,
                prompt_chars=len(system_prompt) + len(user_prompt),
                response_chars=len(json.dumps(res)),
            )
            return res

        cfg = self.large_cfg if tier == "large" else self.local_cfg
        base_url = cfg.get("base_url", "http://localhost:8080/v1").rstrip("/")
        model = cfg.get("model", "mistral")
        api_key = (
            self._get_large_api_key()
            if tier == "large"
            else cfg.get("api_key", "not-needed")
        )
        temperature = float(cfg.get("temperature", 0.1))
        max_tokens = int(cfg.get("max_tokens", 1024))
        timeout = int(cfg.get("timeout_seconds", 120))
        use_json_schema = bool(cfg.get("use_json_schema", True))

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if use_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema,
                },
            }

        try:
            raw_text = self._post_chat_completions(
                base_url=base_url,
                api_key=api_key,
                payload=payload,
                timeout=timeout,
            )
        except Exception as e:
            # Fallback if server only supports response_format: {"type": "json_object"} or plain text
            if use_json_schema:
                payload.pop("response_format", None)
                raw_text = self._post_chat_completions(
                    base_url=base_url,
                    api_key=api_key,
                    payload=payload,
                    timeout=timeout,
                )
            else:
                raise e

        if tier == "large":
            self.large_calls_made_this_run += 1

        self.db.log_llm_call(
            tier=tier,
            model=model,
            purpose=purpose,
            prompt_chars=len(system_prompt) + len(user_prompt),
            response_chars=len(raw_text),
        )

        return self._parse_json_safely(raw_text)

    def chat_text(
        self,
        system_prompt: str,
        user_prompt: str,
        purpose: str = "reduce",
        prefer_large: bool = False,
    ) -> Tuple[str, str]:
        """
        Returns `(response_text, model_used_label)`.
        """
        tier = "large" if prefer_large else "local"
        if self.mock_mode:
            text = self._mock_text_response(purpose, user_prompt)
            model_label = f"{tier}:mock-model"
            if tier == "large":
                self.large_calls_made_this_run += 1
            self.db.log_llm_call(
                tier=f"{tier}-mock",
                model="mock-model",
                purpose=purpose,
                prompt_chars=len(system_prompt) + len(user_prompt),
                response_chars=len(text),
            )
            return text, model_label

        cfg = self.large_cfg if tier == "large" else self.local_cfg
        base_url = cfg.get("base_url", "http://localhost:8080/v1").rstrip("/")
        model = cfg.get("model", "mistral")
        api_key = (
            self._get_large_api_key()
            if tier == "large"
            else cfg.get("api_key", "not-needed")
        )
        temperature = float(cfg.get("temperature", 0.1))
        max_tokens = int(cfg.get("max_tokens", 1500))
        timeout = int(cfg.get("timeout_seconds", 120))

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        raw_text = self._post_chat_completions(
            base_url=base_url,
            api_key=api_key,
            payload=payload,
            timeout=timeout,
        )

        if tier == "large":
            self.large_calls_made_this_run += 1

        self.db.log_llm_call(
            tier=tier,
            model=model,
            purpose=purpose,
            prompt_chars=len(system_prompt) + len(user_prompt),
            response_chars=len(raw_text),
        )
        return raw_text.strip(), f"{tier}:{model}"

    def _post_chat_completions(
        self,
        base_url: str,
        api_key: str,
        payload: Dict[str, Any],
        timeout: int,
    ) -> str:
        url = f"{base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8")
                parsed = json.loads(resp_body)
                return parsed["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LLM HTTP {e.code} from {url}: {err_body[:400]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not connect to LLM server at {url} ({e.reason}). "
                f"Make sure your llama-server is running on {base_url} or pass --mock-llm to test offline."
            ) from e

    @staticmethod
    def _parse_json_safely(raw_text: str) -> Dict[str, Any]:
        cleaned = raw_text.strip()
        # Strip markdown fences if present
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Find outermost JSON object
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(cleaned[start : end + 1])
            raise

    # -------------------------------------------------------------------------
    # Deterministic Heuristic Mock Mode (For testing pipeline without server)
    # -------------------------------------------------------------------------
    def _mock_json_response(self, purpose: str, user_prompt: str) -> Dict[str, Any]:
        if purpose == "entity_resolution":
            # Group obvious aliases in mock mode
            names_match = re.findall(r'- "([^"]+)"', user_prompt)
            mapping: Dict[str, str] = {}
            for name in names_match:
                low = name.lower()
                if "checkout" in low or "chkout" in low:
                    mapping[name] = "checkout-service"
                elif "payment" in low or "pay-gw" in low:
                    mapping[name] = "payment-gateway"
                elif "fraud" in low:
                    mapping[name] = "fraud-engine"
                elif "inventory" in low or "inv-svc" in low:
                    mapping[name] = "inventory-service"
                elif "order" in low:
                    mapping[name] = "order-orchestrator"
                else:
                    mapping[name] = name
            return {
                "aliases": [
                    {"raw_name": k, "canonical_app": v} for k, v in mapping.items()
                ]
            }

        # Extract context header fields
        app_match = re.search(r"Inferred Application:\s*(.+)", user_prompt)
        primary_app = app_match.group(1).strip() if app_match else "unknown-app"

        content_match = re.search(
            r"\[CONTENT\]\s*(.*?)\s*\[/CONTENT\]", user_prompt, re.DOTALL
        )
        content = content_match.group(1).strip() if content_match else user_prompt

        facts: List[Dict[str, Any]] = []
        incomplete_notes: List[str] = []

        for line in content.splitlines():
            s = line.strip(" -*\t")
            if not s or s.startswith("#"):
                continue
            low = s.lower()

            if any(tok in low for tok in ("todo", "tbd", "wip", "coming soon", "fill in")):
                incomplete_notes.append(f"Incomplete section note: {s}")
                continue

            # Detect upstream / downstream mentions
            if any(
                w in low
                for w in ("calls ", "sends to ", "publishes to ", "pushes to ", "downstream", "timeout calling", "503 from ")
            ):
                rel = self._guess_related_app(s, primary_app)
                mech = self._guess_mechanism(s)
                facts.append(
                    {
                        "category": "downstream",
                        "related_app": rel,
                        "mechanism": mech,
                        "detail": s,
                    }
                )
            elif any(
                w in low
                for w in ("called by ", "receives from ", "consumes from ", "upstream", "triggered by ", "webhook from ")
            ):
                rel = self._guess_related_app(s, primary_app)
                mech = self._guess_mechanism(s)
                facts.append(
                    {
                        "category": "upstream",
                        "related_app": rel,
                        "mechanism": mech,
                        "detail": s,
                    }
                )
            elif any(
                w in low
                for w in (
                    "postgres",
                    "redis",
                    "kafka",
                    "rabbitmq",
                    "kubernetes",
                    "eks",
                    "aws",
                    "grpc",
                    "rest",
                    "port ",
                    "database",
                    "table ",
                    "topic ",
                    "queue",
                    "cache",
                )
            ):
                facts.append(
                    {
                        "category": "architecture",
                        "related_app": None,
                        "mechanism": self._guess_mechanism(s),
                        "detail": s,
                    }
                )
            elif any(
                w in low
                for w in ("error", "incident", "outage", "fail", "deadlock", "oom", "timeout", "retry", "dlq")
            ):
                facts.append(
                    {
                        "category": "failure_mode",
                        "related_app": self._guess_related_app(s, primary_app),
                        "mechanism": self._guess_mechanism(s),
                        "detail": s,
                    }
                )
            else:
                facts.append(
                    {
                        "category": "function",
                        "related_app": None,
                        "mechanism": None,
                        "detail": s,
                    }
                )

        if purpose == "topic_map":
            return {
                "relevant": len(facts) > 0,
                "findings": [f["detail"] for f in facts],
                "missing_info": incomplete_notes,
            }

        return {
            "inferred_primary_app": primary_app,
            "facts": facts,
            "incomplete_or_missing": incomplete_notes,
        }

    def _mock_text_response(self, purpose: str, user_prompt: str) -> str:
        lines = [
            line.strip()
            for line in user_prompt.splitlines()
            if line.strip().startswith("- [")
        ]
        if not lines:
            return "No documented facts found yet in the ingested sources."
        return "\n".join(lines)

    @staticmethod
    def _guess_related_app(text: str, self_app: str) -> Optional[str]:
        groups = [
            ("payment-gateway", ["payment-gateway", "payment gateway", "pay-gw"]),
            ("fraud-engine", ["fraud-engine", "fraud engine"]),
            ("checkout-service", ["checkout-service", "checkout service", "chkout"]),
            ("inventory-service", ["inventory-service", "inventory service", "inv-svc"]),
            ("order-orchestrator", ["order-orchestrator", "order orchestrator"]),
            ("notification-hub", ["notification-hub", "notification hub"]),
        ]
        self_low = self_app.lower()
        tl = text.lower()
        for canonical, aliases in groups:
            if any(a in self_low for a in aliases):
                continue
            for a in aliases:
                if a in tl:
                    return canonical
        return None

    @staticmethod
    def _guess_mechanism(text: str) -> Optional[str]:
        low = text.lower()
        for m in ("kafka", "rabbitmq", "grpc", "rest", "webhook", "postgres", "redis", "http"):
            if m in low:
                return m.upper()
        return None
