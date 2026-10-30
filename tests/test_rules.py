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


def test_sim_holiday_stops_only_tansho_after_hit():
    races = [
        {"venue": "東京", "race_no": 1, "post_time": "10:00", "honmei": 1, "stars": [2]},
        {"venue": "東京", "race_no": 2, "post_time": "10:30", "honmei": 3, "stars": []},
        {"venue": "東京", "race_no": 3, "post_time": "11:00", "honmei": 5, "stars": [6]},  # 1.8倍で単勝見送り
        {"venue": "東京", "race_no": 4, "post_time": "11:30", "honmei": 7, "stars": []},   # 的中
        {"venue": "東京", "race_no": 5, "post_time": "12:00", "honmei": 9, "stars": [1]},  # 単勝は終了済み
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
    # 5R: 単勝は的中済みなので買わないが、ワイド・複勝は買う
    assert [t["bet_type"] for t in rows[4]["tickets"]] == ["ワイド", "複勝"] and "的中済み" in rows[4]["note"]
    # 1R: 単100+ワイド100+複100=300 払戻150 / 2R: 200 / 3R: ワイド100+複100 払戻300+200 / 4R: 300 払戻1800 / 5R: 200
    assert rep["cost"] == 300 + 200 + 200 + 300 + 200
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

# 本物の地方競馬の結果ページ（大井 2026/10/29 1R）と同じ作り
RESULT_HTML = """
<table class="RaceTable01 RaceCommon_Table ResultRefund Table_Show_All" id="All_Result_Table">
<tr class="Header"><th class="Result_Num">着<br />順</th><th class="Num">枠</th><th class="Num">馬<br />番</th>
<th class="Horse_Info"><div class="Horse_Name">馬名</div></th><th class="Odds">単勝<br>オッズ</th></tr>
<tr >
<td class="Result_Num"><div class="Rank">1</div></td>
<td class="Num Waku6"><div>6</div></td>
<td class="Num Waku"><div>8</div></td>
<td class="Horse_Info"><span class="Horse_Name"><a title="アイトカコイトカ">アイトカコイトカ</a></span></td>
<td class="Odds BgYellow Txt_C"><span class="OddsPeople">1</span></td>
<td class="Odds Txt_R"><span  class="Odds_Ninki">2.6</span></td>
</tr>
<tr >
<td class="Result_Num"><div class="Rank">2</div></td>
<td class="Num Waku3"><div>3</div></td>
<td class="Num Waku"><div>3</div></td>
<td class="Horse_Info"><span class="Horse_Name"><a>ブライトゲイザー</a></span></td>
<td class="Odds BgYellow Txt_C"><span class="OddsPeople">4</span></td>
<td class="Odds Txt_R"><span  class="Odds_Ninki">9.8</span></td>
</tr>
<tr >
<td class="Result_Num"><div class="Rank">取消</div></td>
<td class="Num Waku1"><div>1</div></td>
<td class="Num Waku"><div>1</div></td>
<td class="Horse_Info"><span class="Horse_Name"><a>トリケシ</a></span></td>
<td class="Odds BgYellow Txt_C"><span class="OddsPeople"></span></td>
<td class="Odds Txt_R"><span  class="Odds_Ninki">---</span></td>
</tr>
</table>
<table class="Payout_Detail_Table">
<tr class="Tansho"><th>単勝</th><td class="Result"><div><span>8</span></div></td><td class="Payout"><span>260円</span></td></tr>
<tr class="Fukusho"><th>複勝</th><td class="Result"><div><span>8</span></div><div><span>3</span></div><div><span>2</span></div></td><td class="Payout"><span>140円<br />260円<br />4,800円</span></td></tr>
<tr class="Wide"><th>ワイド</th><td class="Result"><ul><li><span>3</span></li><li><span>8</span></li></ul><ul><li><span>2</span></li><li><span>8</span></li></ul><ul><li><span>2</span></li><li><span>3</span></li></ul></td><td class="Payout"><span>470円<br />11,400円<br />18,350円</span></td></tr>
<tr class="Wakutan"><th>枠単</th><td class="Result"><ul><li><span>6</span></li><li><span>3</span></li></ul></td><td class="Payout"><span>1,860円</span></td></tr>
<tr class="Fuku3"><th>3連複</th><td class="Result"><ul><li><span>2</span></li><li><span>3</span></li><li><span>8</span></li></ul></td><td class="Payout"><span>56,090円</span></td></tr>
</table>"""


def test_netkeiba_parsers():
    rl = parse_race_list(RACE_LIST_HTML, ["東京", "京都"])
    assert rl[("東京", 11)] == {"race_id": "202605040811", "post_time": "15:40"}
    r = parse_result(RESULT_HTML)
    assert r.settled and r.winner == 8
    assert r.ranks == {8: 1, 3: 2}            # 取消の馬は着順なし
    assert r.odds[8] == 2.6 and r.odds[3] == 9.8  # 人気（OddsPeople）ではなく単勝オッズを読む
    assert r.pay["複勝"][(2,)] == 4800 and r.pay["ワイド"][(2, 8)] == 11400
    assert r.pay["枠単"][(6, 3)] == 1860 and r.pay["3連複"][(2, 3, 8)] == 56090
    tickets = [{"bet_type": "単勝", "combo": [8], "amount": 300},
               {"bet_type": "ワイド", "combo": [8, 3], "amount": 100},
               {"bet_type": "複勝", "combo": [2], "amount": 100}]
    assert r.payout(tickets) == 780 + 470 + 4800


def test_result_without_rank_table_uses_tansho_payout():
    """着順の表が読めなくても、払戻金があれば確定とし、1着は単勝の馬とする（本物の地方競馬のページで起きた）。"""
    html = RESULT_HTML.split('<table class="Payout_Detail_Table">')[1]
    r = parse_result('<table class="Payout_Detail_Table">' + html)
    assert r.ranks == {} and r.settled and r.winner == 8


def test_sim_weekday_mixes_roll_and_bets():
    """平日：◎だけは転がしのみ、◎★は転がしと◎★の買い方、★だけは複勝★。"""
    races = [{"venue": "大井", "race_no": 1, "post_time": "15:00", "honmei": 1, "stars": [2]},
             {"venue": "大井", "race_no": 2, "post_time": "16:00", "honmei": 3, "stars": []},
             {"venue": "大井", "race_no": 3, "post_time": "17:00", "honmei": None, "stars": [4]}]
    res = {("大井", 1): _res(1, {1: 3.0}, fuku={(1,): 150, (2,): 200}, wide={(1, 2): 500}),
           ("大井", 2): _res(5, {3: 4.0, 5: 6.0}),
           ("大井", 3): _res(4, {4: 2.0}, fuku={(4,): 130})}
    rep = simulate("2026-11-02", races, res, S)  # 月曜
    got = [(r["race_no"], r["kind"], r["cost"]) for r in rep["rows"]]
    assert got == [(1, "転がし", 500), (1, "◎★", 300), (2, "転がし", 700), (3, "◎★", 100)]
    # 1Rの転がし：500円→750円、2Rで700円を賭けて外れ
    assert rep["rows"][0]["payout"] == 750 and rep["rows"][2]["payout"] == 0
    # 1Rの◎★：単勝300+ワイド500+複勝200
    assert rep["rows"][1]["payout"] == 300 + 500 + 200
    assert rep["own"] == 500 + 300 + 100  # 転がしの元手500円＋◎★の買い方400円
