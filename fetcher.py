import json
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests

BASE_DIR = Path(__file__).resolve().parent
# Séparé du code exprès : en déploiement Docker, DATA_DIR pointe vers un volume
# monté (ex. /data) qui survit au remplacement de l'image lors d'une mise à
# jour. Par défaut (hors Docker), les données restent à côté du code comme
# avant.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.example.json"
DB_PATH = DATA_DIR / "articles.db"

RETENTION_DAYS = 30

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

_lock = threading.Lock()
_config_lock = threading.Lock()
_state_lock = threading.Lock()
_next_fetch_at = None

_SEASON_EPISODE_RE = re.compile(r"\bS\d{1,2}E\d{1,3}\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_QUALITY_TAG_RE = re.compile(
    r"\b("
    r"FRENCH|MULTI|VOSTFR|VOST|VFF?|VFQ|TRUEFRENCH|SUBFRENCH|"
    r"\d{3,4}p|WEB[-. ]?DL|WEBRIP|BLU[-. ]?RAY|BDRIP|HDTV|DVDRIP|HDLIGHT|"
    r"REMUX|REPACK|PROPER|LIMITED|INTERNAL|"
    r"H\.?26[45]|X26[45]|HEVC|AVC|AV1|AAC|AC3|DTS(?:-HD)?|DD\d\.\d"
    r")\b",
    re.IGNORECASE,
)
_IMG_SRC_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"', re.IGNORECASE)


def normalize(text):
    """Minuscule, sans accents, séparateurs (. _ -) transformés en espaces.
    Permet de matcher "house of the dragon" contre un titre de release du
    type "House.of.the.Dragon.S02E05.FRENCH.1080p"."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[._\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def ensure_config_exists():
    """config.json contient des secrets (clés API, passkeys) et n'est donc pas
    versionné (voir .gitignore). Au premier lancement après un clone, on le
    crée à partir de config.example.json pour que l'appli démarre quand même."""
    if not CONFIG_PATH.exists() and CONFIG_EXAMPLE_PATH.exists():
        shutil.copy(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
        print(f"[config] {CONFIG_PATH.name} créé à partir de {CONFIG_EXAMPLE_PATH.name} — pense à le personnaliser.")


def _migrate_keywords(config):
    """Ancien format : "keywords" (liste de chaînes) + "quality_filters"
    (liste globale appliquée à tous les mots-clés indistinctement). Nouveau
    format : chaque mot-clé porte sa propre qualité optionnelle, ex.
    {"keyword": "house of the dragon", "quality": "1080p"} — un mot-clé sans
    lien avec le format d'un autre flux (ex. Hydracker) n'est plus pénalisé
    par un filtre qualité pensé pour un autre flux (ex. Torr9)."""
    keywords = config.get("keywords", [])
    legacy_quality_filters = config.get("quality_filters")
    default_quality = legacy_quality_filters[0] if legacy_quality_filters else ""

    needs_migration = legacy_quality_filters is not None or any(isinstance(k, str) for k in keywords)
    if not needs_migration:
        return config

    normalized = []
    for kw in keywords:
        if isinstance(kw, str):
            normalized.append({"keyword": kw, "quality": default_quality})
        else:
            normalized.append({"keyword": kw.get("keyword", ""), "quality": kw.get("quality", "")})

    config["keywords"] = normalized
    config.pop("quality_filters", None)
    save_config(config)
    return config


def load_config():
    with _config_lock:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    return _migrate_keywords(config)


def save_config(config):
    with _config_lock:
        tmp_path = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)


def add_keyword(keyword, quality=""):
    keyword = keyword.strip()
    quality = quality.strip()
    if not keyword:
        return
    config = load_config()
    entries = config.setdefault("keywords", [])
    exists = any(
        normalize(e.get("keyword", "")) == normalize(keyword) and normalize(e.get("quality", "")) == normalize(quality)
        for e in entries
    )
    if not exists:
        entries.append({"keyword": keyword, "quality": quality})
        save_config(config)


def remove_keyword(keyword, quality=""):
    config = load_config()
    config["keywords"] = [
        e for e in config.get("keywords", []) if not (e.get("keyword") == keyword and e.get("quality", "") == quality)
    ]
    save_config(config)


def _set_next_fetch_at(seconds_from_now):
    global _next_fetch_at
    with _state_lock:
        _next_fetch_at = datetime.utcnow() + timedelta(seconds=seconds_from_now)


def get_seconds_until_next_fetch():
    with _state_lock:
        target = _next_fetch_at
    if target is None:
        return None
    return max(0, int((target - datetime.utcnow()).total_seconds()))


def add_feed(name, url):
    url = url.strip()
    name = name.strip() or url
    if not url:
        return
    config = load_config()
    feeds = config.setdefault("feeds", [])
    if any(f.get("url") == url for f in feeds):
        return
    feeds.append({"name": name, "url": url})
    save_config(config)


def remove_feed(url):
    config = load_config()
    config["feeds"] = [f for f in config.get("feeds", []) if f.get("url") != url]
    save_config(config)


def set_tmdb_api_key(key):
    config = load_config()
    config["tmdb_api_key"] = key.strip()
    save_config(config)


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_name TEXT,
            feed_url TEXT,
            title TEXT,
            url TEXT UNIQUE,
            download_url TEXT,
            summary TEXT,
            published TEXT,
            published_at TEXT,
            matched_keywords TEXT,
            quality_ok INTEGER DEFAULT 1,
            poster_url TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS poster_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clean_title TEXT UNIQUE COLLATE NOCASE,
            poster_url TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dismissed_urls (
            url TEXT PRIMARY KEY,
            dismissed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Migrations pour les bases créées avant l'ajout de ces colonnes.
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    if "download_url" not in existing_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN download_url TEXT")
    if "quality_ok" not in existing_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN quality_ok INTEGER DEFAULT 1")
    if "poster_url" not in existing_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN poster_url TEXT")
    if "published_at" not in existing_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN published_at TEXT")
    conn.commit()
    conn.close()


def is_dismissed(conn, url):
    return conn.execute("SELECT 1 FROM dismissed_urls WHERE url = ?", (url,)).fetchone() is not None


def dismiss_article(article_id):
    """Supprime l'article et retient son URL de façon permanente, pour qu'il
    ne soit jamais réinséré au prochain pull du flux (ex: faux positif à
    écarter définitivement). Sans ça, supprimer un article ne servait à rien
    puisque son URL redevenait "nouvelle" dès le prochain rafraîchissement."""
    conn = get_connection()
    row = conn.execute("SELECT url FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is not None:
        with _lock:
            conn.execute("INSERT OR IGNORE INTO dismissed_urls (url) VALUES (?)", (row["url"],))
            conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            conn.commit()
    conn.close()


def purge_old_articles(days=RETENTION_DAYS):
    """Supprime les articles vus il y a plus de `days` jours, pour ne pas
    laisser la base grossir indéfiniment."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    with _lock:
        conn.execute("DELETE FROM articles WHERE first_seen < ?", (cutoff,))
        conn.commit()
    conn.close()


def match_keyword_entries(title, summary, keyword_entries):
    """Pour chaque mot-clé configuré (avec sa qualité optionnelle associée),
    vérifie si le titre/résumé matche. Un mot-clé sans qualité associée est
    toujours confirmé dès que le texte matche. Un mot-clé avec une qualité
    associée (ex. "1080p") n'est confirmé que si cette qualité est *aussi*
    présente dans le texte — sinon il est compté comme "texte trouvé mais
    qualité refusée", pour distinguer les vrais faux-positifs des articles
    juste dans la mauvaise qualité.

    Renvoie (confirmed, quality_rejected) : deux listes de noms de mots-clés.
    """
    haystack = normalize(f"{title} {summary}")
    confirmed = []
    quality_rejected = []

    for entry in keyword_entries:
        keyword = entry.get("keyword", "")
        quality = entry.get("quality", "").strip()
        if not keyword or normalize(keyword) not in haystack:
            continue
        if quality and normalize(quality) not in haystack:
            quality_rejected.append(keyword)
        else:
            confirmed.append(keyword)

    return confirmed, quality_rejected


def extract_download_url(entry):
    """Renvoie l'URL de la balise <enclosure> (fichier .torrent, média, etc.)
    si le flux en fournit une, sinon None."""
    for enclosure in entry.get("enclosures", []):
        href = enclosure.get("href")
        if href:
            return href
    return None


def extract_published_at(entry):
    """Date de publication normalisée (UTC, triable) à partir du flux. Utilisée
    pour trier par date réelle de l'article plutôt que par date de récupération :
    plusieurs épisodes récupérés dans le même cycle auraient sinon la même date
    de récupération, quel que soit leur ordre de sortie réel."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", parsed)
    except (TypeError, ValueError):
        return None


def extract_quality_tags(title):
    """Tags de qualité détectés dans le titre (résolution, langue, codec...),
    ex. ["MULTi", "1080p", "H264"] — affichage informatif dans la fiche détail,
    indépendant des mots-clés/qualités configurés."""
    return [m.group(0) for m in _QUALITY_TAG_RE.finditer(title or "")]


def extract_poster_from_summary(summary):
    """Certains flux (ex: Hydracker) embarquent directement une image
    <img src="..."> dans la description de l'item, souvent une affiche TMDB.
    Si présente, on la récupère gratuitement, sans appel à l'API TMDB."""
    if not summary:
        return None
    match = _IMG_SRC_RE.search(summary)
    return match.group(1) if match else None


def clean_release_title(raw_title):
    """Extrait un titre exploitable pour une recherche TMDB à partir d'un nom
    de release torrent, ex: "House.of.the.Dragon.S03E03.MULTi.1080p...-SUPPLY"
    -> "House of the Dragon", "Dune.Part.Two.2024.MULTi.1080p...-EXTREME" ->
    "Dune Part Two". Coupe au plus tôt parmi le motif saison/épisode, l'année,
    ou le premier tag de qualité rencontré."""
    text = raw_title.replace(".", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()

    cut_points = [
        m.start()
        for m in (
            _SEASON_EPISODE_RE.search(text),
            _YEAR_RE.search(text),
            _QUALITY_TAG_RE.search(text),
        )
        if m
    ]

    if cut_points:
        text = text[: min(cut_points)]

    return text.strip(" -")


def fetch_poster_from_tmdb(query, api_key):
    try:
        response = requests.get(
            TMDB_SEARCH_URL,
            params={"api_key": api_key, "query": query, "language": "fr-FR", "include_adult": "false"},
            timeout=6,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f'[poster] erreur TMDB pour "{query}": {exc}')
        return None

    for result in data.get("results", []):
        if result.get("media_type") not in ("movie", "tv"):
            continue
        poster_path = result.get("poster_path")
        if poster_path:
            return f"{TMDB_IMAGE_BASE}{poster_path}"
    return None


def get_poster_url(raw_title, config):
    """Cherche l'affiche correspondant au titre nettoyé, en passant par un
    cache local (poster_cache) pour ne jamais interroger TMDB deux fois pour
    le même titre (utile puisqu'une série publie beaucoup d'épisodes)."""
    api_key = config.get("tmdb_api_key", "").strip()
    if not api_key:
        return None

    clean_title = clean_release_title(raw_title)
    if not clean_title:
        return None

    conn = get_connection()
    row = conn.execute(
        "SELECT poster_url FROM poster_cache WHERE clean_title = ? COLLATE NOCASE",
        (clean_title,),
    ).fetchone()
    if row is not None:
        conn.close()
        return row["poster_url"]

    poster_url = fetch_poster_from_tmdb(clean_title, api_key)
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO poster_cache (clean_title, poster_url) VALUES (?, ?)",
            (clean_title, poster_url),
        )
        conn.commit()
    conn.close()
    return poster_url


def fetch_once():
    """Parcourt tous les flux configurés et enregistre tous les titres rencontrés
    (déduplication par URL). Chaque article garde la trace des mots-clés qui ont
    matché (vide si aucun) et si son nom passe les filtres de qualité — utilisé
    pour distinguer la page "Correspondances" de la page "Tous les titres".
    Relit config.json à chaque appel : éditer le fichier suffit, pas besoin de
    relancer l'appli."""
    config = load_config()
    keyword_entries = config.get("keywords", [])
    feeds = config.get("feeds", [])

    conn = get_connection()
    new_count = 0

    for feed in feeds:
        feed_name = feed.get("name", feed.get("url"))
        feed_url = feed.get("url")
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            print(f"[fetch] erreur sur {feed_name} ({feed_url}): {exc}")
            continue

        for entry in parsed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))
            link = entry.get("link")
            if not link:
                continue
            if is_dismissed(conn, link):
                continue

            matched, quality_rejected = match_keyword_entries(title, summary, keyword_entries)
            quality_ok = not quality_rejected
            published = entry.get("published", entry.get("updated", ""))
            published_at = extract_published_at(entry)
            download_url = extract_download_url(entry)
            # Affiche : d'abord une image déjà embarquée dans le flux (gratuit,
            # ex. Hydracker), sinon recherche TMDB pour les titres qui matchent
            # un mot-clé (pour limiter les appels API).
            poster_url = extract_poster_from_summary(summary)
            if not poster_url and matched:
                poster_url = get_poster_url(title, config)

            try:
                with _lock:
                    conn.execute(
                        """INSERT INTO articles
                           (feed_name, feed_url, title, url, download_url, summary, published, published_at, matched_keywords, quality_ok, poster_url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            feed_name,
                            feed_url,
                            title,
                            link,
                            download_url,
                            summary,
                            published,
                            published_at,
                            ", ".join(matched),
                            1 if quality_ok else 0,
                            poster_url,
                        ),
                    )
                    conn.commit()
                new_count += 1
            except sqlite3.IntegrityError:
                pass  # URL déjà connue

    conn.close()
    return new_count


def backfill_posters():
    """Récupère l'affiche des articles déjà enregistrés (correspondances) qui
    n'en ont pas encore : d'abord via l'image embarquée dans le flux si
    présente, sinon via TMDB si une clé API est configurée. Utile juste après
    avoir renseigné une clé, ou après l'ajout d'un flux qui embarque déjà des
    affiches, vu que la récupération normale ne se fait qu'à l'insertion d'un
    nouvel article."""
    config = load_config()
    api_key = config.get("tmdb_api_key", "").strip()

    conn = get_connection()
    rows = conn.execute(
        "SELECT id, title, summary FROM articles WHERE matched_keywords != '' AND (poster_url IS NULL OR poster_url = '')"
    ).fetchall()

    updated = 0
    for row in rows:
        poster_url = extract_poster_from_summary(row["summary"])
        if not poster_url and api_key:
            poster_url = get_poster_url(row["title"], config)
        if poster_url:
            with _lock:
                conn.execute("UPDATE articles SET poster_url = ? WHERE id = ?", (poster_url, row["id"]))
                conn.commit()
            updated += 1

    conn.close()
    return updated


def background_loop():
    """Suppose qu'un premier fetch_once() a déjà été fait de façon synchrone
    avant le démarrage de ce thread (voir app.py) : on attend donc l'intervalle
    complet avant le prochain cycle, plutôt que de re-fetcher immédiatement."""
    while True:
        config = load_config()
        interval = config.get("poll_interval_seconds", 300)
        _set_next_fetch_at(interval)
        time.sleep(interval)
        try:
            n = fetch_once()
            if n:
                print(f"[fetch] {n} nouvel(le)(s) article(s) trouvé(s)")
            purge_old_articles()
        except Exception as exc:
            print(f"[fetch] erreur inattendue: {exc}")


def start_background_thread():
    thread = threading.Thread(target=background_loop, daemon=True)
    thread.start()
    return thread
