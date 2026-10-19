#!/usr/bin/env python3
"""WorkBuddy「Buddy 加油站」签到 + 派猫猫旅行，结果推送飞书。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_BASE = "https://www.workbuddy.cn"
CST = timezone(timedelta(hours=8))
LOCATION_ID = int(os.environ.get("WB_TRAVEL_LOCATION_ID") or "1")
MAX_RETRIES = 3


def now_cst() -> datetime:
    return datetime.now(CST)


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def uid_from_token(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return str(claims.get("sub") or "")
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""


class ApiError(RuntimeError):
    pass


class WorkBuddy:
    def __init__(self, base: str, token: str, uid: str, domain: str = "", enterprise_id: str = ""):
        self.base = base.rstrip("/")
        self.token = token
        self.uid = uid
        self.domain = domain
        self.enterprise_id = enterprise_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-User-Id": self.uid,
            "X-Client-Platform": "web",
            "X-Product-Code": "workbuddy",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.domain:
            headers["X-Domain"] = self.domain
        if self.enterprise_id:
            headers["X-Enterprise-Id"] = self.enterprise_id
            headers["X-Tenant-Id"] = self.enterprise_id
        return headers

    def call(self, method: str, path: str, body: dict | None = None, timeout: int = 25) -> tuple[int, dict]:
        data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(1, MAX_RETRIES + 1):
            req = urllib.request.Request(
                self.base + path,
                data=data,
                headers=self._headers(),
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
                    return response.status, parse_json(raw)
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", "replace")
                payload = parse_json(raw)
                if error.code == 429 or 500 <= error.code < 600:
                    last_error = f"HTTP {error.code}: {compact(payload)}"
                else:
                    return error.code, payload
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = str(error)

            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)

        raise ApiError(last_error or "request failed")


def parse_json(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except ValueError:
        return {"code": -1, "msg": "non-JSON response", "raw": raw[:500]}
    return value if isinstance(value, dict) else {"code": -1, "msg": "unexpected response", "data": value}


def compact(payload: dict, limit: int = 300) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if any(part in key.lower() for part in ("token", "authorization", "secret", "cookie")):
                result[key] = "***"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def ok(payload: dict) -> bool:
    return payload.get("code") == 0


def checkin(client: WorkBuddy) -> dict:
    result: dict = {"ok": False, "action": "unknown", "raw": []}
    try:
        status_code, status = client.call("POST", "/v2/billing/meter/checkin-activity-status")
        result["raw"].append({"endpoint": "POST /v2/billing/meter/checkin-activity-status", "http": status_code, "body": status})
        if status_code != 200 or not ok(status):
            result.update(action="status_failed", error=f"HTTP {status_code} {compact(status)}")
            return result

        data = status.get("data") or {}
        result.update(
            today_checked_in=bool(data.get("today_checked_in")),
            active=bool(data.get("active")),
            streak_days=data.get("streak_days", 0),
            today_credit=data.get("today_credit", 0),
            total_credits=data.get("total_credits", 0),
            theme_name=data.get("theme_name", ""),
            season=data.get("season", 0),
            checkin_dates=data.get("checkin_dates") or [],
        )

        if data.get("today_checked_in"):
            result.update(ok=True, action="already_checked_in")
            return result
        if not data.get("active"):
            result.update(action="activity_inactive", error="签到活动未开启")
            return result

        claim_code, claim = client.call("POST", "/v2/billing/meter/daily-checkin")
        result["raw"].append({"endpoint": "POST /v2/billing/meter/daily-checkin", "http": claim_code, "body": claim})
        if claim_code == 200 and ok(claim):
            claim_data = claim.get("data") or {}
            result.update(
                ok=True,
                action="claimed",
                today_credit=claim_data.get("today_credit", data.get("today_credit", 0)),
                streak_days=claim_data.get("streak_days", data.get("streak_days", 0)),
                total_credits=claim_data.get("total_credits", data.get("total_credits", 0)),
            )
            return result

        msg = str(claim.get("msg") or "")
        if "already" in msg.lower() or "已签到" in msg or "已领取" in msg:
            result.update(ok=True, action="already_checked_in")
        else:
            result.update(action="claim_failed", error=f"HTTP {claim_code} {compact(claim)}")
        return result
    except ApiError as error:
        result.update(action="request_failed", error=str(error))
        return result


def travel_status(client: WorkBuddy, raw: list[dict]) -> tuple[int, dict, dict]:
    code, payload = client.call("GET", "/v2/activity/growth/buddy/travel/status")
    raw.append({"endpoint": "GET /v2/activity/growth/buddy/travel/status", "http": code, "body": payload})
    return code, payload, payload.get("data") or {}


def travel(client: WorkBuddy) -> dict:
    result: dict = {"ok": False, "action": "unknown", "raw": []}
    try:
        code, payload, data = travel_status(client, result["raw"])
        if code != 200 or not ok(payload):
            result.update(action="status_failed", error=f"HTTP {code} {compact(payload)}")
            return result

        state = data.get("state", "unknown")
        server_now = int(data.get("server_now") or time.time())
        arrive_at = int(data.get("arrive_at") or 0)
        result.update(
            state_before=state,
            daily_limit_reached=bool(data.get("daily_limit_reached")),
            location=(data.get("location") or {}).get("name") if isinstance(data.get("location"), dict) else None,
            arrive_at=arrive_at,
        )

        arrived = state == "arrived" or (state == "traveling" and arrive_at > 0 and server_now >= arrive_at)
        if arrived:
            claim_code, claim = client.call("POST", "/v2/activity/growth/buddy/travel/claim", {})
            result["raw"].append({"endpoint": "POST /v2/activity/growth/buddy/travel/claim", "http": claim_code, "body": claim})
            if claim_code == 200 and ok(claim):
                claim_data = claim.get("data") or {}
                result.update(claimed=True, claimed_credit=claim_data.get("reward_credit", 0), state_after_claim=claim_data.get("state"))
            else:
                msg = str(claim.get("msg") or "")
                if "no unclaimed" in msg.lower() or "没有" in msg or "已领" in msg:
                    result.update(claimed=False, claim_note="没有待领取的旅行积分")
                elif "not arrived" in msg.lower() or "未到达" in msg:
                    result.update(claimed=False, claim_note="尚未到达，暂不领取")
                else:
                    result.update(action="claim_failed", error=f"HTTP {claim_code} {compact(claim)}")
                    return result

        if arrived:
            code, payload, data = travel_status(client, result["raw"])
            if code != 200 or not ok(payload):
                result.update(action="status_failed_after_claim", error=f"HTTP {code} {compact(payload)}")
                return result

        state = data.get("state", "unknown")
        result.update(
            state_after=state,
            daily_limit_reached=bool(data.get("daily_limit_reached")),
            next_arrive_at=int(data.get("arrive_at") or 0),
            location=(data.get("location") or {}).get("name") if isinstance(data.get("location"), dict) else None,
        )

        if state == "traveling":
            result.update(ok=True, action="already_traveling")
            return result
        if data.get("daily_limit_reached"):
            result.update(ok=True, action="daily_limit_reached")
            return result
        if state == "arrived":
            result.update(action="arrived_unclaimed", error="已到达但积分未领取成功，跳过派发")
            return result
        if state != "idle":
            result.update(action="unknown_state", error=f"未知状态: {state}")
            return result

        depart_code, depart = client.call("POST", "/v2/activity/growth/buddy/travel/depart", {"location_id": LOCATION_ID})
        result["raw"].append({"endpoint": "POST /v2/activity/growth/buddy/travel/depart", "http": depart_code, "body": depart})
        if depart_code == 200 and ok(depart):
            depart_data = depart.get("data") or {}
            result.update(
                ok=True,
                action="dispatched",
                state_after=depart_data.get("state", "traveling"),
                depart_at=int(depart_data.get("depart_at") or 0),
                next_arrive_at=int(depart_data.get("arrive_at") or 0),
                location=(depart_data.get("location") or {}).get("name") if isinstance(depart_data.get("location"), dict) else None,
            )
            return result

        result.update(action="dispatch_failed", error=f"HTTP {depart_code} {compact(depart)}")
        return result
    except ApiError as error:
        result.update(action="request_failed", error=str(error))
        return result


def fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M")


def build_card(date_str: str, checkin_result: dict, travel_result: dict) -> dict:
    checkin_ok = checkin_result.get("ok")
    action = checkin_result.get("action")
    if action == "claimed":
        checkin_title = "✅ 签到成功"
        checkin_detail = f"本次领取 **{checkin_result.get('today_credit', 0)}** 积分"
    elif action == "already_checked_in":
        checkin_title = "✅ 今日已签到"
        checkin_detail = f"今日积分 **{checkin_result.get('today_credit', 0)}**"
    elif action == "activity_inactive":
        checkin_title = "⚠️ 签到活动未开启"
        checkin_detail = checkin_result.get("error", "")
    else:
        checkin_title = "❌ 签到失败"
        checkin_detail = checkin_result.get("error", "")

    checkin_lines = [
        f"**{checkin_title}**",
        checkin_detail,
        f"连续签到 **{checkin_result.get('streak_days', 0)}** 天 · 累计 **{checkin_result.get('total_credits', 0)}** 积分",
    ]
    dates = checkin_result.get("checkin_dates") or []
    if dates:
        checkin_lines.append(f"最近签到：{', '.join(dates[:5])}")

    t_action = travel_result.get("action")
    if t_action == "dispatched":
        travel_title = "🐱 已派猫猫出发"
        travel_detail = f"目的地：**{travel_result.get('location') or '咖啡馆'}** · 预计 **{fmt_ts(travel_result.get('next_arrive_at', 0))}** 到达"
    elif t_action == "already_traveling":
        travel_title = "🐱 今天已经派过，正在旅行"
        travel_detail = f"目的地：**{travel_result.get('location') or '-'}** · 预计 **{fmt_ts(travel_result.get('next_arrive_at', 0))}** 到达"
    elif t_action == "daily_limit_reached":
        travel_title = "🐱 今日派发次数已用完"
        travel_detail = "猫猫今天已经旅行过一趟，不再派发"
    elif t_action == "claim_failed":
        travel_title = "⚠️ 领取旅行积分失败"
        travel_detail = travel_result.get("error", "")
    elif t_action == "arrived_unclaimed":
        travel_title = "⚠️ 已到家但积分未领取成功"
        travel_detail = travel_result.get("error", "")
    elif t_action == "dispatch_failed":
        travel_title = "⚠️ 派猫猫失败"
        travel_detail = travel_result.get("error", "")
    elif t_action == "status_failed":
        travel_title = "⚠️ 旅行状态查询失败"
        travel_detail = travel_result.get("error", "")
    else:
        travel_title = "🐱 猫猫旅行"
        travel_detail = travel_result.get("error") or f"当前状态：{travel_result.get('state_after') or travel_result.get('state_before') or 'unknown'}"

    travel_lines = [f"**{travel_title}**", travel_detail]
    if travel_result.get("claimed"):
        travel_lines.append(f"已先领取到家积分 **{travel_result.get('claimed_credit', 0)}**")
    if travel_result.get("daily_limit_reached") and t_action not in ("daily_limit_reached", "already_traveling"):
        travel_lines.append("今日派发次数已用完，未再派发")
    elif travel_result.get("ok") and t_action not in ("dispatched", "already_traveling"):
        travel_lines.append("判断结果：今天不派新的一趟")

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green" if checkin_ok else "red",
                "title": {"tag": "plain_text", "content": f"WorkBuddy 每日任务 · {date_str}"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(checkin_lines)}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(travel_lines)}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "签到与猫猫旅行独立执行；猫猫失败不影响签到结论。"}]},
            ],
        },
    }


def push_feishu(webhook: str, payload: dict, secret: str = "") -> tuple[bool, str]:
    if not webhook:
        return False, "未配置 FEISHU_WEBHOOK"
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        payload = {"timestamp": timestamp, "sign": base64.b64encode(digest).decode("utf-8"), **payload}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", "replace")
            body = parse_json(raw)
            if response.status == 200 and body.get("code", 0) == 0:
                return True, compact(body, 300)
            return False, f"HTTP {response.status} {compact(body, 300)}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code} {error.read().decode('utf-8', 'replace')[:300]}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, str(error)


def write_summary(path: str, date_str: str, checkin_result: dict, travel_result: dict, feishu_ok: bool, feishu_msg: str) -> None:
    lines = [
        f"## WorkBuddy 每日任务 · {date_str}",
        "",
        "### 签到",
        f"- 结果：`{checkin_result.get('action')}` / ok={checkin_result.get('ok')}",
        f"- 连续天数：{checkin_result.get('streak_days', 0)}",
        f"- 今日积分：{checkin_result.get('today_credit', 0)}",
        f"- 累计积分：{checkin_result.get('total_credits', 0)}",
    ]
    if checkin_result.get("error"):
        lines.append(f"- 错误：{checkin_result['error']}")
    lines += [
        "",
        "### 猫猫旅行",
        f"- 结果：`{travel_result.get('action')}` / ok={travel_result.get('ok')}",
        f"- 状态：{travel_result.get('state_before')} → {travel_result.get('state_after')}",
        f"- 今日已派：{travel_result.get('daily_limit_reached')}",
        f"- 领取积分：{travel_result.get('claimed_credit', 0)}",
        f"- 下次到达：{fmt_ts(travel_result.get('next_arrive_at', 0))}",
    ]
    if travel_result.get("error"):
        lines.append(f"- 错误：{travel_result['error']}")
    lines += ["", "### 飞书", f"- 推送：{'成功' if feishu_ok else '失败'}", f"- 返回：{feishu_msg}"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印飞书 payload，不实际发送")
    parser.add_argument("--json-out", default="result.json")
    parser.add_argument("--summary-out", default="result.md")
    args = parser.parse_args()

    token = os.environ.get("WB_ACCESS_TOKEN", "").strip()
    uid = os.environ.get("WB_USER_ID", "").strip() or uid_from_token(token)
    if not token:
        print("缺少 WB_ACCESS_TOKEN", file=sys.stderr)
        return 2
    if not uid:
        print("缺少 WB_USER_ID，且无法从 token 中解析 uid", file=sys.stderr)
        return 2

    base = os.environ.get("WB_API_BASE", DEFAULT_BASE).strip() or DEFAULT_BASE
    client = WorkBuddy(
        base=base,
        token=token,
        uid=uid,
        domain=os.environ.get("WB_DOMAIN", "").strip(),
        enterprise_id=os.environ.get("WB_ENTERPRISE_ID", "").strip(),
    )

    date_str = now_cst().strftime("%Y-%m-%d")
    print(f"[{now_cst().isoformat(timespec='seconds')}] uid={mask(uid)} base={base}")

    try:
        checkin_result = checkin(client)
    except Exception as error:
        checkin_result = {"ok": False, "action": "exception", "error": str(error), "raw": []}

    print(f"checkin: action={checkin_result.get('action')} ok={checkin_result.get('ok')}")
    for item in checkin_result.get("raw", []):
        print(f"  {item['endpoint']} -> HTTP {item['http']}")
        print(json.dumps(redact(item["body"]), ensure_ascii=False, indent=2))

    try:
        travel_result = travel(client)
    except Exception as error:
        travel_result = {"ok": False, "action": "exception", "error": str(error), "raw": []}

    print(f"travel: action={travel_result.get('action')} ok={travel_result.get('ok')} state={travel_result.get('state_before')} -> {travel_result.get('state_after')}")
    for item in travel_result.get("raw", []):
        print(f"  {item['endpoint']} -> HTTP {item['http']}")
        print(json.dumps(redact(item["body"]), ensure_ascii=False, indent=2))

    payload = build_card(date_str, checkin_result, travel_result)
    feishu_ok = False
    feishu_msg = "dry-run"
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not args.dry_run:
        feishu_ok, feishu_msg = push_feishu(webhook, payload, os.environ.get("FEISHU_SECRET", "").strip())
        if webhook:
            feishu_msg = feishu_msg.replace(webhook, "***")
        print(f"feishu: ok={feishu_ok} {feishu_msg}")
    else:
        print("feishu payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    redacted = redact({
        "date": date_str,
        "checkin": checkin_result,
        "travel": travel_result,
        "feishu": {"ok": feishu_ok, "message": feishu_msg},
        "success": bool(checkin_result.get("ok")),
    })
    with open(args.json_out, "w", encoding="utf-8") as handle:
        json.dump(redacted, handle, ensure_ascii=False, indent=2)
    write_summary(args.summary_out, date_str, checkin_result, travel_result, feishu_ok, feishu_msg)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(open(args.summary_out, encoding="utf-8").read())

    if not checkin_result.get("ok"):
        print("签到失败，退出码 1", file=sys.stderr)
        return 1

    if not feishu_ok and not args.dry_run:
        print(f"::warning::签到成功，但飞书推送失败: {feishu_msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
