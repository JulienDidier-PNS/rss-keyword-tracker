import math
import secrets

from flask import Flask, flash, redirect, render_template, request, url_for

from jdownloader import jd_send_links, jd_test_connection
from fetcher import (
    add_feed,
    add_tracked_title,
    backfill_posters,
    clean_media_name,
    detect_media_type,
    dismiss_article,
    ensure_config_exists,
    extract_hydracker_title_id,
    extract_quality_tags,
    fetch_hydracker_links,
    fetch_hydracker_torrents,
    fetch_once,
    get_connection,
    get_seconds_until_next_fetch,
    hydracker_login,
    init_db,
    load_config,
    purge_old_articles,
    rematch_all,
    remove_feed,
    remove_tracked_title,
    resolve_hydracker_link,
    set_hydracker_api_token,
    set_jdownloader_paths,
    set_jdownloader_settings,
    set_tmdb_api_key,
    start_background_thread,
    tmdb_search,
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
# Recale les correspondances des articles déjà en base sur les titres suivis
# actuels (utile après passage au matching TMDB, ou si config.json a été édité).
rematch_all()
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
        article["media_type"] = detect_media_type(article["title"], article.get("feed_name", ""))
        article["clean_title"] = clean_media_name(article["title"], article["media_type"])
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
        "tracked_titles": config.get("tracked_titles", []),
        "configured_feeds": config.get("feeds", []),
        "poll_interval": config.get("poll_interval_seconds", 300),
        "tmdb_api_key": config.get("tmdb_api_key", ""),
        "hydracker_api_token": config.get("hydracker_api_token", ""),
        "jd_email": config.get("jd_email", ""),
        "jd_device": config.get("jd_device", ""),
        "jd_password_set": bool(config.get("jd_password", "")),
        "jd_movies_folder": config.get("jd_movies_folder", ""),
        "jd_series_folder": config.get("jd_series_folder", ""),
        "jd_movies_subfolder": config.get("jd_movies_subfolder", True),
        "jd_series_subfolder": config.get("jd_series_subfolder", True),
        "jd_ready": bool(config.get("jd_email") and config.get("jd_password") and config.get("jd_device")),
        "next_fetch_seconds": get_seconds_until_next_fetch(),
        "quality_choices": QUALITY_CHOICES,
    }


@app.route("/")
def matches():
    keyword_filter = request.args.get("keyword", "").strip()
    feed_filter = [f for f in request.args.getlist("feed") if f.strip()]
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1

    where_sql = "WHERE matched_keywords != ''"
    params = []
    if keyword_filter:
        where_sql += " AND matched_keywords LIKE ?"
        params.append(f"%{keyword_filter}%")
    if feed_filter:
        placeholders = ", ".join("?" for _ in feed_filter)
        where_sql += f" AND feed_name IN ({placeholders})"
        params.extend(feed_filter)
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
    feed_filter = [f for f in request.args.getlist("feed") if f.strip()]
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1

    where_sql = "WHERE 1=1"
    params = []
    if feed_filter:
        placeholders = ", ".join("?" for _ in feed_filter)
        where_sql += f" AND feed_name IN ({placeholders})"
        params.extend(feed_filter)
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


# Sections de configuration, chacune sur sa propre sous-page pleine largeur.
SETTINGS_SECTIONS = [
    ("feeds", "Flux RSS"),
    ("tracked", "Titres suivis"),
    ("media", "Affiches / TMDB"),
    ("hydracker", "Hydracker"),
    ("jdownloader", "JDownloader"),
]
SETTINGS_SECTION_KEYS = {key for key, _ in SETTINGS_SECTIONS}


@app.route("/settings")
def settings():
    return redirect(url_for("settings_section", section="feeds"))


@app.route("/settings/section/<section>")
def settings_section(section):
    if section not in SETTINGS_SECTION_KEYS:
        section = "feeds"
    return render_template(
        "settings/page.html",
        active_tab="settings",
        settings_section=section,
        settings_sections=SETTINGS_SECTIONS,
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


@app.route("/tmdb/search")
def tmdb_search_route():
    """Recherche TMDB pour la config des titres suivis (AJAX)."""
    query = request.args.get("q", "")
    results, error = tmdb_search(query, load_config().get("tmdb_api_key", "").strip())
    if error:
        return {"ok": False, "error": error, "results": []}
    return {"ok": True, "results": results}


@app.route("/tracked/add", methods=["POST"])
def tracked_add():
    tmdb_id = request.form.get("tmdb_id", "")
    ttype = request.form.get("type", "")
    title = request.form.get("title", "")
    year = request.form.get("year", "")
    poster = request.form.get("poster", "")
    quality = request.form.get("quality", "")
    if tmdb_id and ttype and title:
        add_tracked_title(tmdb_id, ttype, title, year, poster or None, quality)
        # Re-matche les articles déjà en base pour ce nouveau titre.
        rematch_all()
        label = title + (f" ({quality.strip()})" if quality.strip() else "")
        flash(f'Titre suivi « {label} » ajouté.')
    return redirect(request.referrer or url_for("settings_section", section="tracked"))


@app.route("/tracked/delete", methods=["POST"])
def tracked_delete():
    tmdb_id = request.form.get("tmdb_id", "")
    ttype = request.form.get("type", "")
    quality = request.form.get("quality", "")
    remove_tracked_title(tmdb_id, ttype, quality)
    rematch_all()
    return redirect(request.referrer or url_for("settings_section", section="tracked"))


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


@app.route("/settings/hydracker", methods=["POST"])
def settings_hydracker():
    token = request.form.get("hydracker_api_token", "")
    set_hydracker_api_token(token)
    flash("Token Hydracker mis à jour." if token.strip() else "Token Hydracker retiré.")
    return redirect(request.referrer or url_for("matches"))


@app.route("/settings/hydracker-login", methods=["POST"])
def settings_hydracker_login():
    """Récupère un token API à partir des identifiants du compte (le mot de
    passe n'est pas conservé, seul le token renvoyé est enregistré)."""
    email = request.form.get("hydracker_email", "")
    password = request.form.get("hydracker_password", "")
    token, error = hydracker_login(email, password)
    if error:
        flash(f"Connexion Hydracker échouée : {error}")
    else:
        set_hydracker_api_token(token)
        flash("Connecté à Hydracker : token récupéré et enregistré.")
    return redirect(request.referrer or url_for("settings"))


@app.route("/settings/jdownloader", methods=["POST"])
def settings_jdownloader():
    email = request.form.get("jd_email", "")
    password = request.form.get("jd_password", "")
    device = request.form.get("jd_device", "")
    # Champ mot de passe laissé vide = on garde celui déjà enregistré.
    if not password:
        password = None
    set_jdownloader_settings(email, password, device)
    flash("Paramètres My.JDownloader enregistrés.")
    return redirect(request.referrer or url_for("settings"))


@app.route("/jdownloader/test", methods=["POST"])
def jdownloader_test():
    """Teste la connexion My.JDownloader et renvoie la liste des appareils.
    Utilise les identifiants saisis dans le formulaire, ou ceux déjà
    enregistrés si le champ est laissé vide."""
    config = load_config()
    email = request.form.get("jd_email", "").strip() or config.get("jd_email", "")
    password = request.form.get("jd_password", "") or config.get("jd_password", "")
    devices, error = jd_test_connection(email, password)
    if error:
        return {"ok": False, "error": error, "devices": []}
    return {"ok": True, "devices": [{"name": d.get("name"), "type": d.get("type")} for d in devices]}


@app.route("/settings/jdownloader-paths", methods=["POST"])
def settings_jdownloader_paths():
    set_jdownloader_paths(
        request.form.get("jd_movies_folder", ""),
        request.form.get("jd_series_folder", ""),
        request.form.get("jd_movies_subfolder") == "on",
        request.form.get("jd_series_subfolder") == "on",
    )
    flash("Dossiers JDownloader enregistrés.")
    return redirect(request.referrer or url_for("settings"))


@app.route("/jdownloader/send", methods=["POST"])
def jdownloader_send():
    """Envoie un lien (déjà résolu côté client) à JDownloader, dans le dossier
    de destination fourni, et démarre le téléchargement."""
    config = load_config()
    email = config.get("jd_email", "")
    password = config.get("jd_password", "")
    device = config.get("jd_device", "")
    if not (email and password and device):
        return {"ok": False, "error": "JDownloader non configuré (Configuration → JDownloader)."}

    url = request.form.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "Aucun lien à envoyer."}
    destination = request.form.get("destination", "").strip() or None
    package = request.form.get("package", "").strip() or None

    ok, error = jd_send_links(
        email, password, device, url,
        package_name=package, destination_folder=destination, autostart=True,
    )
    if not ok:
        return {"ok": False, "error": error}
    return {"ok": True, "destination": destination or ""}


@app.route("/article/<int:article_id>/torrents")
def article_torrents(article_id):
    """Résolution à la demande des liens de téléchargement Hydracker : appelée
    en AJAX au moment où l'utilisateur veut télécharger, jamais au fetch (les
    URLs signées expirent après ~30 min, et chaque appel consomme le crédit
    API)."""
    conn = get_connection()
    row = conn.execute("SELECT url FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    if row is None:
        return {"ok": False, "error": "Article introuvable.", "torrents": []}, 404

    title_id = extract_hydracker_title_id(row["url"])
    if not title_id:
        return {"ok": False, "error": "Cet article n'est pas un titre Hydracker.", "torrents": []}, 400

    token = load_config().get("hydracker_api_token", "").strip()
    torrents, error = fetch_hydracker_torrents(title_id, token)
    if error:
        return {"ok": False, "error": error, "torrents": []}
    return {"ok": True, "torrents": torrents}


@app.route("/article/<int:article_id>/links")
def article_links(article_id):
    """Liste les liens de téléchargement direct (hébergeurs) d'un titre
    Hydracker. Renvoie seulement les métadonnées : chaque lien est ensuite
    résolu à la demande via /link/<id>/resolve."""
    conn = get_connection()
    row = conn.execute("SELECT url FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    if row is None:
        return {"ok": False, "error": "Article introuvable.", "links": []}, 404

    title_id = extract_hydracker_title_id(row["url"])
    if not title_id:
        return {"ok": False, "error": "Cet article n'est pas un titre Hydracker.", "links": []}, 400

    token = load_config().get("hydracker_api_token", "").strip()
    links, error = fetch_hydracker_links(title_id, token)
    if error:
        return {"ok": False, "error": error, "links": []}
    return {"ok": True, "links": links}


@app.route("/link/<int:link_id>/resolve")
def link_resolve(link_id):
    """Résolution debrid d'un lien direct précis : renvoie l'URL de
    téléchargement direct réelle. Déclenchée par un clic explicite (peut débiter
    le crédit / quota du compte)."""
    token = load_config().get("hydracker_api_token", "").strip()
    result, error = resolve_hydracker_link(link_id, token)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, **result}


@app.route("/posters/backfill", methods=["POST"])
def posters_backfill():
    updated = backfill_posters()
    flash(f"{updated} affiche(s) récupérée(s).")
    return redirect(request.referrer or url_for("matches"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
