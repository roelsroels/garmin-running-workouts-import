import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "none"
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key_env: str = "RUNNING_PLANNER_LLM_API_KEY"

    @property
    def enabled(self):
        return self.provider == "openai-compatible" and bool(self.model)


class OpenAICompatibleAdvisor:
    def __init__(self, config, api_key, timeout=30):
        if not config.enabled:
            raise ValueError("The LLM advisor is not configured")
        if not api_key:
            raise ValueError("An LLM API key is required")
        self.config = config
        self.api_key = api_key
        self.timeout = timeout

    def explain(self, assessment):
        prompt = (
            "Explain the following deterministic running-plan assessment in concise plain language. "
            "Do not diagnose medical conditions, invent measurements, or change the proposed workouts. "
            "State the evidence, uncertainty, and why the deterministic engine recommends changing or retaining "
            "the schedule.\n\n" + json.dumps(assessment, indent=2)
        )
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You explain an evidence-informed running planner's already-validated output.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection failed: {exc.reason}") from exc

        try:
            return result["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("The LLM endpoint returned an unsupported response") from exc
