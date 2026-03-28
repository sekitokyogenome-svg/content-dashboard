import base64
import csv
import io
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

import anthropic
import requests as req
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)

# ── GitHub API 設定 ─────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DASHBOARD_REPO = os.getenv("GITHUB_DASHBOARD_REPO", "sekitokyogenome-svg/content-dashboard")
ZENN_REPO = os.getenv("GITHUB_ZENN_REPO", "sekitokyogenome-svg/zenn-content")
GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}
ZENN_USER = "web_benriya"

# ── Threads 設定 ─────────────────────────────────────────────
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")


# ── GitHub API ヘルパー ──────────────────────────────────────

def gh_get_file(repo, path):
    """ファイル内容とSHAを取得"""
    resp = req.get(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers=GH_HEADERS,
    )
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    return None, None


def gh_put_file(repo, path, content, sha, message):
    """ファイルを作成または更新"""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    resp = req.put(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers=GH_HEADERS,
        json=payload,
    )
    return resp.json()


# ── キュー操作 ──────────────────────────────────────────────

def load_queue():
    content, sha = gh_get_file(DASHBOARD_REPO, "queue.json")
    if content:
        return json.loads(content), sha
    return [], None


def save_queue(queue, sha):
    content = json.dumps(queue, ensure_ascii=False, indent=2)
    gh_put_file(DASHBOARD_REPO, "queue.json", content, sha, "update: queue.json")


# ── テーマ読み込み ───────────────────────────────────────────

def load_themes():
    content, _ = gh_get_file(ZENN_REPO, "themes.csv")
    if not content:
        return []
    reader = csv.DictReader(io.StringIO(content))
    return list(reader)


# ── Threads API ─────────────────────────────────────────────

def post_to_threads(text):
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        return {"error": "Threads APIが設定されていません"}

    create_resp = req.post(
        f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads",
        params={"media_type": "TEXT", "text": text, "access_token": THREADS_ACCESS_TOKEN},
    )
    if create_resp.status_code != 200:
        return {"error": create_resp.text}

    container_id = create_resp.json().get("id")
    publish_resp = req.post(
        f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish",
        params={"creation_id": container_id, "access_token": THREADS_ACCESS_TOKEN},
    )
    return publish_resp.json()


# ── Zenn 公開（GitHub API経由）──────────────────────────────

def publish_to_zenn(filename, title):
    path = f"articles/{filename}"
    content, sha = gh_get_file(ZENN_REPO, path)
    if not content:
        return {"error": f"ファイルが見つかりません: {path}"}
    if "published: false" not in content:
        return {"error": "既に公開済みか、published フラグが見つかりません"}

    new_content = content.replace("published: false", "published: true", 1)
    result = gh_put_file(ZENN_REPO, path, new_content, sha, f"publish: {title}")
    if "content" in result:
        return {"success": True}
    return {"error": str(result)}


PROOFREAD_PROMPT = """
あなたはGA4・BigQuery・Claude Codeを専門とするWEBコンサルタントです。
以下のZenn記事を2つの観点で見直し、問題があれば修正した完全な記事を出力してください。

## 確認観点

**① 専門家としての正確性**
- GA4 BigQueryのフィールド名が正しいか（ga_session_id、collected_traffic_source.manual_medium 等）
- SQLクエリの構文・ロジックに誤りがないか
- Claude Code / MCP の説明が現時点で正確か
- 数字・事例が非現実的でないか

**② 文脈・流れの自然さ**
- 冒頭のペルソナの悩みと本文の内容が一致しているか
- 見出しの順序が論理的か
- 突然話題が変わる箇所がないか
- CTAへの導線が自然か

## 出力ルール
- フロントマターから末尾CTAまで、完全な記事をそのまま出力する
- published は false のままにする
- 修正箇所がない場合もそのまま全文出力する
- 説明や前置きは不要。記事本文のみ出力する
"""


# ── ルーティング ─────────────────────────────────────────────

@app.route("/")
def index():
    queue, _ = load_queue()
    pending = [i for i in queue if i["status"] == "pending"]
    done = [i for i in queue if i["status"] != "pending"]
    themes = load_themes()
    published_count = sum(1 for t in themes if t.get("published", "").upper() == "TRUE")
    return render_template("index.html", pending=pending, done=done,
                           themes=themes, published_count=published_count)


@app.route("/api/queue")
def api_queue():
    queue, _ = load_queue()
    return jsonify(queue)


@app.route("/api/themes")
def api_themes():
    return jsonify(load_themes())


@app.route("/api/approve/<item_id>", methods=["POST"])
def approve(item_id):
    queue, sha = load_queue()
    item = next((i for i in queue if i["id"] == item_id), None)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    zenn_result = publish_to_zenn(item["filename"], item["title"])
    if "error" in zenn_result:
        return jsonify({"error": f"Zenn公開失敗: {zenn_result['error']}"}), 500

    threads_result = post_to_threads(item["threads_post"])

    item["status"] = "published"
    item["published_at"] = datetime.now().isoformat()
    item["threads_result"] = threads_result
    save_queue(queue, sha)

    return jsonify({"success": True, "threads": threads_result})


@app.route("/api/reject/<item_id>", methods=["POST"])
def reject(item_id):
    queue, sha = load_queue()
    item = next((i for i in queue if i["id"] == item_id), None)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    item["status"] = "rejected"
    save_queue(queue, sha)
    return jsonify({"success": True})


@app.route("/api/proofread/<item_id>", methods=["POST"])
def proofread(item_id):
    queue, sha = load_queue()
    item = next((i for i in queue if i["id"] == item_id), None)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    # GitHubから記事を取得
    path = f"articles/{item['filename']}"
    content, file_sha = gh_get_file(ZENN_REPO, path)
    if not content:
        return jsonify({"error": f"記事ファイルが見つかりません: {path}"}), 404

    # Claude APIで校正
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": f"{PROOFREAD_PROMPT}\n\n---\n\n{content}"}],
    )
    corrected = message.content[0].text

    # 校正後の記事をGitHubに保存
    result = gh_put_file(ZENN_REPO, path, corrected, file_sha, f"proofread: {item['filename']}")
    if "content" not in result:
        return jsonify({"error": f"保存失敗: {result}"}), 500

    # article_previewを更新
    body = re.sub(r"^---[\s\S]+?---\n", "", corrected, count=1).strip()
    item["article_preview"] = body[:500]
    save_queue(queue, sha)

    return jsonify({"success": True, "article_preview": item["article_preview"]})


@app.route("/api/update_threads/<item_id>", methods=["POST"])
def update_threads(item_id):
    queue, sha = load_queue()
    item = next((i for i in queue if i["id"] == item_id), None)
    if not item:
        return jsonify({"error": "Item not found"}), 404

    data = request.get_json()
    item["threads_post"] = data.get("threads_post", item["threads_post"])
    save_queue(queue, sha)
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
