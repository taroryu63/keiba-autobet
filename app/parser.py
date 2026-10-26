"""予想テキストの読み取り。

新潟
１R　★ 3.7.10
２R　◎ 4 ★ 5.7.9
11R 15:45 ◎ 14 ★ 8.10.12   ← 発走時刻は書いても書かなくてもよい

開催場の行のあとに、そのレースの行が続く形式。全角数字・全角スペースも可。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from .betting import JRA_VENUES, MAX_HORSE, NAR_VENUES

NUM_SEP = r"[\s.,、・･/]+"
RACE_RE = re.compile(r"^(\d{1,2})\s*[RＲr](?:\s|$|(?=[◎★]))(.*)$")
TIME_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
MARK_RE = re.compile(r"([◎★])\s*([\d\s.,、・･/]*)")


@dataclass
class ParsedRace:
    org: str
    venue: str
    race_no: int
    honmei: int | None
    stars: list[int]
    post_time: str = ""
    line: int = 0


@dataclass
class ParseResult:
    races: list[ParsedRace] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"races": [asdict(r) for r in self.races], "errors": self.errors}


def _venue(line: str) -> tuple[str, str] | None:
    s = re.sub(r"(競馬場?|開催)$", "", line.strip())
    for v in JRA_VENUES:
        if s == v:
            return "jra", v
    for v in NAR_VENUES:
        if s == v:
            return "nar", v
    return None


def _nums(s: str) -> list[int]:
    return [int(x) for x in re.split(NUM_SEP, s.strip()) if x]


def parse(text: str) -> ParseResult:
    out = ParseResult()
    cur: tuple[str, str] | None = None
    seen: set[tuple[str, int]] = set()
    for i, raw in enumerate(text.splitlines(), 1):
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line:
            continue
        v = _venue(line)
        if v:
            cur = v
            continue
        m = RACE_RE.match(line)
        if not m:
            out.errors.append(f"{i}行目を読めません: {raw.strip()}")
            continue
        if not cur:
            out.errors.append(f"{i}行目: 開催場が書かれていません（例: 新潟）")
            continue
        race_no, rest = int(m.group(1)), m.group(2)
        if not 1 <= race_no <= 12:
            out.errors.append(f"{i}行目: レース番号が不正です")
            continue
        post_time = ""
        tm = TIME_RE.search(rest)
        if tm:
            h, mi = int(tm.group(1)), int(tm.group(2))
            if h < 24 and mi < 60:
                post_time = f"{h:02d}:{mi:02d}"
            rest = rest[:tm.start()] + rest[tm.end():]
        marks = {"◎": [], "★": []}
        for mark, nums in MARK_RE.findall(rest):
            marks[mark] += _nums(nums)
        leftover = MARK_RE.sub("", rest).strip()
        if leftover:
            out.errors.append(f"{i}行目: 読めない部分があります「{leftover}」")
            continue
        bad = [n for n in marks["◎"] + marks["★"] if not 1 <= n <= MAX_HORSE]
        if bad:
            out.errors.append(f"{i}行目: 馬番は1〜{MAX_HORSE}です（{bad}）")
            continue
        if len(marks["◎"]) > 1:
            out.errors.append(f"{i}行目: ◎は1頭だけにしてください")
            continue
        if not marks["◎"] and not marks["★"]:
            out.errors.append(f"{i}行目: ◎も★もありません")
            continue
        key = (cur[1], race_no)
        if key in seen:
            out.errors.append(f"{i}行目: {cur[1]}{race_no}Rが2回書かれています")
            continue
        seen.add(key)
        honmei = marks["◎"][0] if marks["◎"] else None
        stars = sorted(set(marks["★"]) - {honmei})
        out.races.append(ParsedRace(cur[0], cur[1], race_no, honmei, stars, post_time, i))
    return out
