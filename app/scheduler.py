"""投票時刻になった買い目の投票と、結果の確認を行う。10秒ごとに tick() が呼ばれる。"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from . import notify, results, strategy
from .betting import VENUES
from .bots.engine import SiteBot
from .config import JST, config
from .db import DB, now

log = logging.getLogger(__name__)

SITES = {"jra": "ipat", "nar": "spat4"}
SALES_CLOSE_MIN = 2     # 発売締切は発走の2分前
RESULT_RETRY_MIN = 3    # 払戻が未確定なら何分後に再確認するか
RESULT_GIVE_UP_MIN = 90  # 発走から何分たっても分からなければ手入力をお願いする


def post_dt(order: dict) -> datetime:
    return datetime.fromisoformat(f"{order['race_date']}T{order['post_time']}:00").replace(tzinfo=JST)


def race_label(o: dict) -> str:
    return f"{o['venue']}{o['race_no']}R"


def credentials(site: str) -> dict:
    if site == "ipat":
        return {"inet_id": config.ipat_inet_id, "subscriber": config.ipat_subscriber,
                "pin": config.ipat_pin, "pars": config.ipat_pars}
    return {"subscriber": config.spat4_subscriber, "user_id": config.spat4_user_id, "pin": config.spat4_pin}


def default_bot(site: str) -> SiteBot:
    return SiteBot(site, credentials(site))


class Runner:
    def __init__(self, db: DB, bot_factory=default_bot):
        self.db = db
        self.bot_factory = bot_factory
        self._lock = asyncio.Lock()
        self._time_tried: dict[str, datetime] = {}   # 開催場ごとの、発走時刻を最後に取りに行った時刻
        self._time_notified: set[str] = set()

    async def loop(self) -> None:
        for oid in self.db.recover_interrupted():
            notify.send("要確認", f"#{oid} は投票中に再起動しました。購入履歴を確認してください", "high")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick で例外")
            await asyncio.sleep(10)

    async def tick(self, at: datetime | None = None) -> None:
        async with self._lock:
            at = at or now()
            self.expire(at)
            await self.fill_post_times(at)
            await self.check_results(at)
            s = self.db.settings()
            if s["paused"]:
                return
            for o in self.due(at):
                ready = self.prepare(o, s)
                if ready:
                    await self.vote(ready, s)

    # --- 締切切れ ---
    def expire(self, at: datetime) -> None:
        for o in self.db.list_orders(at.date().isoformat()) + self.db.list_orders((at.date() - timedelta(days=1)).isoformat()):
            if o["post_time"] and o["status"] in ("queued", "pending", "held") and at >= post_dt(o) - timedelta(minutes=SALES_CLOSE_MIN):
                self.db.set_status(o["id"], "expired", "締切までに投票できませんでした", expect=o["status"])
                if o["status"] == "held":
                    notify.send("見送り", f"{race_label(o)}（保留中）は締切になったので買いませんでした")

    # --- 発走時刻 ---
    async def fill_post_times(self, at: datetime) -> None:
        """発走時刻が未定の買い目について、投票サイトから時刻を読み取る（5分おき）。"""
        with self.db.conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM orders WHERE status='needs_time' AND race_date>=?", (at.date().isoformat(),))]
        groups: dict[str, list[dict]] = {}
        for o in map(self.db.get, ids):
            groups.setdefault(f"{o['race_date']}|{o['org']}|{o['venue']}", []).append(o)
        todo: dict[str, list[dict]] = {}
        for key, items in groups.items():
            last = self._time_tried.get(key)
            if last and at - last < timedelta(minutes=5):
                continue
            self._time_tried[key] = at
            todo.setdefault(items[0]["org"], []).extend(items)
        offset = self.db.settings()["vote_offset_min"]
        for org, items in todo.items():
            got: dict[tuple[str, int], str] = {}
            # まずnetkeibaのレース一覧から読む
            for d in {o["race_date"] for o in items}:
                try:
                    rl = await asyncio.to_thread(results.race_list, org, d, VENUES[org])
                    for o in items:
                        info = rl.get((o["venue"], o["race_no"]))
                        if o["race_date"] == d and info and info["post_time"]:
                            got[(o["venue"], o["race_no"])] = info["post_time"]
                except Exception as e:
                    log.warning("netkeibaから発走時刻を読めません: %s", e)
            # 読めなかった分は投票サイトから
            rest = [o for o in items if (o["venue"], o["race_no"]) not in got]
            if rest and all(credentials(SITES[org]).values()):
                try:
                    async with self.bot_factory(SITES[org]) as bot:
                        await bot.login()
                        for o in rest:
                            k = (o["venue"], o["race_no"])
                            if k not in got:
                                try:
                                    t = await bot.read_post_time(*k)
                                    if t:
                                        got[k] = t
                                except Exception as e:
                                    log.warning("発走時刻を読めません %s%sR: %s", *k, e)
                except Exception as e:
                    log.warning("発走時刻の取得でログイン失敗: %s", e)
            missing = []
            for o in items:
                t = got.get((o["venue"], o["race_no"]))
                if t:
                    self.set_post_time(o, t, offset, at)
                else:
                    missing.append(o)
            # 通知が多くなりすぎないよう、開催日・開催場ごとに1回だけ知らせる
            for o in missing:
                key = f"{o['race_date']}|{o['venue']}"
                if key not in self._time_notified:
                    self._time_notified.add(key)
                    notify.send("発走時刻を入力してください",
                                f"{o['venue']} の発走時刻を投票サイトから読み取れませんでした。アプリで時刻を入力してください", "high")

    def set_post_time(self, o: dict, post_time: str, offset: int, at: datetime) -> None:
        o = {**o, "post_time": post_time}
        post = post_dt(o)
        if at >= post - timedelta(minutes=SALES_CLOSE_MIN):
            self.db.update_order(o["id"], post_time=post_time)
            self.db.set_status(o["id"], "expired", "締切を過ぎています")
            return
        vote_at = max(post - timedelta(minutes=offset), at).isoformat(timespec="seconds")
        self.db.update_order(o["id"], post_time=post_time, vote_at=vote_at)
        self.db.set_status(o["id"], "queued" if o["strategy"] != "manual" else "pending", "", expect="needs_time")

    def due(self, at: datetime) -> list[dict]:
        at_iso = at.isoformat(timespec="seconds")
        with self.db.conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM orders WHERE status IN ('queued','pending') AND vote_at<=? ORDER BY vote_at, id",
                (at_iso,))]
        return [self.db.get(i) for i in ids]

    # --- 投票前に金額を決める ---
    def prepare(self, o: dict, s: dict) -> dict | None:
        """買い目を確定させて status='pending' にする。投票しない場合はNone。"""
        if o["strategy"] == "roll":
            return self._prepare_roll(o, s)
        if o["strategy"] != "holiday":
            self.db.set_status(o["id"], "pending", "", expect=o["status"])
            return self.db.get(o["id"])
        amt, chain_id, why = 0, None, ""
        if o["org"] == "jra" and o["plan"].get("honmei"):
            # 中央の単勝◎：外れるたびに増やし、7回外れるか一度当たったらその日は終わり
            chain = self.db.chain_for(o["race_date"], "tansho", create=True)
            chain_id = chain["id"]
            if chain["status"] == "active":
                unresolved = sum(
                    1 for x in self.db.chain_orders(chain["id"])
                    if x["id"] != o["id"] and x["plan"].get("tansho") and x["payout"] is None
                    and x["status"] in ("pending", "voting", "done", "unknown")
                )
                amt = strategy.tansho_amount(chain, unresolved, s) or 0
                if not amt:
                    why = "単勝は予算（7回分）に届いているため見送り" if unresolved else "前の単勝の結果待ちで見送り"
            else:
                why = "単勝は本日終了済み"
        tickets = strategy.bet_tickets(o["plan"], s, amt)
        if not tickets:
            self.db.set_status(o["id"], "cancelled", f"{why or '単勝は本日終了'}のため、買う馬券がありません")
            return None
        plan = {**o["plan"], "tansho": amt}
        if why:
            plan["tansho_skipped"] = why
        self.db.update_order(o["id"], chain_id=chain_id, plan=plan,
                             tickets=[_t(t) for t in tickets], points=len(tickets),
                             total=sum(t.amount for t in tickets),
                             unit_amount=amt or s["wide_amount"])
        o = self.db.get(o["id"])
        # 予算の確認（ワイド・3連複・複勝の分だけ。地方と中央で別々に数える。単勝と転がしは別の予算）
        limit = s["bet_limit"]
        used = self.db.bets_used(o["race_date"], o["org"], exclude_id=o["id"])
        this = strategy.bets_cost(o["tickets"])
        if used + this > limit and not o["plan"].get("allow_over"):
            where = "中央" if o["org"] == "jra" else "地方"
            self.db.set_status(o["id"], "held", f"{where}の本日の予算 {limit:,}円を超えます（使用済み {used:,}円＋今回 {this:,}円）")
            notify.send("予算超え・保留中", f"{race_label(o)} を買うと{where}の本日の予算 {limit:,}円を超えます"
                        f"（使用済み {used:,}円＋今回 {this:,}円）。アプリで「買う」か「見送る」を選んでください", "high")
            return None
        self.db.set_status(o["id"], "pending", "", expect=o["status"])
        return self.db.get(o["id"])

    def _prepare_roll(self, o: dict, s: dict) -> dict | None:
        chain = self.db.chain_for(o["race_date"], "roll", create=True)
        if chain["status"] != "active":
            self.db.set_status(o["id"], "cancelled", "本日の転がしは終了しました")
            return None
        others = [x for x in self.db.chain_orders(chain["id"]) if x["id"] != o["id"]]
        if any(x["status"] in ("voting", "done", "unknown") and x["payout"] is None for x in others):
            self.db.set_status(o["id"], "skipped", "前のレースの結果待ちのため見送りました")
            notify.send("見送り", f"{race_label(o)} は前のレースの払戻が未確定のため見送りました")
            return None
        rs = strategy.roll_stake(chain, s)
        if rs is None:
            self.db.update_chain(chain["id"], status="exhausted", message="資金を使い切りました")
            self.db.set_status(o["id"], "cancelled", "資金を使い切ったので本日は終了しました")
            return None
        stake, new_attempt = rs
        horse = o["plan"]["horse"]
        self.db.update_order(o["id"], chain_id=chain["id"], plan={**o["plan"], "new_attempt": new_attempt},
                             tickets=[{"bet_type": "複勝", "combo": [horse], "amount": stake}],
                             points=1, total=stake, unit_amount=stake)
        self.db.set_status(o["id"], "pending", "", expect=o["status"])
        return self.db.get(o["id"])

    # --- 投票 ---
    async def vote(self, o: dict, s: dict) -> None:
        if not self.db.set_status(o["id"], "voting", "投票中", expect="pending"):
            return
        mode = s["mode"] if (s["mode"] != "live" or config.allow_live) else "rehearsal"
        odds_rule = None
        if o["strategy"] == "holiday" and o["plan"].get("tansho"):
            odds_rule = {"horse": o["plan"]["honmei"], "min": s["tansho_min_odds"], "unknown": s["odds_unknown"]}
        if mode == "off":
            from .bots.engine import VoteResult
            res = VoteResult("done", f"練習：実際には投票していません（{o['points']}点 {o['total']:,}円）",
                             tickets=o["tickets"])
        else:
            try:
                async with self.bot_factory(SITES[o["org"]]) as bot:
                    await bot.login()
                    res = await bot.vote(o, mode, odds_rule)
            except Exception as e:
                from .bots.engine import VoteResult
                res = VoteResult("failed", f"ログインまたは起動に失敗: {e}")

        upd: dict = {}
        plan = dict(o["plan"])
        if mode == "off":
            plan["paper"] = True
        if res.tickets is not None and res.status != "failed":
            upd.update(tickets=res.tickets, points=len(res.tickets), total=sum(t["amount"] for t in res.tickets))
            if plan.get("tansho") and not any(t["bet_type"] == "単勝" for t in res.tickets):
                plan["tansho"] = 0
                plan["tansho_skipped"] = f"オッズ{res.odds}倍" if res.odds is not None else "オッズ不明"
        if res.odds is not None:
            plan["odds"] = res.odds
        upd["plan"] = plan
        if res.status in ("done", "unknown") and o["strategy"] != "manual":
            upd["next_check_at"] = (post_dt(o) + timedelta(minutes=s["result_delay_min"])).isoformat(timespec="seconds")
        self.db.update_order(o["id"], **upd)
        self.db.set_status(o["id"], res.status, res.message, res.shot)
        self.db.log(f"{race_label(o)} {res.status}: {res.message}", o["id"],
                    "error" if res.status in ("failed", "unknown") else "info")

        if o["strategy"] == "roll" and res.status in ("done", "unknown") and plan.get("new_attempt"):
            chain = self.db.chain_for(o["race_date"], "roll")
            self.db.update_chain(chain["id"], own_spent=chain["own_spent"] + o["total"],
                                 attempts=chain["attempts"] + 1)

        title = {"done": "投票しました", "rehearsed": "リハーサル完了", "failed": "投票失敗",
                 "unknown": "要確認", "skipped": "見送り"}.get(res.status, res.status)
        prio = "high" if res.status in ("failed", "unknown") else "default"
        body = f"{race_label(o)} {res.message}"
        notify.send(title, body, prio)

    # --- 結果 ---
    async def check_results(self, at: datetime) -> None:
        """結果を確認する。まずnetkeibaの払戻から計算し、だめなら（本番のみ）投票サイトの照会で確認する。"""
        orders = self.db.awaiting_results(at.isoformat(timespec="seconds"))
        live = self.db.settings()["mode"] == "live" and config.allow_live
        by_org: dict[str, list[dict]] = {}
        for o in orders:
            try:
                rl = await asyncio.to_thread(results.race_list, o["org"], o["race_date"], VENUES[o["org"]])
                info = rl.get((o["venue"], o["race_no"]))
                res = await asyncio.to_thread(results.race_result, o["org"], info["race_id"]) if info else None
            except Exception as e:
                log.warning("netkeibaから結果を読めません #%s: %s", o["id"], e)
                res = None
            if res and res.settled and res.has_all(o["tickets"]):
                self.apply_result(o, {"settled": 1, "payout": res.payout(o["tickets"]), "winner": res.winner})
            elif live:
                by_org.setdefault(o["org"], []).append(o)
            else:
                self._retry_later(o, at)
        for org, items in by_org.items():
            try:
                async with self.bot_factory(SITES[org]) as bot:
                    await bot.login()
                    for o in items:
                        try:
                            r = await bot.check_result(o)
                        except Exception as e:
                            log.warning("結果確認に失敗 #%s: %s", o["id"], e)
                            r = {}
                        if r.get("settled"):
                            r.setdefault("payout", 0)
                            self.apply_result(o, r)
                        else:
                            self._retry_later(o, at)
            except Exception as e:
                log.warning("結果確認のログインに失敗: %s", e)
                for o in items:
                    self._retry_later(o, at)

    def _retry_later(self, o: dict, at: datetime) -> None:
        if at - post_dt(o) > timedelta(minutes=RESULT_GIVE_UP_MIN):
            self.db.update_order(o["id"], next_check_at=None)
            notify.send("結果を入力してください", f"{race_label(o)} の払戻を自動で確認できませんでした。アプリで結果を入力してください", "high")
        else:
            self.db.update_order(o["id"], next_check_at=(at + timedelta(minutes=RESULT_RETRY_MIN)).isoformat(timespec="seconds"))

    def apply_result(self, o: dict, r: dict) -> str:
        """結果を反映する（自動確認と手入力の両方から呼ばれる）。通知した文を返す。"""
        payout = int(r.get("payout") or 0)
        self.db.update_order(o["id"], payout=payout, winner=r.get("winner"), next_check_at=None)
        msg = f"{race_label(o)} 払戻 {payout:,}円"
        if o["strategy"] == "roll":
            chain = self.db.chain_for(o["race_date"], "roll")
            upd = strategy.apply_roll_result(chain, payout, self.db.settings())
            self.db.update_chain(chain["id"], **upd)
            msg = f"{race_label(o)} {upd['message']}"
            if upd.get("status") in ("won", "exhausted"):
                self.cancel_rest(o["race_date"], "本日の転がしは終了しました")
        elif o["strategy"] == "holiday" and o["plan"].get("tansho"):
            hit = strategy.tansho_hit(o, r)
            chain = self.db.chain_for(o["race_date"], "tansho")
            if hit:
                self.db.update_chain(chain["id"], status="won",
                                     message=f"{race_label(o)} 単勝◎{o['plan']['honmei']}番 的中。本日の単勝は終了")
                msg = (f"{race_label(o)} 単勝的中！（払戻 {payout:,}円）"
                       "本日はこのあと単勝は買いません（ワイド・3連複・複勝は続けます）")
            elif hit is False:
                s = self.db.settings()
                losses = chain["losses"] + 1
                if losses >= s["tansho_max_losses"]:
                    self.db.update_chain(chain["id"], losses=losses, status="exhausted",
                                         message=f"単勝が{losses}回連続で外れたので、本日の単勝は終了")
                    msg = f"{race_label(o)} 単勝は外れ（払戻 {payout:,}円）。{losses}回連続の外れで、本日の単勝は終了"
                else:
                    self.db.update_chain(chain["id"], losses=losses)
                    nxt = s["tansho_base"] * (losses + 1)
                    msg = f"{race_label(o)} 単勝は外れ（払戻 {payout:,}円）。次の単勝は {nxt:,}円"
        self.db.log(msg, o["id"])
        notify.send("結果", msg)
        return msg

    def cancel_rest(self, race_date: str, reason: str) -> int:
        n = 0
        for x in self.db.list_orders(race_date):
            if x["status"] in ("queued", "pending", "held", "needs_time"):
                n += self.db.set_status(x["id"], "cancelled", reason, expect=x["status"])
        return n


def _t(t) -> dict:
    return {"bet_type": t.bet_type, "combo": list(t.combo), "amount": t.amount}
