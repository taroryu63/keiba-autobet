"""接続テスト。投票サイトにログインして、購入直前まで1段階ずつ試し、各段階の画面を残す。購入はしない。"""
from __future__ import annotations

from .bots.engine import SiteBot

STAGES = [
    ("login", "ログイン"),
    ("time", "発走時刻の読み取り"),
    ("race", "レースを開く"),
    ("odds", "単勝オッズの読み取り"),
    ("probe", "流しの画面を見る（マークはしません）"),
    ("ticket", "馬券の入力（複勝100円）"),
    ("confirm", "確認画面（合計金額）"),
    ("many", "6点を超えて入れられるか見る（購入はしません）"),
]

OPTIONAL = {"time", "odds", "probe", "many"}
# 確認画面で止まっても、最後の「6点を超えて」は見ておく（別の画面で試すので）
ALWAYS = {"many"}


async def run_check(bot: SiteBot, venue: str, race_no: int, horse: int) -> list[dict]:
    """各段階の結果 [{"stage", "label", "ok", "info", "shot"}] を返す。
    失敗した段階で止まる（発走時刻・オッズの読み取りは失敗しても続ける）。"""
    vars = {**bot.credentials, "venue": venue, "race_no": race_no, "num": horse, "bet_type": "複勝", "bet_label": bot.flow.get("bet_labels", {}).get("複勝", "複勝"),
            "method_label": "通常", "card": 1, "units": 1, "amount": 100, "total": 100, "total_comma": "100",
            "points": 1, "combo": str(horse)}
    calls = {
        "login": lambda: bot.login(),
        "time": lambda: bot.read_post_time(venue, race_no),
        "race": lambda: bot.run("open_race", vars),
        "odds": lambda: bot.read_numbers("read_odds", vars, ("odds",)),
        "probe": lambda: bot.run("probe", vars),
        "ticket": lambda: bot.run("ticket", vars, [(1, horse)]),
        "confirm": lambda: bot.run("confirm", vars),
        "many": lambda: _fill_all_cards(bot, vars, horse),
    }
    out: list[dict] = []
    async with bot:
        stopped = False
        for stage, label in STAGES:
            if stopped and stage not in ALWAYS:
                continue
            if stage == "many" and not bot.flow.get("cards_per_page"):
                continue
            try:
                got = await calls[stage]()
                info = f"読み取り: {got}" if got else ""
                ok = True
            except Exception as e:
                info, ok = str(e)[:500], False
            if stage == "probe" and not bot.flow.get("probe"):
                continue  # 見るものがないサイトは飛ばす
            shot = await bot.screenshot(f"check-{stage}")
            parts = ""
            if not ok or stage in ("probe", "many"):
                await bot.dump(f"check-{stage}")
                try:
                    parts = await bot.elements()
                except Exception as e:
                    parts = f"部品の一覧を取れませんでした: {e}"
            out.append({"stage": stage, "label": label, "ok": ok, "info": info, "shot": shot, "parts": parts})
            if stage == "probe":
                try:
                    await bot.run("probe_back", vars)  # 元の入力画面に戻る
                except Exception:
                    pass
            # 発走時刻とオッズは読めなくても投票は進められるので、次の段階も試す
            if not ok and stage not in OPTIONAL:
                stopped = True
        try:
            await bot.run("cancel", vars)
        except Exception:
            pass
    return out


async def _fill_all_cards(bot: SiteBot, vars: dict, horse: int) -> str:
    """レースを開き直して、全部のカードに複勝をマークする。そのあとの画面（7枚目が出るか）を見るため。"""
    try:
        await bot.run("cancel", vars)
    except Exception:
        pass
    await bot.run("open_race", vars)
    n = bot.flow["cards_per_page"]
    for card in range(1, n + 1):
        await bot.run("ticket", {**vars, "card": card}, [(1, horse)])
    return f"{n}枚のカードにマークしました"
