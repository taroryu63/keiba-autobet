"""スマホアプリ用のAPIサーバー。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import results, sim, strategy
from .betting import BET_TYPES, MAX_HORSE, VENUES, BetError, expand
from .parser import parse
from .config import ROOT, config
from .db import DB, now
from .scheduler import SALES_CLOSE_MIN, Runner, post_dt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    db = DB(config.data_dir / "keiba.db")
    runner = Runner(db)
    state.update(db=db, runner=runner)
    task = asyncio.create_task(runner.loop()) if not state.get("no_loop") else None
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


def db() -> DB:
    return state["db"]


# --- 認証（パスワード1つ、署名付きクッキー） ---
SESSION_DAYS = 30
_fails: dict[str, list[float]] = {}


def _sign(issued: int) -> str:
    mac = hmac.new(config.secret_key.encode(), f"v1:{issued}".encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{mac}"


def auth(request: Request) -> None:
    tok = request.cookies.get("sess", "")
    try:
        issued = int(tok.split(".")[0])
    except ValueError:
        raise HTTPException(401, "ログインしてください")
    if not hmac.compare_digest(tok, _sign(issued)) or time.time() - issued > SESSION_DAYS * 86400:
        raise HTTPException(401, "ログインしてください")


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "失敗が続いたため10分間ログインできません")
    if not hmac.compare_digest(body.password.encode(), config.app_password.encode()):
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "パスワードが違います")
    _fails.pop(ip, None)
    response.set_cookie("sess", _sign(int(time.time())), max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return {"ok": True}


# --- 状態 ---
def day_info(d: str) -> dict:
    s = db().settings()
    chains = {c["kind"]: c for c in db().chains_on(d)}
    c = chains.get("tansho") or {"status": "active", "losses": 0, "message": ""}
    tansho = {"status": c["status"], "losses": c["losses"], "message": c["message"],
              "next": s["tansho_base"] * (c["losses"] + 1)}
    c = chains.get("roll") or {"status": "active", "wins": 0, "stake": None, "own_spent": 0,
                               "attempts": 0, "carry": 0, "message": ""}
    roll = {k: c[k] for k in ("status", "wins", "stake", "own_spent", "attempts", "carry", "message")}
    roll["next"] = c["stake"] or s["roll_stake"]
    return {"date": d, "limit": s["bet_limit"], "tansho": tansho, "roll": roll,
            "used": {"jra": db().bets_used(d, "jra"), "nar": db().bets_used(d, "nar")}}


@app.get("/api/state", dependencies=[Depends(auth)])
def get_state(d: str | None = None):
    d = d or now().date().isoformat()
    return {
        "today": now().date().isoformat(),
        "day": day_info(d),
        "orders": db().list_orders(d),
        "settings": db().settings(),
        "allow_live": config.allow_live,
        "venues": VENUES,
        "bet_types": list(BET_TYPES),
    }


# --- 買い目の登録 ---
class OrderIn(BaseModel):
    strategy: str = Field(pattern="^(roll|holiday|manual)$")
    org: str = Field(pattern="^(jra|nar)$")
    race_date: str
    venue: str
    race_no: int = Field(ge=1, le=12)
    post_time: str = Field(default="", pattern=r"^(([01]\d|2[0-3]):[0-5]\d)?$")  # 空なら自動で取得
    horse: int | None = None          # 転がし
    honmei: int | None = None         # 休日 ◎
    stars: list[int] = []             # 休日 ★
    bet_type: str | None = None       # 自由入力
    method: str = "normal"
    columns: list[list[int]] = []
    unit_amount: int = 100


def _check_horse(n: int | None, label: str) -> int:
    if n is None or not 1 <= n <= MAX_HORSE:
        raise HTTPException(400, f"{label}の馬番を1〜{MAX_HORSE}で指定してください")
    return n


@app.post("/api/orders", dependencies=[Depends(auth)])
def create_order(body: OrderIn):
    oid = db().add_order(build_order(body))
    return {"id": oid, "order": db().get(oid)}


def build_order(body: OrderIn) -> dict:
    """入力を検証して、保存する買い目を作る。問題があればHTTPException。"""
    s = db().settings()
    try:
        d = date.fromisoformat(body.race_date)
    except ValueError:
        raise HTTPException(400, "日付が不正です")
    if body.venue not in VENUES[body.org]:
        raise HTTPException(400, "開催場が不正です")
    if body.strategy == "roll" and body.org != "nar":
        raise HTTPException(400, "複勝の転がしは地方競馬だけです")

    o = {"org": body.org, "race_date": body.race_date, "venue": body.venue, "race_no": body.race_no,
         "post_time": body.post_time, "strategy": body.strategy, "method": body.method,
         "columns": [], "unit_amount": 0, "points": 0, "total": 0, "tickets": [], "status": "queued"}
    if body.strategy == "roll":
        o.update(bet_type="複勝", plan={"horse": _check_horse(body.horse, "転がし")})
    elif body.strategy == "holiday":
        h = _check_horse(body.honmei, "◎") if body.honmei is not None else None
        stars = sorted({_check_horse(x, "★") for x in body.stars} - {h})
        if h is None and not stars:
            raise HTTPException(400, "◎か★を指定してください")
        o.update(bet_type="◎★", plan={"honmei": h, "stars": stars})
    else:
        try:
            tickets = expand(body.bet_type or "", body.method, body.columns, body.unit_amount)
        except BetError as e:
            raise HTTPException(400, str(e))
        o.update(bet_type=body.bet_type, columns=body.columns, unit_amount=body.unit_amount,
                 points=len(tickets), total=sum(t.amount for t in tickets), status="pending",
                 tickets=[{"bet_type": t.bet_type, "combo": list(t.combo), "amount": t.amount} for t in tickets])

    o["dedupe_key"] = f"{body.race_date}|{body.org}|{body.venue}|{body.race_no}|{body.strategy}"
    if body.strategy == "manual":
        o["dedupe_key"] += "|" + ",".join(f"{t['bet_type']}{t['combo']}" for t in o["tickets"])
    if db().find_duplicate(o["dedupe_key"]):
        raise HTTPException(409, "同じレースの予約がすでにあります。取り消してから登録し直してください")

    t = now()
    if not body.post_time:
        # 発走時刻はサーバーが netkeiba / 投票サイトから読み取る
        if d < t.date():
            raise HTTPException(400, "過去の日付です")
        o.update(vote_at="", status="needs_time")
        return o
    post = post_dt(o)
    if t >= post - timedelta(minutes=SALES_CLOSE_MIN):
        raise HTTPException(400, "このレースは締切を過ぎています")
    o["vote_at"] = max(post - timedelta(minutes=s["vote_offset_min"]), t).isoformat(timespec="seconds")
    return o


# --- 予想テキストの一括登録 ---
class TextIn(BaseModel):
    race_date: str
    text: str = Field(max_length=20000)


def _plans_for(r) -> list[tuple[str, str]]:
    """読み取ったレースから、[(買い方, 何を買うかの説明)] を作る。空なら買わない。"""
    s = db().settings()
    st = ".".join(map(str, r.stars))
    out = []
    for kind in strategy.plans_for(r.org, r.honmei, r.stars):
        if kind == "roll":
            out.append(("roll", f"複勝転がし ◎{r.honmei}"))
            continue
        parts = []
        if r.org == "jra" and r.honmei is not None:
            parts.append(f"単勝◎{r.honmei}")
        if r.honmei is not None and r.stars:
            parts.append(f"ワイド{r.honmei}→{st}（各{s['wide_amount']}円）")
            if len(r.stars) >= 2:
                n = len(r.stars) * (len(r.stars) - 1) // 2
                parts.append(f"3連複{r.honmei}→{st}（{n}点・各{s['sanrenpuku_amount']}円）")
        if r.stars:
            parts.append(f"複勝{st}（各{s['fukusho_amount']}円）")
        out.append(("holiday", " / ".join(parts)))
    return out


def _bets_cost(r) -> int:
    plan = {"honmei": r.honmei, "stars": r.stars}
    return strategy.bets_cost(strategy.bet_tickets(plan, db().settings(), 0)) if r.stars else 0


@app.post("/api/parse", dependencies=[Depends(auth)])
def parse_text(body: TextIn):
    """予想テキストを読み取って、何を買うかの確認用の一覧を返す（まだ登録しない）。"""
    pr = parse(body.text)
    rows = []
    cost = {"jra": 0, "nar": 0}
    tansho = 0
    for r in pr.races:
        plans = _plans_for(r)
        if any(k == "holiday" for k, _ in plans):
            cost[r.org] += _bets_cost(r)
        tansho += 1 if r.org == "jra" and r.honmei is not None else 0
        rows.append({"org": r.org, "venue": r.venue, "race_no": r.race_no, "post_time": r.post_time,
                     "honmei": r.honmei, "stars": r.stars,
                     "strategy": "+".join(k for k, _ in plans) or None,
                     "desc": " ＋ ".join(d for _, d in plans) or "買いません"})
    return {"rows": rows, "errors": pr.errors, "bets_cost": cost, "tansho_races": tansho,
            "limit": db().settings()["bet_limit"]}


@app.post("/api/bulk", dependencies=[Depends(auth)])
def bulk(body: TextIn):
    """予想テキストをまとめて予約する。登録できなかったレースは理由を返す。"""
    try:
        date.fromisoformat(body.race_date)
    except ValueError:
        raise HTTPException(400, "日付が不正です")
    pr = parse(body.text)
    if pr.errors:
        raise HTTPException(400, "読めない行があります: " + " / ".join(pr.errors))
    created, skipped = [], []
    for r in pr.races:
        label = f"{r.venue}{r.race_no}R"
        plans = _plans_for(r)
        if not plans:
            skipped.append(f"{label}: 買う馬券がありません")
        for strat, _ in plans:
            try:
                o = build_order(OrderIn(strategy=strat, org=r.org, race_date=body.race_date, venue=r.venue,
                                        race_no=r.race_no, post_time=r.post_time, horse=r.honmei,
                                        honmei=r.honmei, stars=r.stars))
            except HTTPException as e:
                skipped.append(f"{label}: {e.detail}")
                continue
            created.append(db().add_order(o))
    return {"created": len(created), "skipped": skipped}


class TimeIn(BaseModel):
    post_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


@app.post("/api/orders/{oid}/time", dependencies=[Depends(auth)])
async def set_time(oid: int, body: TimeIn):
    """発走時刻の手入力。"""
    o = _order(oid)
    if o["status"] not in ("needs_time", "queued", "pending"):
        raise HTTPException(409, "時刻を変更できる状態ではありません")
    async with state["runner"]._lock:
        o = _order(oid)
        if o["status"] != "needs_time":
            db().set_status(oid, "needs_time", "", expect=o["status"])
        state["runner"].set_post_time(_order(oid), body.post_time, db().settings()["vote_offset_min"], now())
    return {"ok": True, "order": _order(oid)}


# --- 仮想収支 ---
def _run_sim(race_date: str, text: str) -> dict:
    pr = parse(text)
    lists: dict[str, dict] = {}
    res_map: dict = {}
    races = []
    notes = list(pr.errors)
    for r in pr.races:
        if r.org not in lists:
            try:
                lists[r.org] = results.race_list(r.org, race_date, VENUES[r.org])
            except Exception as e:
                lists[r.org] = {}
                notes.append(f"{'中央' if r.org == 'jra' else '地方'}のレース一覧を取得できません: {e}")
        info = lists[r.org].get((r.venue, r.race_no))
        post_time = r.post_time or (info or {}).get("post_time", "")
        if not post_time:
            notes.append(f"{r.venue}{r.race_no}R の発走時刻が分かりません")
            continue
        if info:
            try:
                res_map[(r.venue, r.race_no)] = results.race_result(r.org, info["race_id"])
            except Exception as e:
                notes.append(f"{r.venue}{r.race_no}R の結果を取得できません: {e}")
        races.append({"org": r.org, "venue": r.venue, "race_no": r.race_no, "post_time": post_time,
                      "honmei": r.honmei, "stars": r.stars})
    report = sim.simulate(race_date, races, res_map, db().settings())
    report["notes"] = notes
    return report


@app.post("/api/sims", dependencies=[Depends(auth)])
async def create_sim(body: TextIn):
    try:
        date.fromisoformat(body.race_date)
    except ValueError:
        raise HTTPException(400, "日付が不正です")
    report = await asyncio.to_thread(_run_sim, body.race_date, body.text)
    db().save_sim(body.race_date, body.text, report)
    return report


@app.get("/api/sims", dependencies=[Depends(auth)])
def list_sims():
    return db().sims()


@app.delete("/api/sims/{race_date}", dependencies=[Depends(auth)])
def delete_sim(race_date: str):
    db().delete_sim(race_date)
    return {"ok": True}


@app.post("/api/preview", dependencies=[Depends(auth)])
def preview(body: OrderIn):
    try:
        tickets = expand(body.bet_type or "", body.method, body.columns, body.unit_amount)
    except BetError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "points": len(tickets), "total": sum(t.amount for t in tickets),
            "labels": [t.label() for t in tickets[:30]]}


def _order(oid: int) -> dict:
    o = db().get(oid)
    if not o:
        raise HTTPException(404, "見つかりません")
    return o


@app.post("/api/orders/{oid}/cancel", dependencies=[Depends(auth)])
def cancel(oid: int):
    o = _order(oid)
    if o["status"] not in ("queued", "pending", "held", "needs_time") or not db().set_status(oid, "cancelled", "取り消しました", expect=o["status"]):
        raise HTTPException(409, "この買い目は取り消せません（投票中または投票済み）")
    return {"ok": True}


@app.post("/api/orders/{oid}/approve", dependencies=[Depends(auth)])
def approve(oid: int):
    """上限を超えて保留になった買い目を、上限を超えても買う。"""
    o = _order(oid)
    if o["status"] != "held":
        raise HTTPException(409, "保留中ではありません")
    db().update_order(oid, plan={**o["plan"], "allow_over": True})
    db().set_status(oid, "queued" if o["strategy"] != "manual" else "pending", "上限を超えて買います", expect="held")
    return {"ok": True}


class ResultIn(BaseModel):
    payout: int = Field(ge=0, default=0)
    winner: int | None = None
    tansho_hit: bool | None = None


@app.post("/api/orders/{oid}/result", dependencies=[Depends(auth)])
async def set_result(oid: int, body: ResultIn):
    """結果の手入力（自動で確認できなかったとき、練習モードのとき）。"""
    o = _order(oid)
    if o["status"] != "done" or o["payout"] is not None:
        raise HTTPException(409, "結果を入力できる状態ではありません")
    r = {"payout": body.payout}
    if body.winner is not None:
        r["winner"] = body.winner
    if body.tansho_hit is not None:
        r["tansho_hit"] = body.tansho_hit
    if o["strategy"] == "holiday" and o["plan"].get("tansho") and "winner" not in r and "tansho_hit" not in r:
        raise HTTPException(400, "単勝が当たったかどうかを入力してください")
    async with state["runner"]._lock:
        msg = state["runner"].apply_result(_order(oid), r)
    return {"ok": True, "message": msg}


class ResolveIn(BaseModel):
    bought: bool


@app.post("/api/orders/{oid}/resolve", dependencies=[Depends(auth)])
def resolve(oid: int, body: ResolveIn):
    """「要確認」の買い目を、購入履歴を見て「買えていた／買えていなかった」に確定する。"""
    o = _order(oid)
    if o["status"] != "unknown":
        raise HTTPException(409, "要確認の買い目ではありません")
    if body.bought:
        db().set_status(oid, "done", "購入履歴で確認済み", expect="unknown")
        if o["strategy"] != "manual" and o["payout"] is None:
            post = post_dt(o) + timedelta(minutes=db().settings()["result_delay_min"])
            db().update_order(oid, next_check_at=max(post, now()).isoformat(timespec="seconds"))
    else:
        db().set_status(oid, "failed", "購入されていなかったことを確認", expect="unknown")
        if o["strategy"] == "roll" and o["plan"].get("new_attempt") and o["chain_id"]:
            c = db().chain_for(o["race_date"], "roll")
            db().update_chain(c["id"], own_spent=max(0, c["own_spent"] - o["total"]), attempts=max(0, c["attempts"] - 1))
    return {"ok": True}


# --- 設定 ---
class SettingsIn(BaseModel):
    mode: str | None = Field(default=None, pattern="^(off|rehearsal|live)$")
    vote_offset_min: int | None = Field(default=None, ge=3, le=60)
    roll_budget: int | None = Field(default=None, ge=0, le=1_000_000)
    roll_stake: int | None = Field(default=None, ge=100, le=100_000)
    roll_target: int | None = Field(default=None, ge=1, le=20)
    tansho_base: int | None = Field(default=None, ge=100, le=100_000)
    tansho_max_losses: int | None = Field(default=None, ge=1, le=30)
    tansho_min_odds: float | None = Field(default=None, ge=1.0, le=100)
    odds_unknown: str | None = Field(default=None, pattern="^(skip|buy)$")
    pending_policy: str | None = Field(default=None, pattern="^(assume_loss|skip)$")
    bet_limit: int | None = Field(default=None, ge=0, le=1_000_000)
    wide_amount: int | None = Field(default=None, ge=100, le=100_000)
    sanrenpuku_amount: int | None = Field(default=None, ge=100, le=100_000)
    fukusho_amount: int | None = Field(default=None, ge=100, le=100_000)
    result_delay_min: int | None = Field(default=None, ge=3, le=60)
    paused: bool | None = None


@app.put("/api/settings", dependencies=[Depends(auth)])
def put_settings(body: SettingsIn):
    vals = body.model_dump(exclude_none=True)
    if vals.get("mode") == "live" and not config.allow_live:
        raise HTTPException(400, "本番投票はサーバーの .env で ALLOW_LIVE=1 にしたときだけ選べます")
    for k in ("roll_budget", "roll_stake", "tansho_base", "bet_limit", "wide_amount", "sanrenpuku_amount", "fukusho_amount"):
        if k in vals and vals[k] % 100:
            raise HTTPException(400, "金額は100円単位で指定してください")
    return db().save_settings(vals)


class CheckIn(BaseModel):
    site: str = Field(pattern="^(ipat|spat4)$")
    venue: str
    race_no: int = Field(ge=1, le=12)
    horse: int = Field(ge=1, le=MAX_HORSE)


@app.post("/api/check", dependencies=[Depends(auth)])
async def check_site(body: CheckIn):
    """接続テスト（購入はしない）。"""
    from .calibration import run_check
    from .scheduler import credentials

    creds = credentials(body.site)
    if not all(creds.values()):
        raise HTTPException(400, "ログイン情報がサーバーに設定されていません（セットアップをやり直してください）")
    runner = state["runner"]
    async with runner._lock:  # 投票と同時に動かない
        steps = await run_check(runner.bot_factory(body.site), body.venue, body.race_no, body.horse)
    db().log(f"接続テスト {body.site}: " + " / ".join(f"{x['label']}{'○' if x['ok'] else '×'}" for x in steps))
    return {"steps": steps, "ok": all(x["ok"] for x in steps) and {"confirm", "many"} <= {x["stage"] for x in steps}}


@app.get("/api/logs", dependencies=[Depends(auth)])
def logs():
    return db().logs(100)


@app.get("/api/shots/{name}", dependencies=[Depends(auth)])
def shot(name: str):
    path = (config.data_dir / "shots" / name).resolve()
    if path.parent != (config.data_dir / "shots").resolve() or not path.exists():
        raise HTTPException(404, "見つかりません")
    return FileResponse(path)


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """スマホのブラウザが古い画面を使い続けないよう、毎回サーバーに確認させる。"""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="static")
