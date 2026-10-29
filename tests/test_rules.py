from app.betting import BetError, expand
from app.db import DEFAULT_SETTINGS
from app.parser import parse
from app.results import RaceResult, parse_race_list, parse_result
from app.sim import simulate
from app import strategy

import pytest

S = dict(DEFAULT_SETTINGS)

SAMPLE = """新潟
１R　★ 3.7.10
２R　◎ 4 ★ 5.7.9
11R　◎ 14 ★ 8.10.12

東京
11R　◎ 2 ★ 3.6.9.12

京都
11R　◎ 10 ★ 1.7.13.14.15.18
"""


def test_expand_counts():
    assert len(expand("馬連", "box", [[1, 2, 3, 4]], 100)) == 6
    assert len(expand("3連単", "box", [[1, 2, 3]], 100)) == 6
    assert len(expand("3連単", "formation", [[1], [2, 3], [2, 3, 4]], 100)) == 4
    assert len(expand("ワイド", "formation", [[1], [2, 3]], 100)) == 2
    assert len(expand("単勝", "normal", [[1, 5]], 200)) == 2
    with pytest.raises(BetError):
        expand("馬連", "normal", [[1], [2]], 150)


def test_parse_sample():
    r = parse(SAMPLE)
    assert r.errors == []
    assert [(x.venue, x.race_no, x.honmei, x.stars) for x in r.races] == [
        ("新潟", 1, None, [3, 7, 10]),
        ("新潟", 2, 4, [5, 7, 9]),
        ("新潟", 11, 14, [8, 10, 12]),
        ("東京", 11, 2, [3, 6, 9, 12]),
        ("京都", 11, 10, [1, 7, 13, 14, 15, 18]),
    ]
    assert all(x.org == "jra" for x in r.races)


def test_parse_weekday_and_errors():
    r = parse("大井\n3R 15:45 ◎5\n4R ◎1.2\n川崎\n１Ｒ　◎　７")
    assert [(x.org, x.venue, x.race_no, x.honmei, x.post_time) for x in r.races] == [
        ("nar", "大井", 3, 5, "15:45"), ("nar", "川崎", 1, 7, "")]
    assert "◎は1頭" in r.errors[0]


def test_holiday_tickets():
    ts = strategy.holiday_tickets({"honmei": 4, "stars": [5, 7, 9]}, S, 300)
    assert [(t.bet_type, t.combo, t.amount) for t in ts] == [
        ("単勝", (4,), 300),
        ("ワイド", (4, 5), 100), ("ワイド", (4, 7), 100), ("ワイド", (4, 9), 100),
        ("複勝", (5,), 100), ("複勝", (7,), 100), ("複勝", (9,), 100)]
    only_stars = strategy.holiday_tickets({"honmei": None, "stars": [3, 7]}, S, 0)
    assert [(t.bet_type, t.combo) for t in only_stars] == [("複勝", (3,)), ("複勝", (7,))]


def test_tansho_ladder():
    # 100, 200, ... 1000, 1100（以後100円ずつ）
    amounts = [strategy.tansho_amount({"losses": n}, 0, S) for n in range(11)]
    assert amounts == [100 * (n + 1) for n in range(11)]
    assert sum(amounts[:10]) == 5500
    # 結果待ち1件は外れ扱いで増額、skip設定なら見送り
    assert strategy.tansho_amount({"losses": 2}, 1, S) == 400
    assert strategy.tansho_amount({"losses": 2}, 1, {**S, "pending_policy": "skip"}) is None


def test_roll_rules():
    c = {"status": "active", "wins": 0, "stake": None, "own_spent": 0, "carry": 0}
    assert strategy.roll_stake(c, S) == (500, True)
    c["own_spent"] = 500
    c.update(strategy.apply_roll_result(c, 750, S))
    assert c["stake"] == 700 and c["carry"] == 50 and c["wins"] == 1
    for _ in range(3):
        c.update(strategy.apply_roll_result(c, c["stake"] * 2, S))
    assert c["status"] == "won" and c["wins"] == 4
    # 6回外れたら3000円を使い切って終了
    c = {"status": "active", "wins": 0, "stake": None, "own_spent": 0, "carry": 0}
    for i in range(6):
        stake, new = strategy.roll_stake(c, S)
        c["own_spent"] += stake
        c.update(strategy.apply_roll_result(c, 0, S))
    assert c["status"] == "exhausted" and c["own_spent"] == 3000


def _res(winner, odds, fuku=None, wide=None, tan=None):
    r = RaceResult(settled=True, ranks={winner: 1}, odds=odds)
    r.pay = {"単勝": {(winner,): tan or int(odds[winner] * 100)}, "複勝": fuku or {}, "ワイド": wide or {}}
    return r


def test_sim_holiday_ends_on_tansho_hit():
    races = [
        {"venue": "東京", "race_no": 1, "post_time": "10:00", "honmei": 1, "stars": [2]},
        {"venue": "東京", "race_no": 2, "post_time": "10:30", "honmei": 3, "stars": []},
        {"venue": "東京", "race_no": 3, "post_time": "11:00", "honmei": 5, "stars": [6]},  # 1.8倍で単勝見送り
        {"venue": "東京", "race_no": 4, "post_time": "11:30", "honmei": 7, "stars": []},   # 的中
        {"venue": "東京", "race_no": 5, "post_time": "12:00", "honmei": 9, "stars": [1]},  # 終了済み
    ]
    res = {
        ("東京", 1): _res(4, {1: 5.0, 4: 3.0}, fuku={(2,): 150}),
        ("東京", 2): _res(4, {3: 4.0, 4: 3.0}),
        ("東京", 3): _res(5, {5: 1.8}, fuku={(6,): 200}, wide={(5, 6): 300}),
        ("東京", 4): _res(7, {7: 6.0}),
        ("東京", 5): _res(9, {9: 3.0}),
    }
    rep = simulate("2026-10-25", races, res, S)  # 日曜
    rows = rep["rows"]
    assert [t["amount"] for t in rows[0]["tickets"] if t["bet_type"] == "単勝"] == [100]
    assert [t["amount"] for t in rows[1]["tickets"] if t["bet_type"] == "単勝"] == [200]
    assert not any(t["bet_type"] == "単勝" for t in rows[2]["tickets"]) and "1.8倍" in rows[2]["note"]
    assert [t["amount"] for t in rows[3]["tickets"] if t["bet_type"] == "単勝"] == [300]
    assert rows[4]["cost"] == 0 and "終了" in rows[4]["note"]
    # 1R: 単100+ワイド100+複100=300 払戻150 / 2R: 200 / 3R: ワイド100+複100 払戻300+200 / 4R: 300 払戻1800
    assert rep["cost"] == 300 + 200 + 200 + 300
    assert rep["payout"] == 150 + 0 + 500 + 1800


def test_sim_holiday_overlapping_races_assume_loss():
    # 10分おき：前のレースの結果（発走10分後）が次の投票（発走5分前）に間に合わない
    races = [{"venue": v, "race_no": 1, "post_time": t, "honmei": 1, "stars": []}
             for v, t in [("東京", "10:00"), ("京都", "10:10"), ("新潟", "10:20")]]
    res = {(v, 1): _res(2, {1: 5.0, 2: 3.0}) for v in ("東京", "京都", "新潟")}
    rep = simulate("2026-10-25", races, res, S)
    assert [r["tickets"][0]["amount"] for r in rep["rows"]] == [100, 200, 300]


def test_sim_roll():
    races = [{"venue": "大井", "race_no": n, "post_time": t, "honmei": 1, "stars": []}
             for n, t in [(1, "15:00"), (2, "15:05"), (3, "15:30"), (4, "16:00")]]
    res = {("大井", n): _res(1, {1: 2.0}, fuku={(1,): p}) for n, p in [(1, 150), (2, 150), (3, 0), (4, 130)]}
    rep = simulate("2026-10-26", races, res, S)  # 月曜
    rows = rep["rows"]
    assert rows[0]["cost"] == 500 and rows[0]["payout"] == 750
    assert "結果待ち" in rows[1]["note"]       # 15:05発走は15:00の結果（15:10）に間に合わない
    assert rows[2]["cost"] == 700 and rows[2]["payout"] == 0
    assert rows[3]["cost"] == 500 and rows[3]["own"] == 500   # 外れたので500円から再挑戦
    assert rep["own"] == 1000


RACE_LIST_HTML = """
<dl class="RaceList_DataList"><dt><p class="RaceList_DataTitle">4回 東京 8日目</p></dt><dd><ul>
<li class="RaceList_DataItem"><a href="../race/result.html?race_id=202605040801&rf=race_list">
<div class="Race_Num"><span>1R</span></div><span class="RaceList_Itemtime">10:05</span></a></li>
<li class="RaceList_DataItem"><a href="../race/result.html?race_id=202605040811&rf=race_list">
<div class="Race_Num"><span>11R</span></div><span class="RaceList_Itemtime">15:40</span></a></li>
</ul></dd></dl>"""

RESULT_HTML = """
<table id="All_Result_Table">
<tr class="HorseList"><td class="Result_Num"><div class="Rank">1</div></td><td class="Num Waku2">2</td><td class="Num Txt_C"><div>4</div></td><td class="Odds"><span>3.4</span></td></tr>
<tr class="HorseList"><td class="Result_Num"><div class="Rank">2</div></td><td class="Num Waku5">5</td><td class="Num Txt_C"><div>9</div></td><td class="Odds"><span>12.1</span></td></tr>
</table>
<table class="Payout_Detail_Table">
<tr class="Tansho"><th>単勝</th><td class="Result"><div><span>4</span></div></td><td class="Payout"><span>340円</span></td></tr>
<tr class="Fukusho"><th>複勝</th><td class="Result"><div><span>4</span></div><div><span>9</span></div><div><span>7</span></div></td><td class="Payout"><span>150円<br />310円<br />1,220円</span></td></tr>
<tr class="Wide"><th>ワイド</th><td class="Result"><ul><li><span>4</span></li><li><span>9</span></li></ul><ul><li><span>4</span></li><li><span>7</span></li></ul><ul><li><span>7</span></li><li><span>9</span></li></ul></td><td class="Payout"><span>620円<br />2,150円<br />5,890円</span></td></tr>
</table>"""


def test_netkeiba_parsers():
    rl = parse_race_list(RACE_LIST_HTML, ["東京", "京都"])
    assert rl[("東京", 11)] == {"race_id": "202605040811", "post_time": "15:40"}
    r = parse_result(RESULT_HTML)
    assert r.settled and r.winner == 4 and r.odds[9] == 12.1
    assert r.pay["複勝"][(7,)] == 1220 and r.pay["ワイド"][(4, 7)] == 2150
    tickets = [{"bet_type": "単勝", "combo": [4], "amount": 300},
               {"bet_type": "ワイド", "combo": [9, 4], "amount": 100},
               {"bet_type": "複勝", "combo": [7], "amount": 100}]
    assert r.payout(tickets) == 1020 + 620 + 1220


def test_result_without_rank_table_uses_tansho_payout():
    """着順の表が読めなくても、払戻金があれば確定とし、1着は単勝の馬とする（本物の地方競馬のページで起きた）。"""
    html = RESULT_HTML.split('<table class="Payout_Detail_Table">')[1]
    r = parse_result('<table class="Payout_Detail_Table">' + html)
    assert r.ranks == {} and r.settled and r.winner == 4
