"""SPAT4のマークカード投票に似せた模擬サイトで、本物の手順ファイル（spat4.json）の入力の流れを確かめる。

模擬サイトは、スクショと部品の一覧から分かった作りをまねている:
- 券種ボタン（単勝・複勝・ワイド…）、6枚のカードに button.num.no-N
- 「連複流し」タブ: 軸と相手の2枚のカード
- 「投票金額入力へ」→ 左に入力欄2つ（一件ごと・全て同じ）、右の表の各行に金額欄
- 「買い目を追加する」でカードに戻る（入力済みの行は残る）
- 「投票内容確認へ」→ 合計金額を表示
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from app.bots.engine import SiteBot, wide_nagashi
from app.calibration import sample_order
from app.config import config

from .test_engine import chromium

MOCK = """<!doctype html><meta charset="utf-8"><body>
<div id="cards">
  <div class="tabs"><a onclick="tab('c')">単･複･連勝複式</a><a onclick="tab('n')">連複流し</a></div>
  <div class="types"><span class="t">単勝</span><span class="t">複勝</span><span class="t">ワイド</span></div>
  <div id="tab-c"></div><div id="tab-n" style="position:absolute;left:-5000px"></div>
  <span id="goamount">投票金額入力へ</span>
</div>
<div id="amount" style="display:none">
  <p>投票金額を入力してください</p>
  <div class="left"><input class="input number with-calc" name="buy_one" type="text" value="1"><span>セット</span>
    <input class="input number with-calc" name="buy_all" type="text"><span>セット</span></div>
  <table id="rows"></table>
  <span id="addmore">買い目を追加する</span><span id="toconfirm">投票内容確認へ</span>
</div>
<div id="confirm" style="display:none"><p>投票内容を確認の上、暗証番号、投票金額を入力してください</p>
  <span class="money" style="position:absolute;left:-5000px">999円</span>
  <div id="sum"></div>
  <input class="input with-calc" name="cfm_ansho" type="password"><input class="input number with-calc" name="cfm_amount" type="text">
  <span id="buy">投票する</span></div>
<script>
let mode = 'c', type = null, marks = [], rows = [];
window.log = [];
function show(id, on) { const e = document.getElementById(id); e.style.position = on ? '' : 'absolute';
  e.style.left = on ? '' : '-5000px'; }
function tab(m) { mode = m; show('tab-c', m === 'c'); show('tab-n', m === 'n'); marks = []; }
function card(box, k) {
  const d = document.createElement('div');
  for (let n = 1; n <= 16; n++) {
    const b = document.createElement('button'); b.className = 'btn num no-' + n; b.textContent = n;
    b.onclick = () => { (marks[k] ||= []).push(n); }; d.appendChild(b);
  }
  box.appendChild(d);
}
for (let k = 0; k < 6; k++) card(document.getElementById('tab-c'), k);
card(document.getElementById('tab-n'), 0); card(document.getElementById('tab-n'), 1);
document.querySelectorAll('.t').forEach((e) => e.onclick = () => { type = e.textContent;
  if (mode === 'c') { const k = marks.length; marks[k] = []; window.pending = k; } });
// 複・連勝複式: 券種を押すと次のカードに入る。カードの番号は押した順に関係なく button の位置で決まる
document.getElementById('tab-c').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  const k = [...document.getElementById('tab-c').children].indexOf(b.parentElement);
  window.log.push([type, k, +b.textContent]);
});
document.getElementById('goamount').onclick = () => {
  if (mode === 'c') {
    const byCard = {};
    for (const [t, k, n] of window.log) (byCard[k] ||= { t, nums: [] }).nums.push(n);
    Object.keys(byCard).sort().forEach((k) => rows.push({ t: byCard[k].t, c: byCard[k].nums.join('-'), n: 1 }));
  } else {
    const axis = marks[0][0], partners = marks[1];
    rows.push({ t: type + '流し', c: axis + '→' + partners.join('.'), n: partners.length });
  }
  window.log = []; marks = [];
  draw(); document.getElementById('cards').style.display = 'none'; document.getElementById('amount').style.display = '';
};
function draw() {
  const tb = document.getElementById('rows'); tb.innerHTML = '';
  rows.forEach((r) => { const tr = document.createElement('tr');
    tr.innerHTML = `<td>${r.t}</td><td>${r.c}</td><td><input class="input number" type="text"></td>`;
    const inp = tr.querySelector('input'); inp.value = r.v || ''; inp.oninput = () => r.v = inp.value; tb.appendChild(tr); });
}
document.getElementById('addmore').onclick = () => {
  document.getElementById('amount').style.display = 'none'; document.getElementById('cards').style.display = ''; tab('c');
};
document.getElementById('toconfirm').onclick = () => {
  const total = rows.reduce((a, r) => a + (+r.v || 0) * 100 * r.n, 0);
  window.rows = rows;
  document.getElementById('amount').style.display = 'none'; document.getElementById('confirm').style.display = '';
  document.getElementById('sum').innerHTML = `合計 <b>${rows.length}</b> 件 合計金額 <span class="money">${total.toLocaleString()}</span> 円`;
};
</script>"""


@pytest.fixture
def spat4(tmp_path, monkeypatch):
    if not os.path.exists(chromium):
        pytest.skip("Chromiumがありません")
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "chromium_path", chromium)
    (tmp_path / "shots").mkdir()
    (tmp_path / "selectors").mkdir()
    page = tmp_path / "mock.html"
    page.write_text(MOCK, encoding="utf-8")
    flow = json.loads((Path(__file__).parent.parent / "app" / "selectors" / "spat4.json").read_text(encoding="utf-8"))
    # ログインとレースを開くところは模擬サイトに置きかえ、入力から先は本物の手順を使う
    flow["login"] = [{"do": "goto", "url": page.as_uri()}]
    flow["open_race"] = [{"do": "wait_for", "sel": "#cards"}]
    (tmp_path / "selectors" / "spat4.json").write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")
    return SiteBot("spat4", {"subscriber": "x", "user_id": "x", "pin": "1234"})


def test_wide_nagashi_grouping():
    order = sample_order("大井", 11, 1)
    nag, rest = wide_nagashi(order["tickets"])
    assert nag == {"axis": 1, "partners": [2, 3], "amount": 100}
    assert [t["bet_type"] for t in rest] == ["単勝"] + ["複勝"] * 6
    # 金額が違うワイドはまとめない
    t = [{"bet_type": "ワイド", "combo": [1, 2], "amount": 100}, {"bet_type": "ワイド", "combo": [1, 3], "amount": 200}]
    assert wide_nagashi(t)[0] is None


def test_holiday_order_on_mark_cards(spat4):
    order = sample_order("大井", 11, 1)  # 単勝1、ワイド1-2,1-3、複勝2〜7 = 900円

    async def go():
        async with spat4 as bot:
            await bot.login()
            res = await bot.vote(order, "rehearsal")
            rows = await bot.page.evaluate("window.rows")
            filled = await bot.page.evaluate(
                "[document.querySelector('[name=cfm_ansho]').value, document.querySelector('[name=cfm_amount]').value]")
            return res, rows, filled
    res, rows, filled = asyncio.run(go())
    assert res.status == "rehearsed", res.message
    assert filled == ["1234", "900"]  # 暗証番号と、確認のための合計金額
    # 流し1行、カード1回目（単勝＋複勝5点）、買い目を追加して2回目（複勝1点）
    assert [(r["t"], r["c"], r["v"]) for r in rows] == [
        ("ワイド流し", "1→2.3", "1"),
        ("単勝", "1", "1"),
        ("複勝", "2", "1"), ("複勝", "3", "1"), ("複勝", "4", "1"), ("複勝", "5", "1"),
        ("複勝", "6", "1"),
        ("複勝", "7", "1"),
    ]


def test_amounts_go_to_the_right_rows(spat4):
    tickets = [{"bet_type": "単勝", "combo": [3], "amount": 700},
               {"bet_type": "複勝", "combo": [5], "amount": 100}]
    order = {"id": 1, "venue": "大井", "race_no": 11, "bet_type": "x", "tickets": tickets,
             "points": 2, "total": 800, "unit_amount": 700}

    async def go():
        async with spat4 as bot:
            await bot.login()
            return await bot.vote(order, "rehearsal"), await bot.page.evaluate("window.rows")
    res, rows = asyncio.run(go())
    assert res.status == "rehearsed", res.message
    assert [(r["c"], r["v"]) for r in rows] == [("3", "7"), ("5", "1")]


ODDS_TABLE = """<table>
<tr><th>枠</th><th>馬</th><th>馬名</th><th>単勝式オッズ</th><th>複勝式オッズ</th></tr>
<tr><td>1</td><td>1</td><td>エレパイオ</td><td><a>23.2</a></td><td>3.0-13</td></tr>
<tr><td>2</td><td>2</td><td>ラビダニーニャ</td><td><a>23.1</a></td><td>2.2-9.3</td></tr>
<tr><td rowspan="2">3</td><td>3</td><td>キタノプレミアム</td><td><a>8.6</a></td><td>2.1-8.7</td></tr>
<tr><td>4</td><td>トーセンアマンダ</td><td><a>68.7</a></td><td>8.1-39</td></tr>
<tr><td rowspan="2">4</td><td>5</td><td>ヴァンクラッセ</td><td><a>62.9</a></td><td>10-53</td></tr>
<tr><td>6</td><td>ラブリーファンク</td><td><a>71.5</a></td><td>5.3-25</td></tr>
<tr><td rowspan="2">8</td><td>13</td><td>ハヴィティベイ</td><td><a>170</a></td><td>14-71</td></tr>
<tr><td>14</td><td>アイビーブリザード</td><td><a>1.3</a></td><td>1.0-1.0</td></tr>
</table>"""


def test_reads_odds_inside_frames(spat4, tmp_path):
    """オッズの画面は区画（フレーム）に分かれていて、枠番の欄が2頭分の高さの行がある。"""
    import html as h
    page = tmp_path / "odds.html"
    page.write_text(f'<meta charset="utf-8"><iframe srcdoc="{h.escape(ODDS_TABLE)}"></iframe>', encoding="utf-8")
    read = [x for x in spat4.flow["read_odds"] if x["do"] == "read"][0]

    async def go():
        async with spat4 as bot:
            await bot.page.goto(page.as_uri())
            out = {}
            for n in (1, 3, 4, 5, 6, 13, 14):
                v = {"num": n}
                await bot._step(read, v)
                out[n] = v["odds"]
            return out
    # 4番は枠3の2行目（枠の欄がない行）。5番の行より前に「枠4」の欄があっても取り違えない
    # 13番の170倍のように、小数点のないオッズも読む
    assert asyncio.run(go()) == {1: 23.2, 3: 8.6, 4: 68.7, 5: 62.9, 6: 71.5, 13: 170.0, 14: 1.3}
