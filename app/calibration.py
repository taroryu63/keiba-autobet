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
    ("many", "土日の買い方で入力（単勝・ワイド流し・複勝6点、購入はしません）"),
]

# 読めなくても投票は進められる段階。失敗しても次へ進む
OPTIONAL = {"time", "odds"}
# 前の段階で止まっても試す段階（レースを開き直して最初からやるので）
ALWAYS = {"many"}


def sample_order(venue: str, race_no: int, horse: int) -> dict:
    """土日の買い方の見本: 単勝◎、ワイド◎-★2点（流し）、複勝★6点 = 9点 900円。"""
    stars = [((horse + k - 1) % 8) + 1 for k in range(1, 7)]  # ◎以外の6頭（1〜8番から）
    tickets = [{"bet_type": "単勝", "combo": [horse], "amount": 100}]
    tickets += [{"bet_type": "ワイド", "combo": sorted([horse, s]), "amount": 100} for s in stars[:2]]
    tickets += [{"bet_type": "複勝", "combo": [s], "amount": 100} for s in stars]
    return {"id": 0, "venue": venue, "race_no": race_no, "bet_type": "◎★", "tickets": tickets,
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
        order = sample_order(venue, race_no, horse)
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
