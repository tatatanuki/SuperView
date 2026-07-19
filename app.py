"""
景色のいい場所マップ Webアプリケーション (MVP + 天気連携/現在地検索)
--------------------------------------------------------------
OpenStreetMapのOverpass API(展望スポットデータ)、
Nominatim API(地名検索)、Open-Meteo API(天気予報)を利用して、
・地図上に「景色のいい場所(展望台・展望スポット等)」を表示
・各スポットの現在の天気を取得し、晴れているスポットを優先表示
・ブラウザの位置情報を使って現在地周辺を検索
する機能を持つアプリ。

いずれのAPIもAPIキー不要・無料で利用できます。
ただし利用規約上、過度な連続リクエストは避けてください
(個人の学習・課題用途であれば問題ありません)。

実行方法:
    pip install flask requests
    python app.py
    ブラウザで http://127.0.0.1:5000 を開く
"""

from flask import Flask, jsonify, request, render_template
import requests
import sqlite3
import os

app = Flask(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# お気に入りスポットを保存するSQLiteデータベース
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favorites.db")


def get_db():
    """SQLite接続を取得する(リクエストごとに接続し、行を辞書形式で扱えるようにする)"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """お気に入りテーブルが無ければ作成する"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            category TEXT,
            description TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(spot_id)
        )
    """)
    conn.commit()
    conn.close()


init_db()

# Nominatim/Overpassの利用ポリシー上、送信元を明示するUser-Agentを設定する
HEADERS = {
    "User-Agent": "ScenicMapStudentProject/1.0 (university-assignment)"
}

# 天気APIへ問い合わせるスポット数の上限(応答速度とAPI負荷対策)
MAX_SPOTS_FOR_WEATHER = 30

# WMO Weather interpretation codes (Open-Meteoが採用している標準コード)
# code: (アイコン, 日本語ラベル, 晴れ判定)
WEATHER_CODE_MAP = {
    0: ("☀️", "快晴", True),
    1: ("🌤️", "晴れ", True),
    2: ("⛅", "薄曇り", False),
    3: ("☁️", "曇り", False),
    45: ("🌫️", "霧", False),
    48: ("🌫️", "霧(霜)", False),
    51: ("🌦️", "小雨", False),
    53: ("🌦️", "霧雨", False),
    55: ("🌧️", "強い霧雨", False),
    56: ("🌧️", "着氷性の霧雨", False),
    57: ("🌧️", "強い着氷性の霧雨", False),
    61: ("🌧️", "弱い雨", False),
    63: ("🌧️", "雨", False),
    65: ("🌧️", "強い雨", False),
    66: ("🌧️", "着氷性の雨", False),
    67: ("🌧️", "強い着氷性の雨", False),
    71: ("🌨️", "弱い雪", False),
    73: ("🌨️", "雪", False),
    75: ("🌨️", "強い雪", False),
    77: ("🌨️", "霧雪", False),
    80: ("🌦️", "にわか雨", False),
    81: ("🌧️", "強いにわか雨", False),
    82: ("⛈️", "激しいにわか雨", False),
    85: ("🌨️", "にわか雪", False),
    86: ("🌨️", "強いにわか雪", False),
    95: ("⛈️", "雷雨", False),
    96: ("⛈️", "雷雨(ひょう)", False),
    99: ("⛈️", "激しい雷雨(ひょう)", False),
}


def attach_weather(spots):
    """各スポットの現在の天気をOpen-Meteo APIから取得して付与する"""
    if not spots:
        return spots

    lats = ",".join(str(s["lat"]) for s in spots)
    lons = ",".join(str(s["lon"]) for s in spots)
    params = {
        "latitude": lats,
        "longitude": lons,
        "current_weather": "true",
        "timezone": "auto",
    }

    try:
        resp = requests.get(OPEN_METEO_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        # 天気情報が取れなくてもスポット自体は表示できるようにする
        for s in spots:
            s["weather"] = None
        return spots

    # Open-Meteoは地点数が1件だとdict、複数件だとlistで返ってくる
    data_list = [data] if isinstance(data, dict) else data

    for s, w in zip(spots, data_list):
        current = w.get("current_weather") if isinstance(w, dict) else None
        if current:
            code = current.get("weathercode")
            icon, label, is_sunny = WEATHER_CODE_MAP.get(code, ("❓", "不明", False))
            s["weather"] = {
                "code": code,
                "icon": icon,
                "label": label,
                "temperature": current.get("temperature"),
                "is_sunny": is_sunny,
            }
        else:
            s["weather"] = None

    return spots


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/viewpoints")
def viewpoints():
    """指定した緯度経度の周辺にある観光スポット(展望台等)を取得し、天気で並び替える"""
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
        radius = int(request.args.get("radius", 3000))
    except (TypeError, ValueError):
        return jsonify({"error": "lat と lng は数値で指定してください"}), 400

    # 安全のため半径を100m〜20kmに制限(負荷対策)
    radius = max(100, min(radius, 20000))

    query = f"""
    [out:json][timeout:25];
    (
      node["tourism"="viewpoint"](around:{radius},{lat},{lng});
      node["tourism"="attraction"](around:{radius},{lat},{lng});
      node["natural"="peak"]["tourism"="viewpoint"](around:{radius},{lat},{lng});
    );
    out body;
    """

    try:
        resp = requests.post(OVERPASS_URL, data={"data": query},
                              headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return jsonify({"error": f"地図データの取得に失敗しました: {e}"}), 502
    except ValueError:
        return jsonify({"error": "地図データの解析に失敗しました"}), 502

    spots = []
    seen_ids = set()
    for el in data.get("elements", []):
        if el.get("id") in seen_ids:
            continue
        seen_ids.add(el.get("id"))
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name") or "名称不明の観光スポット"
        spots.append({
            "id": el.get("id"),
            "name": name,
            "lat": el.get("lat"),
            "lon": el.get("lon"),
            "category": "展望スポット" if tags.get("tourism") == "viewpoint" else "観光名所",
            "description": tags.get("description") or tags.get("note") or "",
        })

    # 天気APIへの問い合わせ数を制限してから天気情報を付与
    spots = spots[:MAX_SPOTS_FOR_WEATHER]
    spots = attach_weather(spots)

    # 「本日晴れているスポット」を上位に安定ソート
    spots.sort(key=lambda s: 0 if (s.get("weather") and s["weather"]["is_sunny"]) else 1)

    return jsonify({"count": len(spots), "spots": spots})


@app.route("/api/geocode")
def geocode():
    """地名からおおよその緯度経度を検索する(Nominatim API)"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "検索語を入力してください"}), 400

    params = {
        "q": q,
        "format": "json",
        "limit": 1,
        "accept-language": "ja",
    }

    try:
        resp = requests.get(NOMINATIM_URL, params=params,
                             headers=HEADERS, timeout=10)
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as e:
        return jsonify({"error": f"地名検索に失敗しました: {e}"}), 502

    if not results:
        return jsonify({"error": "該当する場所が見つかりませんでした"}), 404

    top = results[0]
    return jsonify({
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "display_name": top.get("display_name", q),
    })


@app.route("/api/favorites", methods=["GET"])
def get_favorites():
    """保存済みのお気に入りスポット一覧を取得する"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM favorites ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({"favorites": [dict(r) for r in rows]})


@app.route("/api/favorites", methods=["POST"])
def add_favorite():
    """スポットをお気に入りに追加する(同じスポットの重複登録は防ぐ)"""
    data = request.get_json(silent=True) or {}
    required = ["spot_id", "name", "lat", "lon"]
    if not all(k in data for k in required):
        return jsonify({"error": "spot_id, name, lat, lon は必須です"}), 400

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO favorites (spot_id, name, lat, lon, category, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                data["spot_id"], data["name"], data["lat"], data["lon"],
                data.get("category", ""), data.get("description", ""),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # UNIQUE制約違反 = すでに登録済み。エラーにせず現状の状態を返す
        conn.close()
        return jsonify({"message": "すでにお気に入りに登録されています"}), 200
    conn.close()
    return jsonify({"message": "お気に入りに追加しました"}), 201


@app.route("/api/favorites/<int:spot_id>", methods=["DELETE"])
def remove_favorite(spot_id):
    """スポットをお気に入りから削除する"""
    conn = get_db()
    cur = conn.execute("DELETE FROM favorites WHERE spot_id = ?", (spot_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "指定されたお気に入りは見つかりませんでした"}), 404
    return jsonify({"message": "お気に入りから削除しました"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
