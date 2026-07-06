import math
import secrets

from flask import Flask, flash, redirect, render_template, request, url_for

from fetcher import (
    add_feed,
    add_keyword,
    backfill_posters,
    dismiss_article,
    ensure_config_exists,
    extract_quality_tags,
    fetch_once,
    get_connection,
    get_seconds_until_next_fetch,
    init_db,
    load_config,
    purge_old_articles,
    remove_feed,
    remove_keyword,
    set_tmdb_api_key,
    start_background_thread,
)

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

PAGE_SIZE = 24
QUALITY_CHOICES = ["720p", "1080p", "2160p"]

# Exécuté à l'import du module : nécessaire pour que ça marche aussi
# quand l'appli est servie via waitress (waitress-serve app:app), pas
# seulement via "python app.py".
ensure_config_exists()
init_db()
purge_old_articles()
# Premier pull synchrone AVANT que le serveur ne commence à répondre : sans
# ça, une base vide au démarrage restait vide tant qu'on ne cliquait pas sur
# "Vérifier maintenant", le premier cycle en arrière-plan n'ayant pas encore
# eu le temps de tourner.
fetch_once()
start_background_thread()


def paginate(conn, where_sql, params, page):
    total_filtered = conn.execute(f"SELECT COUNT(*) AS c FROM articles {where_sql}", params).fetchone()["c"]
    total_pages = max(1, math.ceil(total_filtered / PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    offset = (page - 1) * PAGE_SIZE

    rows = conn.execute(
        f"SELECT * FROM articles {where_sql} ORDER BY COALESCE(published_at, first_seen) DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, offset],
    ).fetchall()

    articles = []
    for row in rows:
        article = dict(row)
        article["quality_tags"] = ", ".join(extract_quality_tags(article["title"]))
        articles.append(article)

    return articles, page, total_pages, total_filtered


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
        "poll_interval": config.get("poll_interval_seconds", 300),
        "tmdb_api_key": config.get("tmdb_api_key", ""),
        "next_fetch_seconds": get_seconds_until_next_fetch(),
        "quality_choices": QUALITY_CHOICES,
    }


@app.route("/")
def matches():
    keyword_filter = request.args.get("keyword", "").strip()
    feed_filter = request.args.get("feed", "").strip()
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1

    where_sql = "WHERE matched_keywords != ''"
    params = []
    if keyword_filter:
        where_sql += " AND matched_keywords LIKE ?"
        params.append(f"%{keyword_filter}%")
    if feed_filter:
        where_sql += " AND feed_name = ?"
        params.append(feed_filter)
    if search:
        where_sql += " AND title LIKE ?"
        params.append(f"%{search}%")

    conn = get_connection()
    articles, page, total_pages, total_filtered = paginate(conn, where_sql, params, page)
    conn.close()

    filter_args = {}
    if keyword_filter:
        filter_args["keyword"] = keyword_filter
    if feed_filter:
        filter_args["feed"] = feed_filter
    if search:
        filter_args["q"] = search

    return render_template(
        "matches.html",
        active_tab="matches",
        articles=articles,
        keyword_filter=keyword_filter,
        feed_filter=feed_filter,
        search=search,
        page=page,
        total_pages=total_pages,
        total_filtered=total_filtered,
        filter_args=filter_args,
        **common_context(),
    )


@app.route("/all")
def all_titles():
    feed_filter = request.args.get("feed", "").strip()
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1

    where_sql = "WHERE 1=1"
    params = []
    if feed_filter:
        where_sql += " AND feed_name = ?"
        params.append(feed_filter)
    if search:
        where_sql += " AND title LIKE ?"
        params.append(f"%{search}%")

    conn = get_connection()
    articles, page, total_pages, total_filtered = paginate(conn, where_sql, params, page)
    conn.close()

    filter_args = {}
    if feed_filter:
        filter_args["feed"] = feed_filter
    if search:
        filter_args["q"] = search

    return render_template(
        "all_titles.html",
        active_tab="all",
        articles=articles,
        feed_filter=feed_filter,
        search=search,
        page=page,
        total_pages=total_pages,
        total_filtered=total_filtered,
        filter_args=filter_args,
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
    dismiss_article(article_id)
    return redirect(request.referrer or url_for("matches"))


@app.route("/keywords/add", methods=["POST"])
def keywords_add():
    keyword = request.form.get("keyword", "")
    quality = request.form.get("quality", "")
    if keyword.strip():
        add_keyword(keyword, quality)
        label = keyword.strip()
        if quality.strip():
            label += f' ({quality.strip()})'
        flash(f'Mot-clé "{label}" ajouté.')
    return redirect(request.referrer or url_for("matches"))


@app.route("/keywords/delete", methods=["POST"])
def keywords_delete():
    keyword = request.form.get("keyword", "")
    quality = request.form.get("quality", "")
    remove_keyword(keyword, quality)
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
