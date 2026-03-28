"""
記事生成スクリプト
- themes.csvから次のテーマを取得（GitHub API）
- Claude APIで記事とThreads投稿文を生成
- zenn-content/articles/ に保存（GitHub API、published: false）
- queue.json にレビュー待ちエントリを追加（GitHub API）

実行方法:
  python generate_content.py

環境変数:
  ANTHROPIC_API_KEY       : Claude APIキー
  GITHUB_TOKEN            : GitHub Personal Access Token（repo スコープ）
  GITHUB_DASHBOARD_REPO   : content-dashboard リポジトリ名（例: user/content-dashboard）
  GITHUB_ZENN_REPO        : zenn-content リポジトリ名（例: user/zenn-content）
"""

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

load_dotenv(Path(__file__).parent / ".env")

ZENN_USER = "web_benriya"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DASHBOARD_REPO = os.getenv("GITHUB_DASHBOARD_REPO", "sekitokyogenome-svg/content-dashboard")
ZENN_REPO = os.getenv("GITHUB_ZENN_REPO", "sekitokyogenome-svg/zenn-content")
GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

ARTICLE_SYSTEM_PROMPT = """
あなたはGA4・BigQuery・Claude Codeを専門とするWEBコンサルタントです。
中小EC経営者やWEBマーケター向けにZennで技術記事を書いています。

記事を書く際のルール：
- Zennのフロントマター（title/emoji/type/topics/published）を必ず含める
- published は必ず false にする
- 本文は1,500〜2,500字
- 冒頭はペルソナの悩みから始める
- 技術系（type:tech）ならSQLやコードを必ず含める
- 事例系（type:idea）なら具体的な数字やビフォーアフターを含める
- 末尾にococナラサービスへの誘導CTAを入れる（URLは https://coconala.com/services/1791205）
- Zenn Markdown記法（:::message等）を適切に使う
- NG表現：「必ず〜」「確実に〜」「最短で〜」

GA4 BigQueryのSQLで使う正しいフィールド名：
- セッションID: ga_session_id（UNNEST(event_params)から取得）
- チャネル: collected_traffic_source.manual_medium / manual_source
- セッション識別: CONCAT(user_pseudo_id, CAST((SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS STRING))
"""

THREADS_SYSTEM_PROMPT = """
Threads（@delta11235813）への投稿文を作成してください。

ルール：
- 500文字以内
- 冒頭に【新記事】または絵文字で始める
- 記事の一番の価値を1〜2文で伝える
- 読者の悩み・ベネフィットを明示する
- ハッシュタグは3〜5個（#GA4 #BigQuery #ClaudeCode #データ分析 等）
- 末尾にZenn記事URLを入れる
- フランクで読みやすいトーン
"""


# ── GitHub API ヘルパー ──────────────────────────────────────

def gh_get_file(repo, path):
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


# ── テーマ取得 ──────────────────────────────────────────────

def get_next_theme():
    """themes.csvから未投稿・優先度1のテーマを1件取得"""
    content, _ = gh_get_file(ZENN_REPO, "themes.csv")
    if not content:
        return None
    reader = csv.DictReader(io.StringIO(content))
    rows = [row for row in reader if row["published"].upper() == "FALSE"]
    for priority in ["1", "2", "3"]:
        candidates = [r for r in rows if r["priority"] == priority]
        if candidates:
            return candidates[0]
    return None


# ── スラッグ生成 ────────────────────────────────────────────

def title_to_slug(title):
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) < 5:
        slug = "article-" + str(uuid.uuid4())[:8]
    return slug[:50]


def slug_exists_on_github(slug):
    _, sha = gh_get_file(ZENN_REPO, f"articles/{slug}.md")
    return sha is not None


# ── Claude API ──────────────────────────────────────────────

def generate_article(theme):
    client = anthropic.Anthropic()
    prompt = f"""
以下のテーマでZenn記事を書いてください。

テーマID: {theme['id']}
カテゴリ: {theme['category']}
タイトル: {theme['title']}
記事タイプ: {theme['type']}

Markdownフォーマットで、フロントマターから本文末尾のCTAまで完全な記事を出力してください。
"""
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=ARTICLE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def generate_threads_post(title, slug, article_preview):
    client = anthropic.Anthropic()
    zenn_url = f"https://zenn.dev/{ZENN_USER}/articles/{slug}"
    prompt = f"""
以下の記事に対するThreads投稿文を作成してください。

タイトル: {title}
記事冒頭: {article_preview[:300]}
ZennURL: {zenn_url}
"""
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        system=THREADS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def extract_title_from_md(content):
    match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    return match.group(1) if match else "無題"


# ── メイン ──────────────────────────────────────────────────

def main():
    theme = get_next_theme()
    if not theme:
        print("投稿可能なテーマがありません。themes.csvを確認してください。")
        return

    print(f"テーマ選択: [{theme['id']}] {theme['title']}")

    print("記事を生成中...")
    article_content = generate_article(theme)

    title = extract_title_from_md(article_content)
    slug = title_to_slug(theme["title"])

    # スラッグ重複チェック
    if slug_exists_on_github(slug):
        slug = slug + "-" + str(uuid.uuid4())[:4]

    filename = f"{slug}.md"

    # GitHub APIで記事を保存
    result = gh_put_file(ZENN_REPO, f"articles/{filename}", article_content, None, f"draft: {filename}")
    if "content" not in result:
        print(f"記事の保存に失敗しました: {result}")
        return
    print(f"記事を保存: {filename}")

    # 記事プレビュー（フロントマター除いた先頭500字）
    body = re.sub(r"^---[\s\S]+?---\n", "", article_content, count=1).strip()
    article_preview = body[:500]

    print("Threads投稿文を生成中...")
    threads_post = generate_threads_post(title, slug, article_preview)

    # キューに追加
    queue, sha = load_queue()
    entry = {
        "id": str(uuid.uuid4())[:8],
        "theme_id": theme["id"],
        "title": title,
        "filename": filename,
        "article_preview": article_preview,
        "threads_post": threads_post,
        "zenn_url": f"https://zenn.dev/{ZENN_USER}/articles/{slug}",
        "created_at": datetime.now().isoformat(),
        "status": "pending",
    }
    queue.append(entry)
    save_queue(queue, sha)

    print(f"\n✅ 完了！ダッシュボードで確認してください。")
    print(f"   タイトル : {title}")
    print(f"   ファイル : {filename}")
    print(f"   Zenn URL : {entry['zenn_url']}")


if __name__ == "__main__":
    main()
