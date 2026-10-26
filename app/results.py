"""レース情報の取得（netkeiba）。発走時刻・着順・単勝オッズ・払戻金を読む。

ログイン不要の公開ページを、1ページずつ間隔をあけて読むだけにしている。
ページの作りが変わったときは、このファイルのCSSセレクタを直す。
"""
from __future__ import annotations

import logging
import re
import time
import urllib.request
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HOSTS = {"jra": "https://race.netkeiba.com", "nar": "https://nar.netkeiba.com"}
UA = "Mozilla/5.0 (keiba-autobet; personal use)"
_last_fetch = 0.0
_cache: dict[str, tuple[float, str]] = {}

PAYOUT_ROWS = {"Tansho": "単勝", "Fukusho": "複勝", "Wakuren": "枠連", "Umaren": "馬連",
               "Wide": "ワイド", "Umatan": "馬単", "Fuken": "3連複", "Tan3": "3連単"}
TYPE_SIZE = {"単勝": 1, "複勝": 1, "枠連": 2, "馬連": 2, "ワイド": 2, "馬単": 2, "3連複": 3, "3連単": 3}
ORDERED = {"単勝", "複勝", "馬単", "3連単"}


@dataclass
class RaceResult:
    settled: bool
    ranks: dict[int, int] = field(default_factory=dict)        # 馬番 -> 着順
    odds: dict[int, float] = field(default_factory=dict)       # 馬番 -> 単勝オッズ（最終）
    pay: dict[str, dict[tuple[int, ...], int]] = field(default_factory=dict)  # 券種 -> 組番 -> 100円あたり払戻

    @property
    def winner(self) -> int | None:
        return next((h for h, r in self.ranks.items() if r == 1), None)

    def payout(self, tickets: list[dict]) -> int:
        """馬券の払戻合計（円）。"""
        total = 0
        for t in tickets:
            combo = tuple(t["combo"]) if t["bet_type"] in ORDERED else tuple(sorted(t["combo"]))
            per100 = self.pay.get(t["bet_type"], {}).get(combo, 0)
            total += per100 * t["amount"] // 100
        return total


def fetch(url: str, ttl: float = 60) -> str:
    """ページを取得する。連続アクセスしないよう1秒以上あける。"""
    global _last_fetch
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    wait = 1.0 - (time.time() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        charset = r.headers.get_content_charset()
    _last_fetch = time.time()
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000])
        charset = m.group(1).decode() if m else "euc-jp"
    html = raw.decode(charset, errors="replace")
    _cache[url] = (time.time(), html)
    return html


def parse_race_list(html: str, venues: list[str]) -> dict[tuple[str, int], dict]:
    """開催日のレース一覧から {(開催場, R): {"race_id", "post_time"}} を作る。"""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[tuple[str, int], dict] = {}
    for block in soup.select("dl.RaceList_DataList"):
        title = block.select_one(".RaceList_DataTitle")
        title_text = title.get_text(" ", strip=True) if title else ""
        venue = next((v for v in venues if v in title_text), None)
        if not venue:
            continue
        for item in block.select("li.RaceList_DataItem"):
            a = item.select_one("a[href*='race_id=']")
            num = item.select_one(".Race_Num")
            if not a or not num:
                continue
            rid = re.search(r"race_id=(\d+)", a["href"])
            rn = re.search(r"(\d+)", num.get_text())
            tm = item.select_one(".RaceList_Itemtime")
            t = re.search(r"(\d{1,2}):(\d{2})", tm.get_text() if tm else item.get_text(" "))
            if rid and rn:
                out[(venue, int(rn.group(1)))] = {
                    "race_id": rid.group(1),
                    "post_time": f"{int(t.group(1)):02d}:{t.group(2)}" if t else "",
                }
    return out


def _ints(text: str) -> list[int]:
    return [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", text)]


def parse_result(html: str) -> RaceResult:
    soup = BeautifulSoup(html, "html.parser")
    res = RaceResult(settled=False)
    for tr in soup.select("table#All_Result_Table tr.HorseList, table.RaceTable01 tr.HorseList"):
        rank_el = tr.select_one(".Rank, td.Result_Num")
        tds = tr.select("td.Num")
        if not rank_el or len(tds) < 2:
            continue
        rank = _ints(rank_el.get_text())
        num = _ints(tds[1].get_text())
        if not num:
            continue
        horse = num[0]
        if rank:
            res.ranks[horse] = rank[0]
        odds_el = tr.select_one("td.Odds, .Odds")
        if odds_el:
            m = re.search(r"\d+(?:\.\d+)?", odds_el.get_text())
            if m:
                res.odds[horse] = float(m.group(0))
    for cls, bet in PAYOUT_ROWS.items():
        for tr in soup.select(f"table.Payout_Detail_Table tr.{cls}"):
            r_el, p_el = tr.select_one("td.Result"), tr.select_one("td.Payout")
            if not r_el or not p_el:
                continue
            nums = _ints(r_el.get_text(" "))
            pays = _ints(p_el.get_text(" ").replace("円", " "))
            k = TYPE_SIZE[bet]
            combos = [tuple(nums[i:i + k]) for i in range(0, len(nums) - k + 1, k)]
            for combo, yen in zip(combos, pays):
                key = combo if bet in ORDERED else tuple(sorted(combo))
                res.pay.setdefault(bet, {})[key] = yen
    res.settled = bool(res.pay.get("単勝")) and bool(res.ranks)
    return res


def race_list(org: str, race_date: str, venues: list[str]) -> dict[tuple[str, int], dict]:
    url = f"{HOSTS[org]}/top/race_list_sub.html?kaisai_date={race_date.replace('-', '')}"
    return parse_race_list(fetch(url, ttl=600), venues)


def race_result(org: str, race_id: str) -> RaceResult:
    return parse_result(fetch(f"{HOSTS[org]}/race/result.html?race_id={race_id}", ttl=60))
