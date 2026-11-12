"""Reserved integration contracts. No adapter in this build performs network I/O."""
from typing import Protocol

class TrendProvider(Protocol):
    def discover(self, region: str, category: str) -> list: ...

class AssetProvider(Protocol):
    def list_owned(self) -> list: ...

class GenerationProvider(Protocol):
    def submit(self, plan: dict) -> dict: ...
    def status(self, remote_id: str) -> dict: ...

class DisabledGenerationProvider:
    def submit(self, plan):
        raise RuntimeError('Live generation is disabled. Configure and audit a real adapter before use.')
    def status(self, remote_id):
        raise RuntimeError('No external generation job exists in simulation mode.')
