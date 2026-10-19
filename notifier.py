#!/usr/bin/env python3
"""ChatWork の自分宛てメンション / 自分担当タスクを Slack に転送する.

認証情報は環境変数 CHATWORK_API_TOKEN / SLACK_WEBHOOK_URL を優先して読む
(GitHub Actions の secrets 用)。無ければ同じディレクトリの config.json を読む:
{
  "chatwork_api_token": "xxxx",
  "slack_webhook_url": "https://hooks.slack.com/services/xxx/yyy/zzz"
}

状態 (既読管理) は state.json に保存される。
"""
import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "notifier.log")

CHATWORK_API_BASE = "https://api.chatwork.com/v2"


def log(message):
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def cw_request(path, token, params=None):
    url = f"{CHATWORK_API_BASE}{path}"
    if params:
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"X-ChatWorkToken": token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        if not raw.strip():
            return []
        return json.loads(raw)


def slack_post(webhook_url, text):
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def strip_chatwork_markup(body):
    import re
    body = re.sub(r"\[To:\d+\][^\n]*\n?", "", body)
    body = re.sub(r"\[rp aid=\d+ to=\d+-\d+\][^\n]*\n?", "", body)
    body = re.sub(r"\[/?qt\]", "", body)
    body = re.sub(r"\[qtmeta[^\]]*\]", "", body)
    return body.strip()


def main():
    token = os.environ.get("CHATWORK_API_TOKEN")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not token or not webhook_url:
        config = load_json(CONFIG_PATH, None) or {}
        token = token or config.get("chatwork_api_token")
        webhook_url = webhook_url or config.get("slack_webhook_url")

    if not token or not webhook_url or token == "YOUR_CHATWORK_API_TOKEN":
        log("ERROR: 認証情報が未設定です。環境変数 CHATWORK_API_TOKEN / SLACK_WEBHOOK_URL か config.json を設定してください。")
        sys.exit(1)

    state = load_json(STATE_PATH, {"account_id": None, "seen_task_ids": []})

    try:
        if not state.get("account_id"):
            me = cw_request("/me", token)
            state["account_id"] = me["account_id"]
            log(f"account_id を取得しました: {state['account_id']}")

        account_id = state["account_id"]
        mention_tag = f"[To:{account_id}]"
        seen_task_ids = set(state.get("seen_task_ids", []))

        # --- 自分宛てメンションのチェック ---
        rooms = cw_request("/rooms", token)
        for room in rooms:
            if room.get("mention_num", 0) <= 0:
                continue
            room_id = room["room_id"]
            room_name = room.get("name", str(room_id))
            try:
                messages = cw_request(f"/rooms/{room_id}/messages", token, {"force": 0})
            except urllib.error.HTTPError as e:
                log(f"WARN: room {room_id} のメッセージ取得に失敗: {e}")
                continue
            if not messages:
                continue
            for msg in messages:
                body = msg.get("body", "")
                if mention_tag not in body:
                    continue
                sender = msg.get("account", {}).get("name", "unknown")
                clean_body = strip_chatwork_markup(body) or "(本文なし)"
                text = (
                    f":bell: *ChatWork メンション*\n"
                    f"*ルーム:* {room_name}\n"
                    f"*送信者:* {sender}\n"
                    f"{clean_body}"
                )
                slack_post(webhook_url, text)
                log(f"メンション通知を送信: room={room_name} sender={sender}")

        # --- 自分担当タスクのチェック ---
        tasks = cw_request("/my/tasks", token, {"status": "open"})
        for task in tasks:
            task_id = str(task["task_id"])
            if task_id in seen_task_ids:
                continue
            room_name = task.get("room", {}).get("name", "")
            assigner = task.get("assigned_by_account", {}).get("name", "unknown")
            body = task.get("body", "")
            limit_time = task.get("limit_time", 0)
            limit_str = (
                datetime.fromtimestamp(limit_time).strftime("%Y-%m-%d %H:%M")
                if limit_time
                else "期限なし"
            )
            text = (
                f":memo: *ChatWork タスク*\n"
                f"*ルーム:* {room_name}\n"
                f"*依頼者:* {assigner}\n"
                f"*期限:* {limit_str}\n"
                f"{body}"
            )
            slack_post(webhook_url, text)
            seen_task_ids.add(task_id)
            log(f"タスク通知を送信: room={room_name} task_id={task_id}")

        state["seen_task_ids"] = list(seen_task_ids)
        save_json(STATE_PATH, state)

    except urllib.error.HTTPError as e:
        log(f"ERROR: ChatWork API HTTPError: {e.code} {e.read()}")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
