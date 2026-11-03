"""仮想収支。予想とレース結果から、ルール通りに賭けていたらどうなったかを計算する。

本番と同じく、各レースは「発走のvote_offset_min分前」に投票し、
結果は「発走のresult_delay_min分後」に分かるものとして扱う。
（前のレースの結果がまだ分からないうちに次のレースを買う状況も、本番と同じように再現する）
単勝2.1倍以下の見送りは最終オッズで判定する（本番は直前オッズなので少しずれることがある）。
ルールは strategy.py と同じ（地方：転がし＋流し・複勝、中央：単勝＋流し・複勝）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from itertools import combinations, permutations

from . import strategy
from .betting import Ticket
from .results import RaceResult


def _t(s: str) -> datetime:
    return datetime.strptime(s, "%H:%M")


def _tickets(ts: list[Ticket]) -> list[dict]:
    return [{"bet_type": t.bet_type, "combo": list(t.combo), "amount": t.amount} for t in ts]


def simulate(race_date: str, races: list[dict], results: dict, settings: dict) -> dict:
    """races: [{"org", "venue", "race_no", "post_time", "honmei", "stars"}]（org が無ければ中央）
    results: {(venue, race_no): RaceResult}"""
    races = sorted((r for r in races if r.get("post_time")), key=lambda r: (r["post_time"], r["venue"]))
    rows: list[dict] = []
    over = False
    for org in ("jra", "nar"):
        mine = [r for r in races if r.get("org", "jra") == org]
        if not mine:
            continue
        bet_races = [r for r in mine if "holiday" in strategy.plans_for(org, r.get("honmei"), r.get("stars", []), settings)]
        bets = simulate_bets(bet_races, results, settings, org=org,
                             with_tansho=(org == "jra" and strategy.buys(settings, "buy_tansho")))
        for x in bets:
            x["kind"] = "単勝・流し・複勝" if org == "jra" else "流し・複勝"
        over |= any(x.get("over") for x in bets)
        rows += bets
        if org == "nar" and strategy.buys(settings, "buy_roll"):
            roll = simulate_roll([r for r in mine if r.get("honmei")], results, settings)
            for x in roll:
                x["kind"] = "転がし"
            rows += roll
    rows.sort(key=lambda x: (x["post_time"], x["venue"], x["kind"] != "転がし"))
    cost = sum(r["cost"] for r in rows)
    payout = sum(r["payout"] for r in rows)
    # 転がしは払戻金を賭け直すので、使った元手は「流し・複勝・単勝の合計＋転がしの元手」
    own = sum(r["cost"] for r in rows if r["kind"] != "転がし") + sum(r.get("own", 0) for r in rows)
    return {
        "race_date": race_date, "rows": rows,
        "cost": cost, "payout": payout, "own": own,
        "profit": payout - cost,
        "roi": round(payout / cost * 100, 1) if cost else None,
        "limit": settings["bet_limit"], "over_limit": over,
        "by_type": by_type(races, results),
    }


# 賭け方ごとの回収率（ルールとは別に、各100円で全レース買った場合）
BY_TYPE = [
    ("◎単勝", "単勝"), ("◎複勝", "複勝"), ("★複勝", "複勝"), ("◎→★3連単", "3連単"),
    ("◎→★3連複", "3連複"), ("◎→★ワイド", "ワイド"), ("◎→★馬単", "馬単"), ("◎→★馬連", "馬連"),
]


def type_combos(label: str, h: int | None, stars: list[int]) -> list[tuple[int, ...]]:
    """その賭け方で買う組番。◎→★の3連単・馬単は◎を1着に固定する。"""
    st = sorted(x for x in stars if x != h)
    if label == "★複勝":
        return [(x,) for x in st]
    if h is None:
        return []
    if label in ("◎単勝", "◎複勝"):
        return [(h,)]
    if label == "◎→★3連単":
        return [(h, a, b) for a, b in permutations(st, 2)]
    if label == "◎→★3連複":
        return [tuple(sorted((h, a, b))) for a, b in combinations(st, 2)]
    if label == "◎→★馬単":
        return [(h, x) for x in st]
    return [tuple(sorted((h, x))) for x in st]  # ワイド・馬連


def by_type(races: list[dict], results: dict) -> list[dict]:
    out = []
    for label, bet in BY_TYPE:
        races_n = points = payout = hits = 0
        for r in races:
            res: RaceResult | None = results.get((r["venue"], r["race_no"]))
            combos = type_combos(label, r.get("honmei"), r.get("stars", []))
            if not res or not res.settled or not combos:
                continue
            got = sum(res.pay.get(bet, {}).get(c, 0) for c in combos)
            races_n += 1
            points += len(combos)
            payout += got
            hits += got > 0
        cost = points * 100
        out.append({"label": label, "races": races_n, "points": points, "cost": cost, "payout": payout,
                    "hits": hits, "roi": round(payout / cost * 100, 1) if cost else None})
    return out


def simulate_bets(races: list[dict], results: dict, s: dict, with_tansho: bool, org: str = "jra") -> list[dict]:
    rows: list[dict] = []
    chain = {"losses": 0}
    done = False  # 単勝が当たったか、7回外れて、その日の単勝が終わったか
    pending: list[tuple[datetime, bool]] = []  # (結果が分かる時刻, 単勝が当たったか)
    spent = 0
    for r in races:
        vote_t = _t(r["post_time"]) - timedelta(minutes=s["vote_offset_min"])
        for known, hit in [p for p in pending if p[0] <= vote_t]:
            pending.remove((known, hit))
            if hit:
                done = True
            else:
                chain["losses"] += 1
                if chain["losses"] >= s["tansho_max_losses"]:
                    done = True
        row = {"venue": r["venue"], "race_no": r["race_no"], "post_time": r["post_time"],
               "honmei": r.get("honmei"), "stars": r.get("stars", []), "cost": 0, "payout": 0,
               "tickets": [], "note": ""}
        rows.append(row)
        res: RaceResult | None = results.get((r["venue"], r["race_no"]))
        if not res or not res.settled:
            row["note"] = "結果を取得できませんでした（計算に含めません）"
            continue
        amt = 0
        if with_tansho and r.get("honmei"):
            if done:
                row["note"] = "単勝は本日終了済み"
            else:
                amt = strategy.tansho_amount(chain, len(pending), s) or 0
                if amt == 0:
                    row["note"] = "前の単勝の結果待ちで単勝見送り"
                odds = res.odds.get(r["honmei"])
                if amt and odds is not None and odds <= s["tansho_min_odds"]:
                    row["note"] = f"単勝{odds}倍のため単勝見送り"
                    amt = 0
        ts = strategy.bet_tickets({"honmei": r.get("honmei"), "stars": r.get("stars", [])}, s, amt, org)
        row["tickets"] = _tickets(ts)
        row["cost"] = sum(t.amount for t in ts)
        row["payout"] = res.payout(row["tickets"])
        spent += strategy.bets_cost(ts)
        row["spent_so_far"] = spent
        if spent > s["bet_limit"]:
            row["over"] = True
            row["note"] = (row["note"] + "・" if row["note"] else "") + "予算超え（本番なら保留）"
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
