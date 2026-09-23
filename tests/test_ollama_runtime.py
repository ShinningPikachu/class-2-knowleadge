"""Tests for local Ollama model-memory management."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.ollama_runtime import OllamaRuntime


class FakeOllamaClient:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, object]] = []

    def ps(self) -> object:
        return SimpleNamespace(
            models=[
                SimpleNamespace(
                    model="qwen3.5:27b",
                    size=12_345,
                    size_vram=6_789,
                    expires_at="later",
                )
            ]
        )

    def generate(self, **kwargs: object) -> dict[str, object]:
        self.generate_calls.append(kwargs)
        if kwargs["model"] == "broken:model":
            raise RuntimeError("server rejected unload")
        return {"done": True, "done_reason": "unload"}


class OllamaRuntimeTest(unittest.TestCase):
    def test_running_models_are_normalized(self) -> None:
        runtime = OllamaRuntime("http://127.0.0.1:11434", client=FakeOllamaClient())
        models = runtime.running_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "qwen3.5:27b")
        self.assertEqual(models[0].size_bytes, 12_345)
        self.assertEqual(models[0].vram_bytes, 6_789)

    def test_unload_uses_empty_generate_request_and_keep_alive_zero(self) -> None:
        client = FakeOllamaClient()
        runtime = OllamaRuntime("http://127.0.0.1:11434", client=client)
        report = runtime.unload_models(["qwen3.5:27b", "qwen3.5:27b", "broken:model"])

        self.assertEqual(report.stopped, ["qwen3.5:27b"])
        self.assertIn("broken:model", report.failures)
        self.assertEqual(
            client.generate_calls[0],
            {"model": "qwen3.5:27b", "prompt": "", "keep_alive": 0},
        )
        self.assertEqual(len(client.generate_calls), 2)

    def test_automatic_unload_does_not_load_an_absent_model(self) -> None:
        client = FakeOllamaClient()
        runtime = OllamaRuntime("http://127.0.0.1:11434", client=client)
        report = runtime.unload_if_running(["qwen3.5:27b", "nomic-embed-text"])

        self.assertEqual(report.stopped, ["qwen3.5:27b"])
        self.assertEqual([call["model"] for call in client.generate_calls], ["qwen3.5:27b"])


if __name__ == "__main__":
    unittest.main()
