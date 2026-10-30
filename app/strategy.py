"""資金のルール（地方と中央で分ける。曜日による違いはない）。

地方競馬
  複勝◎の転がし: ◎のあるレースで、roll_stake（500円）から始め、的中したら払戻金を全額次へ。
      roll_target（4回）連続で的中するか、roll_budget（3,000円＝6回外れ）を使い切ったら終了。
  ◎★の馬券: ワイド◎→★流し・3連複◎→★流し・複勝★（各100円）。予算 bet_limit（7,000円）。
  単勝は買わない。
中央競馬
  単勝◎: tansho_base（100円）から、外れるたびに100円ずつ増やす（100→200→…→700円、計2,800円）。
      tansho_max_losses（7回）連続で外れるか、一度当たったら、その日の単勝は終了。
      直前の単勝オッズが tansho_min_odds（2.1倍）以下なら、その回は単勝だけ見送る（外れには数えない）。
  ◎★の馬券: 地方と同じ。予算 bet_limit（7,000円）。地方とは別に数える。
共通
  ◎がないレースは複勝★だけ。3連複の流しは★が2頭以上のときだけ（★から2頭の組み合わせを全部）。
  予算を超える買い目は保留にして、スマホで「買う／見送る」を選んでもらう。
"""
from __future__ import annotations

from itertools import combinations

from .betting import Ticket


def floor100(yen: int) -> int:
    return yen // 100 * 100


def plans_for(org: str, honmei: int | None, stars: list[int]) -> list[str]:
    """1レースで予約する買い方。roll=複勝◎の転がし（地方のみ）、holiday=◎★の馬券（中央は単勝◎も）。"""
    out = []
    if org == "nar" and honmei is not None:
        out.append("roll")
    if stars or (org == "jra" and honmei is not None):
        out.append("holiday")
    return out


def bet_tickets(plan: dict, settings: dict, tansho_amount: int) -> list[Ticket]:
    """◎と★から馬券を作る。tansho_amount=0なら単勝は買わない。"""
    h = plan.get("honmei")
    stars = sorted(s for s in plan["stars"] if s != h)
    out: list[Ticket] = []
    if h is not None and tansho_amount:
        out.append(Ticket("単勝", (h,), tansho_amount))
    if h is not None:
        for s in stars:
            out.append(Ticket("ワイド", tuple(sorted((h, s))), settings["wide_amount"]))
        for a, b in combinations(stars, 2):
            out.append(Ticket("3連複", tuple(sorted((h, a, b))), settings["sanrenpuku_amount"]))
    for s in stars:
        out.append(Ticket("複勝", (s,), settings["fukusho_amount"]))
    return out


def bets_cost(tickets) -> int:
    """予算（bet_limit）に数える額。単勝は別の予算なので除く。"""
    def get(t, k):
        return t[k] if isinstance(t, dict) else getattr(t, k)
    return sum(get(t, "amount") for t in tickets if get(t, "bet_type") != "単勝")


def tansho_amount(chain: dict, unresolved: int, settings: dict) -> int | None:
    """次の単勝の金額。Noneなら今回は単勝を見送る。

    unresolved: 単勝を買ったが結果がまだ分からないレースの数
    """
    if unresolved and settings["pending_policy"] == "skip":
        return None
    losses = chain["losses"] + (unresolved if settings["pending_policy"] == "assume_loss" else 0)
    if losses >= settings["tansho_max_losses"]:
        return None  # 予算（7回分）を使い切っている
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
    if chain["own_spent"] + settings["roll_stake"] > settings["roll_budget"]:
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
    if chain["own_spent"] + settings["roll_stake"] > settings["roll_budget"]:
        upd.update(status="exhausted", message=f"外れ。資金 {settings['roll_budget']:,}円を使い切ったので本日は終了")
    else:
        left = settings["roll_budget"] - chain["own_spent"]
        upd["message"] = f"外れ。残り資金 {left:,}円で {settings['roll_stake']}円から再挑戦"
    return upd
