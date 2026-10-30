"""接続テスト。投票サイトにログインして、購入直前まで1段階ずつ試し、各段階の画面を残す。購入はしない。"""
from __future__ import annotations

from .bots.engine import SiteBot

STAGES = [
    ("login", "ログイン"),
    ("time", "発走時刻の読み取り"),
    ("race", "レースを開く"),
    ("odds", "単勝オッズの読み取り"),
    ("ticket", "馬券の入力（複勝100円）"),
    ("confirm", "確認画面（合計金額）"),
    ("many", "今のルールの買い方で入力（流し・複勝、中央は単勝も。購入はしません）"),
]

# 読めなくても投票は進められる段階。失敗しても次へ進む
OPTIONAL = {"time", "odds"}
# 前の段階で止まっても試す段階（レースを開き直して最初からやるので）
ALWAYS = {"many"}


def sample_order(venue: str, race_no: int, horse: int, org: str = "jra") -> dict:
    """今のルールの見本: ◎＝horse、★＝その次の3頭（1〜8番から）。
    中央：単勝◎＋ワイド流し3点＋3連複流し3点＋複勝★3点＝10点 1,000円
    地方：ワイド流し3点＋3連複流し3点＋複勝★3点＝9点 900円"""
    from .db import DEFAULT_SETTINGS
    from .strategy import bet_tickets
    stars = [((horse + k - 1) % 8) + 1 for k in range(1, 4)]
    ts = bet_tickets({"honmei": horse, "stars": stars}, DEFAULT_SETTINGS, 100 if org == "jra" else 0)
    tickets = [{"bet_type": t.bet_type, "combo": list(t.combo), "amount": t.amount} for t in ts]
    return {"id": 0, "org": org, "venue": venue, "race_no": race_no, "bet_type": "◎★", "tickets": tickets,
            "points": len(tickets), "total": sum(t["amount"] for t in tickets), "unit_amount": 100}


async def run_check(bot: SiteBot, venue: str, race_no: int, horse: int) -> list[dict]:
    """各段階の結果 [{"stage", "label", "ok", "info", "shot", "parts"}] を返す。"""
    vars = {**bot.credentials, "venue": venue, "race_no": race_no, "num": horse, "bet_type": "複勝",
            "method_label": "通常", "total": 100, "total_comma": "100", "points": 1}
    one = [{"bet_type": "複勝", "combo": [horse], "amount": 100}]

    async def many() -> str:
        try:
            await bot.run("cancel", vars)
        except Exception:
            pass
        order = sample_order(venue, race_no, horse, "nar" if bot.site == "spat4" else "jra")
        res = await bot.vote(order, "rehearsal")
        if res.status != "rehearsed":
            raise RuntimeError(res.message)
        return f"{order['points']}点 {order['total']:,}円を購入直前まで入力できました"

    calls = {
        "login": lambda: bot.login(),
        "time": lambda: bot.read_post_time(venue, race_no),
        "race": lambda: bot.run("open_race", vars),
        "odds": lambda: bot.read_numbers("read_odds", vars, ("odds",)),
        "ticket": lambda: bot._enter(one, vars),
        "confirm": lambda: bot.run("confirm", vars),
        "many": many,
    }
    stages = list(STAGES)
    if bot.flow.get("odds_before_open"):
        i, j = [k for k, (st, _) in enumerate(stages) if st in ("race", "odds")]
        stages[i], stages[j] = stages[j], stages[i]
    out: list[dict] = []
    async with bot:
        stopped = False
        for stage, label in stages:
            if stopped and stage not in ALWAYS:
                continue
            try:
                got = await calls[stage]()
                info = f"読み取り: {got}" if got else ""
                ok = True
            except Exception as e:
                info, ok = str(e)[:500], False
            shot = await bot.screenshot(f"check-{stage}")
            parts = ""
            if not ok or stage == "odds":
                await bot.dump(f"check-{stage}")
                try:
                    parts = await bot.elements()
                except Exception as e:
                    parts = f"部品の一覧を取れませんでした: {e}"
            out.append({"stage": stage, "label": label, "ok": ok, "info": str(info), "shot": shot, "parts": parts})
            if not ok and stage == "login":
                break  # ログインできなければ何もできない
            if not ok and stage not in OPTIONAL:
                stopped = True
        try:
            await bot.run("cancel", vars)
        except Exception:
            pass
    return out
