from pathlib import Path
from typing import Iterable, List

from core.config import settings


class LocalResearchTool:
    def __init__(self, roots: Iterable[str] | None = None):
        self.roots = [Path(root) for root in (roots or [settings.local_memory_path, "."])]

    def search(self, query: str, limit: int = 5) -> List[str]:
        terms = [term.lower() for term in query.split() if len(term) > 3]
        matches: List[tuple[int, str]] = []

        for root in self.roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".py", ".yml", ".yaml"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                score = sum(text.lower().count(term) for term in terms)
                if score:
                    snippet = self._snippet(text, terms)
                    matches.append((score, f"{path}: {snippet}"))

        matches.sort(key=lambda item: item[0], reverse=True)
        return [match for _, match in matches[:limit]]

    @staticmethod
    def _snippet(text: str, terms: List[str], width: int = 420) -> str:
        lowered = text.lower()
        first_index = min((lowered.find(term) for term in terms if term in lowered), default=0)
        start = max(first_index - 80, 0)
        snippet = " ".join(text[start:start + width].split())
        return snippet


research_tool = LocalResearchTool()
