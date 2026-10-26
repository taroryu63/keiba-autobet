"""資金のルール。

平日: 複勝の転がしのみ。roll_stake（500円）から始め、的中したら払戻金を全額次へ。
      roll_target（4回）連続で的中するか、weekday_limit（3000円）を使い切ったら終了。
休日: 1レースごとに ◎ と ★ を受け取り、
      ◎が無いレースは複勝★だけ。
      単勝◎（tansho_base円から、外れるたびにtansho_base円ずつ増やす。単勝が当たったらその日は終了）
      ワイド◎-★ 各wide_amount円、複勝★ 各fukusho_amount円 を買う。
      holiday_limit（10000円）を超える買い目は保留にして、スマホで判断してもらう。
"""
from __future__ import annotations

from datetime import date

from .betting import Ticket

try:
    import jpholiday
except ImportError:  # 祝日判定ライブラリが無ければ土日だけで判定
    jpholiday = None


def is_holiday(d: date) -> bool:
    return d.weekday() >= 5 or bool(jpholiday and jpholiday.is_holiday(d))


def day_limit(d: date, settings: dict) -> int:
    return settings["holiday_limit"] if is_holiday(d) else settings["weekday_limit"]


def floor100(yen: int) -> int:
    return yen // 100 * 100


def holiday_tickets(plan: dict, settings: dict, tansho_amount: int) -> list[Ticket]:
    """◎と★から馬券を作る。tansho_amount=0なら単勝は買わない。◎が無いレースは複勝★だけ。"""
    h = plan.get("honmei")
    stars = [s for s in plan["stars"] if s != h]
    out: list[Ticket] = []
    if h is None:
        return [Ticket("複勝", (s,), settings["fukusho_amount"]) for s in stars]
    if tansho_amount:
        out.append(Ticket("単勝", (h,), tansho_amount))
    for s in stars:
        out.append(Ticket("ワイド", tuple(sorted((h, s))), settings["wide_amount"]))
    for s in stars:
        out.append(Ticket("複勝", (s,), settings["fukusho_amount"]))
    return out


def tansho_amount(chain: dict, unresolved: int, settings: dict) -> int | None:
    """次の単勝の金額。Noneなら今回は単勝を見送る。

    unresolved: 単勝を買ったが結果がまだ分からないレースの数
    """
    if unresolved and settings["pending_policy"] == "skip":
        return None
    losses = chain["losses"] + (unresolved if settings["pending_policy"] == "assume_loss" else 0)
    return settings["tansho_base"] * (1 + losses)


def tansho_hit(order: dict, result: dict) -> bool | None:
    """結果から、その買い目の単勝が当たったかを判定する。分からなければNone。"""
    if "tansho_hit" in result:
        return bool(result["tansho_hit"])
    if "winner" in result:
        return result["winner"] == order["plan"]["honmei"]
    return None


def roll_stake(chain: dict, settings: dict) -> tuple[int, bool] | None:
    """転がしで次に賭ける金額と、新しく500円から始めるかどうか。Noneなら資金切れ。"""
    if chain["stake"]:
        return chain["stake"], False
    if chain["own_spent"] + settings["roll_stake"] > settings["weekday_limit"]:
        return None
    return settings["roll_stake"], True


def apply_roll_result(chain: dict, payout: int, settings: dict) -> dict:
    """転がしの結果を反映した chains の更新内容を返す。"""
    if payout > 0:
        wins = chain["wins"] + 1
        upd = {"wins": wins, "stake": floor100(payout), "carry": chain["carry"] + payout % 100}
        if wins >= settings["roll_target"]:
            upd.update(status="won", stake=None,
                       message=f"{wins}回連続で的中！払戻 {payout:,}円で本日は終了")
        else:
            upd["message"] = f"{wins}回目的中（払戻 {payout:,}円）。次は {floor100(payout):,}円"
        return upd
    upd = {"wins": 0, "stake": None}
    if chain["own_spent"] + settings["roll_stake"] > settings["weekday_limit"]:
        upd.update(status="exhausted", message=f"外れ。資金 {settings['weekday_limit']:,}円を使い切ったので本日は終了")
    else:
        left = settings["weekday_limit"] - chain["own_spent"]
        upd["message"] = f"外れ。残り資金 {left:,}円で {settings['roll_stake']}円から再挑戦"
    return upd
