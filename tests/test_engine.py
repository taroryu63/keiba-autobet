"""画面操作エンジンを、本物のブラウザと模擬の投票サイトで確認する。"""
import asyncio
import json
import os
import shutil

import pytest

from app.bots.engine import SiteBot
from app.config import config

MOCK_HTML = """<!doctype html><meta charset="utf-8"><body>
<div id="login"><a class="go" style="display:none">ログイン</a><input name="id"><a class="go" onclick="login()">ログイン</a></div>
<div id="race" hidden>
  <table><tr data-n="4"><td class="num">4</td><td class="odds">ODDS4</td></tr>
         <tr data-n="5"><td class="num">5</td><td class="odds">8.0</td></tr></table>
  <select id="type"><option>単勝</option><option>複勝</option><option>ワイド</option></select>
  <span id="marks"></span>
  MARKS
  <input id="units"><button id="set" onclick="setT()">セット</button>
  <button id="end" onclick="document.getElementById('sum').textContent=total.toLocaleString()+'円'">入力終了</button>
  <div id="sum"></div>
  <button id="buy" onclick="buy()">購入する</button>
  <button id="clear" onclick="total=0;sets=[];document.getElementById('sum').textContent=''">全て取消</button>
</div>
<div id="done" hidden>受付番号 0001<button id="again" onclick="again()">続けて投票</button></div>
<script>
let total=0, sets=[], cur=[];
window.bought=[];
function login(){document.getElementById('login').hidden=true;document.getElementById('race').hidden=false}
function pick(c,n){cur.push(n)}
function setT(){const u=+document.getElementById('units').value;total+=u*100;
  sets.push([document.getElementById('type').value,cur.join('-'),u*100]);cur=[]}
function buy(){window.bought=window.bought.concat(sets);window.buys=(window.buys||0)+1;
  document.getElementById('race').hidden=true;document.getElementById('done').hidden=false}
function again(){total=0;sets=[];document.getElementById('sum').textContent='';
  document.getElementById('race').hidden=false;document.getElementById('done').hidden=true}
</script>"""
MOCK_HTML = MOCK_HTML.replace("MARKS", "".join(
    f'<button id="m{c}-{n}" onclick="pick({c},{n})">{c}-{n}</button>' for c in (1, 2) for n in range(1, 17)))

FLOW = {
    "required": ["user"],
    "timeout": 3000,
    "login": [
        {"do": "goto", "url": "{url}"},
        {"do": "fill", "sel": "input[name='id']", "value": "{user}"},
        {"do": "click", "sel": "a.go"},
    ],
    "open_race": [{"do": "click", "sel": "#again", "optional": True, "timeout": 300},
                  {"do": "wait_for", "sel": "#race"}],
    "read_odds": [{"do": "read", "sel": "tr[data-n='{num}'] .odds", "var": "odds"}],
    "ticket": [
        {"do": "select", "sel": "#type", "label": "{bet_type}"},
        {"do": "pick"},
        {"do": "fill", "sel": "#units", "value": "{units}"},
        {"do": "click", "sel": "#set"},
    ],
    "pick": [{"do": "click", "sel": "#m{col}-{num}"}],
    "confirm": [{"do": "click", "sel": "#end"}, {"do": "expect_text", "text": "{total_comma}円", "timeout": 1500}],
    "purchase": [{"do": "click", "sel": "#buy"}],
    "done_check": [{"do": "wait_for", "sel": "text=/受付番号/"}],
    "cancel": [{"do": "click", "sel": "#clear", "optional": True, "timeout": 1000}],
}

ORDER = {"id": 1, "venue": "東京", "race_no": 11, "bet_type": "◎★", "points": 3, "total": 1500,
         "unit_amount": 1200,
         "tickets": [{"bet_type": "単勝", "combo": [4], "amount": 1200},
                     {"bet_type": "ワイド", "combo": [4, 5], "amount": 100},
                     {"bet_type": "複勝", "combo": [5], "amount": 200}]}

chromium = os.environ.get("CHROMIUM_PATH") or shutil.which("chromium") or "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"


@pytest.fixture
def site(tmp_path, monkeypatch):
    if not os.path.exists(chromium):
        pytest.skip("Chromiumがありません")
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "chromium_path", chromium)
    (tmp_path / "shots").mkdir()
    (tmp_path / "selectors").mkdir()

    def make(odds="3.4"):
        page = tmp_path / "mock.html"
        page.write_text(MOCK_HTML.replace("ODDS4", odds), encoding="utf-8")
        flow = json.loads(json.dumps(FLOW).replace("{url}", page.as_uri()))
        (tmp_path / "selectors" / "mock.json").write_text(json.dumps(flow), encoding="utf-8")
        return SiteBot("mock", {"user": "me"})
    return make


async def _vote(bot, mode, rule=None):
    async with bot:
        await bot.login()
        res = await bot.vote(ORDER, mode, rule)
        bought = await bot.page.evaluate("window.bought")
    return res, bought


def test_live_buys_all_tickets(site):
    res, bought = asyncio.run(_vote(site(), "live"))
    assert res.status == "done", res.message
    assert bought == [["単勝", "4", 1200], ["ワイド", "4-5", 100], ["複勝", "5", 200]]


def test_rehearsal_never_buys(site):
    res, bought = asyncio.run(_vote(site(), "rehearsal"))
    assert res.status == "rehearsed" and bought == []


def test_low_odds_drops_tansho(site):
    rule = {"horse": 4, "min": 2.1, "unknown": "skip"}
    res, bought = asyncio.run(_vote(site("2.0"), "live", rule))
    assert res.status == "done" and res.odds == 2.0
    assert bought == [["ワイド", "4-5", 100], ["複勝", "5", 200]]
    res, bought = asyncio.run(_vote(site("3.4"), "live", rule))
    assert [b[0] for b in bought] == ["単勝", "ワイド", "複勝"]


def test_unreadable_odds_skips_tansho(site):
    rule = {"horse": 4, "min": 2.1, "unknown": "skip"}
    res, bought = asyncio.run(_vote(site("取消"), "live", rule))
    assert [b[0] for b in bought] == ["ワイド", "複勝"]


def test_total_mismatch_aborts_before_purchase(site):
    bot = site()
    bot.flow["ticket"][2]["value"] = "1"  # 金額を間違えて入力する画面の変化を再現
    res, bought = asyncio.run(_vote(bot, "live"))
    assert res.status == "failed" and bought == []
    assert "購入前に中止" in res.message


def test_connection_check_stops_at_failure(site):
    from app.calibration import run_check
    bot = site()
    steps = asyncio.run(run_check(bot, "東京", 11, 4))
    assert [s["ok"] for s in steps] == [True] * 7, steps
    bot = site()
    bot.flow["ticket"][0]["sel"] = "#nothing"
    bot.flow["ticket"][0]["timeout"] = 500
    steps = asyncio.run(run_check(bot, "東京", 11, 5))
    # 馬券の入力で止まり、確認画面は飛ばし、最後の「土日の買い方」だけは試す
    assert [(s["stage"], s["ok"]) for s in steps][-2:] == [("ticket", False), ("many", False)]


def test_connection_check_continues_past_time(site):
    from app.calibration import run_check
    bot = site()
    bot.flow["post_time"] = [{"do": "click", "sel": "#nothing", "timeout": 500}]
    steps = asyncio.run(run_check(bot, "東京", 11, 4))
    assert [s["ok"] for s in steps] == [True, False, True, True, True, True, True]


def test_follows_new_window(site, tmp_path):
    form = tmp_path / "form.html"
    form.write_text('<meta charset="utf-8"><input id="x"><p id="ok">マークカード画面</p>', encoding="utf-8")
    bot = site()
    page = tmp_path / "mock.html"
    page.write_text(page.read_text(encoding="utf-8").replace(
        "<body>", f"<body><a id='mc' href='{form.as_uri()}' target='_blank'>マークカード投票</a>"), encoding="utf-8")

    async def go():
        async with bot:
            await bot.login()
            await bot._step({"do": "click", "sel": "#mc", "new_window": True}, {})
            await bot._step({"do": "fill", "sel": "#x", "value": "1"}, {})
            return await bot.page.inner_text("#ok")
    assert asyncio.run(go()) == "マークカード画面"


def test_new_window_click_still_fails_when_button_missing(site):
    bot = site()

    async def go():
        async with bot:
            await bot.login()
            await bot._step({"do": "click", "sel": "#none", "new_window": True, "timeout": 500}, {})
    with pytest.raises(Exception):
        asyncio.run(go())


def test_all_selectors_are_valid():
    """手順ファイルのセレクタが、すべてブラウザで解釈できる書き方になっていること。"""
    import re
    from pathlib import Path
    from playwright.async_api import async_playwright

    if not os.path.exists(chromium):
        pytest.skip("Chromiumがありません")

    async def go():
        bad = []
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=chromium)
            pg = await b.new_page()
            await pg.set_content("<p>x</p>")
            for f in (Path(__file__).parent.parent / "app" / "selectors").glob("*.json"):
                for sec, steps in json.loads(f.read_text(encoding="utf-8")).items():
                    for st in steps if isinstance(steps, list) else []:
                        if isinstance(st, dict) and "sel" in st:
                            try:
                                await pg.locator(re.sub(r"\{\w+\}", "1", st["sel"])).count()
                            except Exception:
                                bad.append(f"{f.name} {sec}: {st['sel']}")
            await b.close()
        return bad
    assert asyncio.run(go()) == []


def test_connection_check_lists_parts_on_failure(site):
    from app.calibration import run_check
    bot = site()
    bot.flow["ticket"][0]["sel"] = "#nothing"
    bot.flow["ticket"][0]["timeout"] = 500
    steps = asyncio.run(run_check(bot, "東京", 11, 5))
    parts = steps[-1]["parts"]
    assert 'button#m1-5 onclick=pick(1,5) "1-5"' in parts
    units = next(line for line in parts.splitlines() if "input#units" in line)
    assert "value" not in units  # 入力欄の値は出さない


def test_splits_into_batches(site):
    bot = site()
    bot.flow["max_tickets_per_vote"] = 2

    async def go():
        async with bot:
            await bot.login()
            res = await bot.vote(ORDER, "live")
            return res, await bot.page.evaluate("window.bought"), await bot.page.evaluate("window.buys")
    res, bought, buys = asyncio.run(go())
    assert res.status == "done" and "2回に分けて" in res.message
    assert buys == 2
    assert bought == [["単勝", "4", 1200], ["ワイド", "4-5", 100], ["複勝", "5", 200]]
