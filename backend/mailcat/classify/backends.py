"""Model backends behind one small interface.

`Classifier.classify(inputs, choices)` returns, per input, a probability per category name plus
two flags. Swapping Laya for an Ollama model (or anything else) is a config change:
MAILCAT_CLASSIFIER=laya | laya_onnx | ollama | rules_only.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from mailcat.config import get_config

log = logging.getLogger(__name__)


@dataclass
class ModelInput:
    sender: str
    subject: str
    body: str
    max_chars: int = 3000


@dataclass
class ModelOutput:
    probabilities: dict[str, float]
    phishing: float | None = None
    needs_action: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def top(self) -> tuple[str | None, float]:
        if not self.probabilities:
            return None, 0.0
        name = max(self.probabilities, key=lambda k: self.probabilities[k])
        return name, float(self.probabilities[name])


class Classifier(Protocol):
    version: str

    def classify(self, inputs: list[ModelInput], choices: dict[str, str]) -> list[ModelOutput]: ...

    def close(self) -> None: ...


class RulesOnlyClassifier:
    """No model: everything the rules do not decide goes to Undecided."""

    version = "rules-only"

    def classify(self, inputs: list[ModelInput], choices: dict[str, str]) -> list[ModelOutput]:
        return [ModelOutput(probabilities={}) for _ in inputs]

    def close(self) -> None:
        pass


def _laya_questions(choices: dict[str, str]) -> dict:
    return {
        "category": {
            "type": "choice",
            "instructions": "Which folder should this email in my personal mailbox be filed in?",
            "criteria": choices,
        },
        "is_phishing": {
            "type": "noul",
            "instructions": "Is this email a phishing or scam attempt to steal money, credentials, or "
                            "personal data?",
            "criteria": {"true": "phishing, scam, or fraud", "false": "a legitimate email"},
        },
        "needs_action": {
            "type": "noul",
            "instructions": "Does this email ask the recipient to do something, reply, pay, or meet a "
                            "deadline?",
        },
    }


class LayaClassifier:
    """Laya decision model (https://huggingface.co/convaiinnovations/laya)."""

    def __init__(self, onnx: bool = False, batch_size: int = 8) -> None:
        cfg = get_config()
        import os

        os.environ.setdefault("HF_HOME", str(cfg.models_path))
        import laya  # heavy import, only in the worker

        self._laya = laya
        started = time.monotonic()
        if onnx:
            from laya.onnx_agent import ONNXAgent

            self.agent = ONNXAgent(cfg.laya_model, onnx_path=str(cfg.laya_onnx_path),
                                   subfolder=cfg.laya_subfolder or None, revision=cfg.laya_revision)
        else:
            self.agent = laya.load(cfg.laya_model, device="cpu", subfolder=cfg.laya_subfolder or None,
                                   revision=cfg.laya_revision)
        self.batch_size = batch_size
        self.version = f"laya:{cfg.laya_subfolder or 'root'}{':onnx' if onnx else ''}" \
                       f"@{cfg.laya_revision or 'latest'}"
        log.info("loaded %s in %.1fs", self.version, time.monotonic() - started)

    def _state(self, item: ModelInput) -> dict:
        # email_state strips quoted replies and signatures, then cuts to max_chars.
        return self._laya.email_state(item.subject, item.body, sender=item.sender,
                                      max_chars=item.max_chars)

    def classify(self, inputs: list[ModelInput], choices: dict[str, str]) -> list[ModelOutput]:
        if not inputs:
            return []
        questions = _laya_questions(choices)
        results = self.agent.predict_batch([self._state(i) for i in inputs], questions,
                                           batch_size=self.batch_size)
        outputs: list[ModelOutput] = []
        for result in results:
            answers = result.get("answers", result)
            cat = answers.get("category", {})
            probs = {str(k): float(v) for k, v in (cat.get("probabilities") or {}).items()}
            outputs.append(ModelOutput(
                probabilities=probs,
                phishing=_noul(answers.get("is_phishing")),
                needs_action=_noul(answers.get("needs_action")),
                extra={"answer_confidence": cat.get("answer_confidence")},
            ))
        return outputs

    def close(self) -> None:
        self.agent = None


def _noul(answer: dict | None) -> float | None:
    if not answer:
        return None
    value = answer.get("noul")
    return None if value is None else float(value)


class OllamaClassifier:
    """Fallback: a small local LLM via Ollama, constrained to JSON with an enum of categories."""

    def __init__(self) -> None:
        cfg = get_config()
        self.url = cfg.ollama_url.rstrip("/")
        self.model = cfg.ollama_model
        self.version = f"ollama:{self.model}"
        self.client = httpx.Client(timeout=300)

    def classify(self, inputs: list[ModelInput], choices: dict[str, str]) -> list[ModelOutput]:
        names = list(choices)
        schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": names},
                "confidence": {"type": "number"},
                "phishing": {"type": "boolean"},
                "needs_action": {"type": "boolean"},
            },
            "required": ["category", "confidence", "phishing", "needs_action"],
        }
        legend = "\n".join(f"- {n}: {d}" for n, d in choices.items())
        outputs: list[ModelOutput] = []
        for item in inputs:
            prompt = (
                "Classify this email into exactly one category.\nCategories:\n"
                f"{legend}\n\nFrom: {item.sender}\nSubject: {item.subject}\n\n{item.body[:item.max_chars]}\n\n"
                "Answer with JSON. confidence is your probability (0-1) that the category is right."
            )
            try:
                response = self.client.post(f"{self.url}/api/generate", json={
                    "model": self.model, "prompt": prompt, "format": schema, "stream": False,
                    "options": {"temperature": 0}, "keep_alive": 0,
                })
                response.raise_for_status()
                data = json.loads(response.json()["response"])
                name = data["category"] if data.get("category") in choices else None
                conf = min(max(float(data.get("confidence", 0)), 0.0), 1.0)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("ollama classification failed: %s", exc)
                outputs.append(ModelOutput(probabilities={}))
                continue
            probs = {n: (conf if n == name else (1 - conf) / max(len(names) - 1, 1)) for n in names}
            outputs.append(ModelOutput(
                probabilities=probs,
                phishing=1.0 if data.get("phishing") else 0.0,
                needs_action=1.0 if data.get("needs_action") else 0.0,
            ))
        return outputs

    def close(self) -> None:
        self.client.close()


def build_classifier(name: str | None = None) -> Classifier:
    name = (name or get_config().classifier).lower()
    if name == "laya":
        return LayaClassifier()
    if name == "laya_onnx":
        return LayaClassifier(onnx=True)
    if name == "ollama":
        return OllamaClassifier()
    if name == "rules_only":
        return RulesOnlyClassifier()
    raise ValueError(f"unknown classifier backend {name!r}")

