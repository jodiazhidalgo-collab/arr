from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ParserTrace:
    """Recolector local: una llamada no comparte estado con ninguna otra."""

    steps: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, rule: str, before: Any, after: Any, *, changed_only: bool = False) -> None:
        if changed_only and before == after:
            return
        self.steps.append(
            {
                "rule": str(rule),
                "before": deepcopy(before),
                "after": deepcopy(after),
            }
        )

    def to_list(self) -> List[Dict[str, Any]]:
        return deepcopy(self.steps)
