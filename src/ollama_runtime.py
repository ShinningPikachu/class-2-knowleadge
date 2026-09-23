"""Inspect and unload local Ollama models without stopping the server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


class OllamaRuntimeError(RuntimeError):
    """Raised when Ollama model-memory management cannot be completed."""


def model_names_match(running: str, configured: str) -> bool:
    """Treat an omitted tag and the explicit ``latest`` tag as equivalent."""
    if running == configured:
        return True
    if ":" not in configured and running == f"{configured}:latest":
        return True
    if ":" not in running and configured == f"{running}:latest":
        return True
    return False


@dataclass(frozen=True)
class RunningOllamaModel:
    """A model currently loaded by the local Ollama server."""

    name: str
    size_bytes: int = 0
    vram_bytes: int = 0
    expires_at: str = ""


@dataclass
class OllamaUnloadReport:
    """Best-effort results from one model cleanup operation."""

    stopped: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def successful(self) -> bool:
        return not self.failures


class OllamaRuntime:
    """Use Ollama's API to inspect or release model memory."""

    def __init__(self, host: str, client: Any | None = None, timeout: float = 3.0) -> None:
        self.host = host
        if client is not None:
            self._client = client
            return
        try:
            import ollama
        except ImportError as exc:
            raise OllamaRuntimeError("ollama is not installed. Run: pip install -r requirements.txt") from exc
        self._client = ollama.Client(host=host, timeout=timeout)

    def close(self) -> None:
        """Close the short-lived HTTP client used for lifecycle requests."""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def running_models(self) -> list[RunningOllamaModel]:
        """Return models currently resident in RAM or VRAM."""
        try:
            response = self._client.ps()
            raw_models = self._field(response, "models") or []
        except Exception as exc:
            raise OllamaRuntimeError(f"Could not inspect the local Ollama server at {self.host}: {exc}") from exc

        models: list[RunningOllamaModel] = []
        for raw_model in raw_models:
            name = str(self._field(raw_model, "model") or self._field(raw_model, "name") or "").strip()
            if not name:
                continue
            models.append(
                RunningOllamaModel(
                    name=name,
                    size_bytes=self._integer_field(raw_model, "size"),
                    vram_bytes=self._integer_field(raw_model, "size_vram"),
                    expires_at=str(self._field(raw_model, "expires_at") or ""),
                )
            )
        return models

    def unload_models(self, models: Iterable[str]) -> OllamaUnloadReport:
        """Unload named models through Ollama while leaving its server running."""
        names = list(dict.fromkeys(str(model).strip() for model in models if str(model).strip()))
        report = OllamaUnloadReport()
        for name in names:
            try:
                self._client.generate(model=name, prompt="", keep_alive=0)
                report.stopped.append(name)
            except Exception as exc:
                report.failures[name] = str(exc)
        return report

    def unload_if_running(self, configured_models: Iterable[str]) -> OllamaUnloadReport:
        """Unload configured models only when Ollama reports them as resident."""
        configured = list(
            dict.fromkeys(str(model).strip() for model in configured_models if str(model).strip())
        )
        running = self.running_models()
        targets = [
            model.name
            for model in running
            if any(model_names_match(model.name, candidate) for candidate in configured)
        ]
        return self.unload_models(targets)

    def unload_all(self) -> OllamaUnloadReport:
        """Unload every model reported by this Ollama server."""
        return self.unload_models(model.name for model in self.running_models())

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    @classmethod
    def _integer_field(cls, value: Any, name: str) -> int:
        try:
            return int(cls._field(value, name) or 0)
        except (TypeError, ValueError):
            return 0
