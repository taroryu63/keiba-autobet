"""買い目の定義と、ボックス・フォーメーションを1点ずつの馬券に展開する処理。"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations, product

JRA_VENUES = ["札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"]
NAR_VENUES = [
    "帯広", "門別", "盛岡", "水沢", "浦和", "船橋", "大井", "川崎",
    "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀",
]
VENUES = {"jra": JRA_VENUES, "nar": NAR_VENUES}

# 券種: (選ぶ頭数, 着順を区別するか, 枠番で選ぶか)
BET_TYPES = {
    "単勝": (1, True, False),
    "複勝": (1, True, False),
    "枠連": (2, False, True),
    "馬連": (2, False, False),
    "ワイド": (2, False, False),
    "馬単": (2, True, False),
    "3連複": (3, False, False),
    "3連単": (3, True, False),
}
METHODS = ("normal", "box", "formation")
MAX_HORSE = 18
MAX_FRAME = 8
MAX_POINTS = 300


class BetError(ValueError):
    pass


@dataclass(frozen=True)
class Ticket:
    bet_type: str
    combo: tuple[int, ...]
    amount: int  # 円

    def label(self) -> str:
        sep = "→" if BET_TYPES[self.bet_type][1] and len(self.combo) > 1 else "-"
        return f"{self.bet_type} {sep.join(map(str, self.combo))} {self.amount}円"


def expand(bet_type: str, method: str, columns: list[list[int]], unit_amount: int) -> list[Ticket]:
    """入力された買い目を1点ずつの馬券に展開する。

    normal   : 単勝・複勝は1列に複数頭可（1頭1点）。それ以外は1列につき1頭ずつ、k列。
    box      : 1列にk頭以上。
    formation: k列。各列に1頭以上。
    """
    if bet_type not in BET_TYPES:
        raise BetError(f"券種が不正です: {bet_type}")
    if method not in METHODS:
        raise BetError(f"方式が不正です: {method}")
    if unit_amount < 100 or unit_amount % 100:
        raise BetError("金額は100円単位で指定してください")
    k, ordered, by_frame = BET_TYPES[bet_type]
    top = MAX_FRAME if by_frame else MAX_HORSE
    cols = [sorted(set(c)) for c in columns]
    for c in cols:
        for n in c:
            if not 1 <= n <= top:
                raise BetError(f"番号は1〜{top}で指定してください: {n}")

    combos: list[tuple[int, ...]]
    if k == 1:
        if len(cols) != 1 or not cols[0]:
            raise BetError("馬番を1頭以上選んでください")
        combos = [(n,) for n in cols[0]]
    elif method == "box":
        if len(cols) != 1 or len(cols[0]) < k:
            raise BetError(f"ボックスは{k}頭以上選んでください")
        gen = permutations if ordered else combinations
        combos = list(gen(cols[0], k))
    else:
        if len(cols) != k or any(not c for c in cols):
            raise BetError(f"{k}列すべてに番号を選んでください")
        if method == "normal" and any(len(c) != 1 for c in cols):
            raise BetError("通常は各列1頭ずつ選んでください")
        seen: set[tuple[int, ...]] = set()
        combos = []
        for combo in product(*cols):
            # 枠連だけは同じ枠同士（ゾロ目）を買える
            if len(set(combo)) < len(combo) and not by_frame:
                continue
            key = combo if ordered else tuple(sorted(combo))
            if key not in seen:
                seen.add(key)
                combos.append(key)
        if not combos:
            raise BetError("成立する組み合わせがありません")

    if len(combos) > MAX_POINTS:
        raise BetError(f"点数が多すぎます（{len(combos)}点、上限{MAX_POINTS}点）")
    return [Ticket(bet_type, c, unit_amount) for c in combos]
