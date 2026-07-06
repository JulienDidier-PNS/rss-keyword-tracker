import secrets

from flask import Flask, flash, redirect, render_template, request, url_for

from fetcher import (
    add_feed,
    add_keyword,
    add_quality_filter,
    backfill_posters,
    ensure_config_exists,
    fetch_once,
    get_connection,
    init_db,
    load_config,
    remove_feed,
    remove_keyword,
    remove_quality_filter,
    set_tmdb_api_key,
    start_background_thread,
)

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Exécuté à l'import du module : nécessaire pour que ça marche aussi
# quand l'appli est servie via waitress (waitress-serve app:app), pas
# seulement via "python app.py".
ensure_config_exists()
init_db()
start_background_thread()


def common_context():
    conn = get_connection()
    feed_names = [
        row["feed_name"]
        for row in conn.execute("SELECT DISTINCT feed_name FROM articles ORDER BY feed_name")
    ]
    total = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
    conn.close()

    config = load_config()
    return {
        "feed_names": feed_names,
        "total": total,
        "configured_keywords": config.get("keywords", []),
        "configured_feeds": config.get("feeds", []),
        "configured_quality_filters": config.get("quality_filters", []),
        "poll_interval": config.get("poll_interval_seconds", 300),
        "tmdb_api_key": config.get("tmdb_api_key", ""),
    }


@app.route("/")
def matches():
    conn = get_connection()
    keyword_filter = request.args.get("keyword", "").strip()
    feed_filter = request.args.get("feed", "").strip()
    search = request.args.get("q", "").strip()

    query = "SELECT * FROM articles WHERE matched_keywords != '' AND quality_ok = 1"
    params = []
    if keyword_filter:
        query += " AND matched_keywords LIKE ?"
        params.append(f"%{keyword_filter}%")
    if feed_filter:
        query += " AND feed_name = ?"
        params.append(feed_filter)
    if search:
        query += " AND title LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY first_seen DESC LIMIT 300"

    articles = conn.execute(query, params).fetchall()
    conn.close()

    return render_template(
        "matches.html",
        active_tab="matches",
        articles=articles,
        keyword_filter=keyword_filter,
        feed_filter=feed_filter,
        search=search,
        **common_context(),
    )


@app.route("/all")
def all_titles():
    conn = get_connection()
    feed_filter = request.args.get("feed", "").strip()
    search = request.args.get("q", "").strip()

    query = "SELECT * FROM articles WHERE 1=1"
    params = []
    if feed_filter:
        query += " AND feed_name = ?"
        params.append(feed_filter)
    if search:
        query += " AND title LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY first_seen DESC LIMIT 300"

    articles = conn.execute(query, params).fetchall()
    conn.close()

    return render_template(
        "all_titles.html",
        active_tab="all",
        articles=articles,
        feed_filter=feed_filter,
        search=search,
        **common_context(),
    )


@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        active_tab="settings",
        **common_context(),
    )


@app.route("/refresh", methods=["POST"])
def refresh():
    fetch_once()
    return redirect(request.referrer or url_for("matches"))


@app.route("/delete/<int:article_id>", methods=["POST"])
def delete(article_id):
    conn = get_connection()
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("matches"))


@app.route("/keywords/add", methods=["POST"])
def keywords_add():
    keyword = request.form.get("keyword", "")
    if keyword.strip():
        add_keyword(keyword)
        flash(f'Mot-clé "{keyword.strip()}" ajouté.')
    return redirect(request.referrer or url_for("matches"))


@app.route("/keywords/delete", methods=["POST"])
def keywords_delete():
    keyword = request.form.get("keyword", "")
    remove_keyword(keyword)
    return redirect(request.referrer or url_for("matches"))


@app.route("/feeds/add", methods=["POST"])
def feeds_add():
    name = request.form.get("name", "")
    url = request.form.get("url", "")
    if not url.strip().lower().startswith(("http://", "https://")):
        flash("L'URL du flux doit commencer par http:// ou https://.")
        return redirect(request.referrer or url_for("matches"))
    add_feed(name, url)
    flash(f'Flux "{name.strip() or url.strip()}" ajouté.')
    return redirect(request.referrer or url_for("matches"))


@app.route("/feeds/delete", methods=["POST"])
def feeds_delete():
    url = request.form.get("url", "")
    remove_feed(url)
    return redirect(request.referrer or url_for("matches"))


@app.route("/quality/add", methods=["POST"])
def quality_add():
    value = request.form.get("value", "")
    if value.strip():
        add_quality_filter(value)
        flash(f'Filtre de qualité "{value.strip()}" ajouté.')
    return redirect(request.referrer or url_for("matches"))


@app.route("/quality/delete", methods=["POST"])
def quality_delete():
    value = request.form.get("value", "")
    remove_quality_filter(value)
    return redirect(request.referrer or url_for("matches"))


@app.route("/settings/tmdb", methods=["POST"])
def settings_tmdb():
    key = request.form.get("tmdb_api_key", "")
    set_tmdb_api_key(key)
    flash("Clé TMDB mise à jour." if key.strip() else "Clé TMDB retirée.")
    return redirect(request.referrer or url_for("matches"))


@app.route("/posters/backfill", methods=["POST"])
def posters_backfill():
    updated = backfill_posters()
    flash(f"{updated} affiche(s) récupérée(s).")
    return redirect(request.referrer or url_for("matches"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
