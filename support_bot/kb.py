"""A small knowledge base: markdown files split into sections by heading, searched with BM25.
No embeddings service needed. A question with no section scoring above the floor never
reaches the model at all: the bot says it is not in the docs and offers a person."""
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List

STOP = set("""a an and are as at be but by can do does for from has have how i if in is it its me my of on or
our so that the their them there this to was we what when where which who why will with you your""".split())


@dataclass(frozen=True)
class Section:
    source: str    # file name, e.g. billing.md
    heading: str   # e.g. Billing > Refunds
    text: str

    @property
    def cite(self) -> str:
        return f"{self.source} > {self.heading.split(' > ')[-1]}"


@dataclass
class Hit:
    section: Section
    score: float


def tokens(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        for suffix in ("ing", "es", "s", "ed"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


def split_markdown(source: str, text: str) -> List[Section]:
    sections, title, current, buf = [], "", "", []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            heading = f"{title} > {current}" if title and current else (current or title or source)
            sections.append(Section(source, heading, body))

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush()
            buf = []
            if len(m.group(1)) == 1:
                title, current = m.group(2).strip(), ""
            else:
                current = m.group(2).strip()
        else:
            buf.append(line)
    flush()
    return sections


class KnowledgeBase:
    def __init__(self, sections: List[Section], k1: float = 1.4, b: float = 0.75):
        self.sections = sections
        self.k1, self.b = k1, b
        self.docs = [Counter(tokens(s.heading + " " + s.text)) for s in sections]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0
        df = Counter(t for d in self.docs for t in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def load(cls, folder: str) -> "KnowledgeBase":
        sections = []
        for path in sorted(Path(folder).glob("*.md")):
            sections += split_markdown(path.name, path.read_text(encoding="utf-8"))
        return cls(sections)

    def search(self, query: str, k: int = 4, floor: float = 1.0) -> List[Hit]:
        q = tokens(query)
        hits = []
        for sec, doc, length in zip(self.sections, self.docs, self.lengths):
            score = 0.0
            for t in q:
                f = doc.get(t, 0)
                if f:
                    score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            if score >= floor:
                hits.append(Hit(sec, round(score, 3)))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]
