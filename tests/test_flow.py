"""予約→投票→結果確認→次の金額、の流れを偽の投票サイトと偽の結果で確認する。"""
import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, results
from app.bots.engine import VoteResult
from app.config import JST, config
from app.results import RaceResult


class FakeBot:
    votes: list = []
    odds: dict = {}

    def __init__(self, site):
        self.site = site

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def login(self):
        pass

    async def read_post_time(self, venue, race_no):
        return None

    async def vote(self, order, mode, odds_rule=None):
        tickets = order["tickets"]
        if odds_rule:
            o = FakeBot.odds.get((order["venue"], order["race_no"]))
            if o is not None and o <= odds_rule["min"]:
                tickets = [t for t in tickets if t["bet_type"] != "単勝"]
                if not tickets:
                    return VoteResult("skipped", "見送り", tickets=[], odds=o)
        FakeBot.votes.append((order["venue"], order["race_no"], mode, tickets))
        return VoteResult("done", "ok", tickets=tickets, odds=FakeBot.odds.get((order["venue"], order["race_no"])))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "allow_live", True)
    main.state.clear()
    main.state["no_loop"] = True
    FakeBot.votes, FakeBot.odds = [], {}
    clock = {"now": datetime(2026, 11, 1, 8, 0, tzinfo=JST)}  # 日曜
    monkeypatch.setattr(main, "now", lambda: clock["now"])
    race_list, race_results = {}, {}
    monkeypatch.setattr(results, "race_list", lambda org, d, v: race_list)
    monkeypatch.setattr(results, "race_result", lambda org, rid: race_results[rid])
    with TestClient(main.app) as c:
        main.state["runner"].bot_factory = FakeBot
        assert c.post("/api/login", json={"password": "testpass123"}).status_code == 200
        c.clock, c.race_list, c.race_results = clock, race_list, race_results
        yield c


def tick(c, hhmm, day=None):
    h, m = map(int, hhmm.split(":"))
    c.clock["now"] = c.clock["now"].replace(day=day or c.clock["now"].day, hour=h, minute=m)
    asyncio.run(main.state["runner"].tick(c.clock["now"]))


def settle(c, venue, race_no, winner, fuku=None, wide=None, tan=500):
    r = RaceResult(settled=True, ranks={winner: 1}, odds={winner: tan / 100})
    r.pay = {"単勝": {(winner,): tan}, "複勝": fuku or {}, "ワイド": wide or {}}
    c.race_results[f"{venue}{race_no}"] = r


def orders(c):
    return {(o["venue"], o["race_no"]): o for o in c.get("/api/state", params={"d": c.clock["now"].date().isoformat()}).json()["orders"]}


def test_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", tmp_path)
    main.state.clear()
    main.state["no_loop"] = True
    with TestClient(main.app) as c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/login", json={"password": "wrong"}).status_code == 401


def test_holiday_flow(client):
    c = client
    c.put("/api/settings", json={"mode": "live"})
    text = "東京\n1R ◎1 ★2.3\n2R ◎4\n3R ◎5 ★6\n4R ★7.8\n5R ◎9 ★1\n6R ◎2\n"
    preview = c.post("/api/parse", json={"race_date": "2026-11-01", "text": text}).json()
    assert preview["holiday"] and preview["errors"] == []
    assert preview["rows"][3]["desc"].startswith("複勝 7.8")
    assert c.post("/api/bulk", json={"race_date": "2026-11-01", "text": text}).json()["created"] == 6

    # 発走時刻はnetkeibaの一覧から入る
    for n, t in [(1, "10:00"), (2, "10:30"), (3, "11:00"), (4, "11:30"), (5, "12:00"), (6, "12:30")]:
        c.race_list[("東京", n)] = {"race_id": f"東京{n}", "post_time": t}
    tick(c, "08:01")
    assert [o["status"] for o in orders(c).values()] == ["queued"] * 6, orders(c)

    tick(c, "09:55")  # 1R 投票
    assert FakeBot.votes[-1][3][0] == {"bet_type": "単勝", "combo": [1], "amount": 100}
    assert len(FakeBot.votes[-1][3]) == 5  # 単勝1 + ワイド2 + 複勝2

    settle(c, "東京", 1, winner=2, fuku={(2,): 150}, wide={(1, 2): 400})
    tick(c, "10:10")  # 結果確認
    o1 = orders(c)[("東京", 1)]
    assert o1["payout"] == 150 + 400

    FakeBot.odds[("東京", 2)] = 2.0
    tick(c, "10:25")  # 2R: 単勝のみ・オッズ2.0倍 → 見送り
    assert orders(c)[("東京", 2)]["status"] == "skipped"

    tick(c, "10:55")  # 3R: 1回外れたので200円
    assert FakeBot.votes[-1][3][0]["amount"] == 200
    settle(c, "東京", 3, winner=5, tan=380)
    tick(c, "11:10")  # 単勝的中 → この日はもう単勝は買わない（ほかは続ける）
    assert orders(c)[("東京", 3)]["payout"] == 760

    tick(c, "11:25")  # 4R: ★だけなので複勝★はそのまま買う
    assert FakeBot.votes[-1][3] == [{"bet_type": "複勝", "combo": [7], "amount": 100},
                                    {"bet_type": "複勝", "combo": [8], "amount": 100}]
    tick(c, "11:55")  # 5R: 単勝は買わず、ワイドと複勝だけ
    assert [t["bet_type"] for t in FakeBot.votes[-1][3]] == ["ワイド", "複勝"]
    tick(c, "12:25")  # 6R: ◎だけ（単勝だけ）なので買う馬券がない
    assert orders(c)[("東京", 6)]["status"] == "cancelled"
    assert len(FakeBot.votes) == 4


def test_holiday_limit_holds(client):
    c = client
    c.put("/api/settings", json={"mode": "live", "holiday_limit": 300})
    c.post("/api/bulk", json={"race_date": "2026-11-01", "text": "東京\n1R 10:00 ◎1 ★2\n2R 11:00 ◎3 ★4"})
    tick(c, "09:55")
    tick(c, "10:55")
    st = orders(c)
    assert st[("東京", 1)]["status"] == "done"
    assert st[("東京", 2)]["status"] == "held"
    assert c.post(f"/api/orders/{st[('東京', 2)]['id']}/approve").status_code == 200
    tick(c, "10:56")
    assert orders(c)[("東京", 2)]["status"] == "done"


def by_strategy(c, d):
    return {(o["venue"], o["race_no"], o["strategy"]): o
            for o in c.get("/api/state", params={"d": d}).json()["orders"]}


def test_weekday_roll_and_bets(client):
    """平日：◎は転がし、◎★のレースは転がしと◎★の買い方の両方、★だけは複勝★。"""
    c = client
    c.clock["now"] = datetime(2026, 11, 2, 8, 0, tzinfo=JST)  # 月曜
    c.put("/api/settings", json={"mode": "live"})
    text = "大井\n1R 15:00 ◎1 ★2.3\n2R 15:05 ◎4\n3R 16:00 ◎5\n4R 16:30 ★6"
    preview = c.post("/api/parse", json={"race_date": "2026-11-02", "text": text}).json()
    assert preview["rows"][0]["strategy"] == "roll+holiday"
    r = c.post("/api/bulk", json={"race_date": "2026-11-02", "text": text}).json()
    assert r["created"] == 5 and r["skipped"] == []
    tick(c, "14:55")  # 1R: 転がし（複勝◎500円）と◎★の買い方（単勝◎100円・ワイド・複勝★）
    kinds = sorted((v[3][0]["bet_type"], len(v[3])) for v in FakeBot.votes)
    assert kinds == [("単勝", 5), ("複勝", 1)]
    assert [{"bet_type": "複勝", "combo": [1], "amount": 500}] in [v[3] for v in FakeBot.votes]
    tick(c, "15:00")  # 2Rの転がしは、1Rの結果待ちで見送り
    assert by_strategy(c, "2026-11-02")[("大井", 2, "roll")]["status"] == "skipped"
    for n in (1, 2, 3, 4):
        c.race_list[("大井", n)] = {"race_id": f"大井{n}", "post_time": ""}
    settle(c, "大井", 1, winner=1, fuku={(1,): 180})
    tick(c, "15:10")
    tick(c, "15:55")
    assert FakeBot.votes[-1][3] == [{"bet_type": "複勝", "combo": [5], "amount": 900}]
    tick(c, "16:25")  # 4R: ★だけ → 複勝★
    assert FakeBot.votes[-1][3] == [{"bet_type": "複勝", "combo": [6], "amount": 100}]
    day = c.get("/api/state", params={"d": "2026-11-02"}).json()["day"]
    assert day["roll"]["wins"] == 1 and day["roll"]["own_spent"] == 500
    # 1Rの単勝◎1番が的中したので、平日も単勝はこの日終了
    assert day["tansho"]["status"] == "won"
    # ◎★の買い方の使用額（転がしの元手は含まない）: 1R 500円 + 4R 100円
    assert day["used"] == 600 and day["limit"] == 10000


def test_holiday_rejects_roll(client):
    c = client
    r = c.post("/api/orders", json={"strategy": "roll", "org": "jra", "race_date": "2026-11-01",
                                    "venue": "東京", "race_no": 1, "post_time": "15:00", "horse": 1})
    assert r.status_code == 400


def test_off_mode_does_not_use_browser(client):
    c = client
    c.post("/api/bulk", json={"race_date": "2026-11-01", "text": "東京\n1R 10:00 ◎1"})
    tick(c, "09:55")
    o = orders(c)[("東京", 1)]
    assert o["status"] == "done" and o["plan"]["paper"] and FakeBot.votes == []
