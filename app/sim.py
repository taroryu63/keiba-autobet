"""仮想収支。予想とレース結果から、ルール通りに賭けていたらどうなったかを計算する。

本番と同じく、各レースは「発走のvote_offset_min分前」に投票し、
結果は「発走のresult_delay_min分後」に分かるものとして扱う。
（前のレースの結果がまだ分からないうちに次のレースを買う状況も、本番と同じように再現する）
単勝2.1倍以下の見送りは最終オッズで判定する（本番は直前オッズなので少しずれることがある）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import strategy
from .betting import Ticket
from .results import RaceResult


def _t(s: str) -> datetime:
    return datetime.strptime(s, "%H:%M")


def _tickets(ts: list[Ticket]) -> list[dict]:
    return [{"bet_type": t.bet_type, "combo": list(t.combo), "amount": t.amount} for t in ts]


def simulate(race_date: str, races: list[dict], results: dict, settings: dict) -> dict:
    """races: [{"venue", "race_no", "post_time", "honmei", "stars"}]
    results: {(venue, race_no): RaceResult}"""
    hol = strategy.is_holiday(date.fromisoformat(race_date))
    races = sorted((r for r in races if r.get("post_time")), key=lambda r: (r["post_time"], r["venue"]))
    rows = simulate_holiday(races, results, settings) if hol else simulate_roll(races, results, settings)
    cost = sum(r["cost"] for r in rows)
    payout = sum(r["payout"] for r in rows)
    limit = strategy.day_limit(date.fromisoformat(race_date), settings)
    own = cost if hol else sum(r.get("own", 0) for r in rows)
    return {
        "race_date": race_date, "holiday": hol, "rows": rows,
        "cost": cost, "payout": payout,
        # 平日の転がしは払戻金を賭け直すので、収支は「使った元手」と「最後に手元に残った額」で見る
        "own": own,
        "profit": payout - cost,
        "roi": round(payout / cost * 100, 1) if cost else None,
        "limit": limit, "over_limit": own > limit,
    }


def simulate_holiday(races: list[dict], results: dict, s: dict) -> list[dict]:
    rows: list[dict] = []
    chain = {"losses": 0}
    ended_at: datetime | None = None
    pending: list[tuple[datetime, bool]] = []  # (結果が分かる時刻, 単勝が当たったか)
    spent = 0
    for r in races:
        vote_t = _t(r["post_time"]) - timedelta(minutes=s["vote_offset_min"])
        for known, hit in [p for p in pending if p[0] <= vote_t]:
            pending.remove((known, hit))
            if hit and ended_at is None:
                ended_at = known
            elif not hit:
                chain["losses"] += 1
        row = {"venue": r["venue"], "race_no": r["race_no"], "post_time": r["post_time"],
               "honmei": r.get("honmei"), "stars": r.get("stars", []), "cost": 0, "payout": 0,
               "tickets": [], "note": ""}
        rows.append(row)
        if ended_at is not None:
            row["note"] = "単勝的中で終了済み"
            continue
        res: RaceResult | None = results.get((r["venue"], r["race_no"]))
        if not res or not res.settled:
            row["note"] = "結果を取得できませんでした（計算に含めません）"
            continue
        amt = 0
        if r.get("honmei"):
            amt = strategy.tansho_amount(chain, len(pending), s) or 0
            if amt == 0:
                row["note"] = "前の単勝の結果待ちで単勝見送り"
            odds = res.odds.get(r["honmei"])
            if amt and odds is not None and odds <= s["tansho_min_odds"]:
                row["note"] = f"単勝{odds}倍のため単勝見送り"
                amt = 0
        ts = strategy.holiday_tickets({"honmei": r.get("honmei"), "stars": r.get("stars", [])}, s, amt)
        row["tickets"] = _tickets(ts)
        row["cost"] = sum(t.amount for t in ts)
        row["payout"] = res.payout(row["tickets"])
        spent += row["cost"]
        row["spent_so_far"] = spent
        if spent > s["holiday_limit"]:
            row["note"] = (row["note"] + "・" if row["note"] else "") + "上限超え（本番なら保留）"
        if amt:
            hit = res.winner == r["honmei"]
            row["tansho_hit"] = hit
            pending.append((_t(r["post_time"]) + timedelta(minutes=s["result_delay_min"]), hit))
    return rows


def simulate_roll(races: list[dict], results: dict, s: dict) -> list[dict]:
    rows: list[dict] = []
    chain = {"status": "active", "wins": 0, "stake": None, "own_spent": 0, "carry": 0}
    pending: tuple[datetime, int, dict] | None = None  # (結果が分かる時刻, 払戻, その行)

    def settle(payout: int) -> str:
        upd = strategy.apply_roll_result(chain, payout, s)
        chain.update(upd)
        return upd["message"]

    for r in races:
        if not r.get("honmei"):
            continue
        vote_t = _t(r["post_time"]) - timedelta(minutes=s["vote_offset_min"])
        row = {"venue": r["venue"], "race_no": r["race_no"], "post_time": r["post_time"],
               "honmei": r["honmei"], "stars": [], "cost": 0, "payout": 0, "own": 0, "tickets": [], "note": ""}
        if pending and pending[0] <= vote_t:
            pending[2]["note"] = settle(pending[1])
            pending = None
        rows.append(row)
        if chain["status"] != "active":
            row["note"] = "本日の転がしは終了済み"
            continue
        if pending:
            row["note"] = "前のレースの結果待ちで見送り"
            continue
        res: RaceResult | None = results.get((r["venue"], r["race_no"]))
        if not res or not res.settled:
            row["note"] = "結果を取得できませんでした（計算に含めません）"
            continue
        rs = strategy.roll_stake(chain, s)
        if rs is None:
            chain["status"] = "exhausted"
            row["note"] = "資金切れ"
            continue
        stake, new = rs
        if new:
            chain["own_spent"] += stake
            row["own"] = stake
        row["tickets"] = [{"bet_type": "複勝", "combo": [r["honmei"]], "amount": stake}]
        row["cost"] = stake
        row["payout"] = res.payout(row["tickets"])
        pending = (_t(r["post_time"]) + timedelta(minutes=s["result_delay_min"]), row["payout"], row)
    if pending:
        pending[2]["note"] = settle(pending[1])
    return rows
