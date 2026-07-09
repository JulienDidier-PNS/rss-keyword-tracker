import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests

from jdownloader import jd_send_links

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

# Contrairement à Torr9 dont le flux RSS embarque directement le .torrent (voir
# extract_download_url), Hydracker ne fournit dans son flux qu'un lien vers la
# fiche du titre (ex. https://hydracker.com/titles/108276/play-dirty). Le lien
# de téléchargement s'obtient via l'API, en repartant de l'id du titre extrait
# de ce lien.
HYDRACKER_API_BASE = "https://hydracker.com/api/v1"
_HYDRACKER_TITLE_RE = re.compile(r"hydracker\.com/titles/(\d+)")
# Hydracker est derrière Cloudflare, qui répond 403 (page de challenge HTML) au
# User-Agent par défaut de requests. Un UA de navigateur laisse passer les
# appels API légitimes (authentifiés par le bearer token).
_HYDRACKER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

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
# Priorité aux liens en français pour l'auto-download DDL (l'utilisateur est
# francophone) : un lien MULTI/FRENCH est proposé avant un lien anglais seul.
_FRENCH_LANG_RE = re.compile(r"\b(true[- ]?french|french|multi|vff?|vfq|vf|vostfr)\b", re.IGNORECASE)


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


def set_hydracker_api_token(token):
    config = load_config()
    config["hydracker_api_token"] = token.strip()
    save_config(config)


def set_jdownloader_settings(email, password, device):
    """Identifiants My.JDownloader + appareil ciblé. Le mot de passe est
    conservé dans config.json (fichier local) car nécessaire pour rouvrir une
    session à chaque envoi ; passer password=None laisse l'existant inchangé."""
    config = load_config()
    config["jd_email"] = (email or "").strip()
    if password is not None:
        config["jd_password"] = password
    config["jd_device"] = (device or "").strip()
    save_config(config)


def set_jdownloader_paths(movies_folder, series_folder, movies_subfolder, series_subfolder):
    """Dossiers de destination JDownloader par type de média, et si un
    sous-dossier au nom du média doit être créé sous le dossier de base."""
    config = load_config()
    config["jd_movies_folder"] = (movies_folder or "").strip()
    config["jd_series_folder"] = (series_folder or "").strip()
    config["jd_movies_subfolder"] = bool(movies_subfolder)
    config["jd_series_subfolder"] = bool(series_subfolder)
    save_config(config)


def add_size_rule(quality, media_type, max_gb):
    """Ajoute (ou met à jour, dédup par quality+media_type) une règle globale
    limitant la taille des liens DDL envoyés en téléchargement automatique — ex.
    "2160p" + "movie" + 6 Go pour ne jamais auto-télécharger un remux BluRay
    massif. `media_type` vaut "movie", "series", ou "any" (les deux)."""
    quality = (quality or "").strip()
    media_type = media_type if media_type in ("movie", "series") else "any"
    try:
        max_gb = float(max_gb)
    except (TypeError, ValueError):
        return
    if max_gb <= 0:
        return
    config = load_config()
    rules = config.setdefault("size_rules", [])
    for r in rules:
        if normalize(r.get("quality", "")) == normalize(quality) and (r.get("media_type") or "any") == media_type:
            r["max_gb"] = max_gb
            save_config(config)
            return
    rules.append({"quality": quality, "media_type": media_type, "max_gb": max_gb})
    save_config(config)


def remove_size_rule(quality, media_type):
    quality = (quality or "").strip()
    media_type = media_type if media_type in ("movie", "series") else "any"
    config = load_config()
    config["size_rules"] = [
        r for r in config.get("size_rules", [])
        if not (normalize(r.get("quality", "")) == normalize(quality) and (r.get("media_type") or "any") == media_type)
    ]
    save_config(config)


def extract_hydracker_title_id(url):
    """Id du titre Hydracker extrait d'un lien de fiche, ex.
    "https://hydracker.com/titles/108276/play-dirty" -> "108276". None si le
    lien n'est pas un titre Hydracker."""
    if not url:
        return None
    match = _HYDRACKER_TITLE_RE.search(url)
    return match.group(1) if match else None


def _format_size(num):
    """Taille lisible à partir d'un nombre d'octets (champ `taille` de l'API)."""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit in ("o", "Ko") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} Po"


def _qual_label(item):
    """Qualité lisible d'un item, ex. "WEB 1080p Light" / "REMUX BLURAY". Le
    champ `qual.qual` porte la chaîne affichable ; `qual.label` n'est qu'un
    tier interne ("highqual")."""
    qual = item.get("qual") or {}
    return qual.get("qual") or qual.get("label") or ""


def _langs(item):
    """Langues d'un item (langues_compact), ex. ["TrueFrench", "English"]."""
    return [lang.get("name") for lang in (item.get("langues_compact") or []) if lang.get("name")]


def _hydracker_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _HYDRACKER_UA,
    }


def _hydracker_http_error(response):
    """Message d'erreur lisible pour les statuts d'échec courants de l'API
    Hydracker, ou None si la réponse est exploitable (200). Un token invalide
    fait rediriger (3xx) vers /login (d'où allow_redirects=False côté appelant),
    ce qu'on traite comme un échec d'authentification."""
    code = response.status_code
    if 300 <= code < 400:
        return "Token API Hydracker invalide ou accès API non activé (redirection vers la connexion)."
    if code == 401:
        return "Token API Hydracker invalide ou expiré (401)."
    if code == 402:
        return "Crédit GB Hydracker insuffisant pour ce lien (402)."
    if code == 403:
        return "Accès refusé par Hydracker (403) : compte premium requis, accès API non activé, ou contenu non autorisé."
    if code == 404:
        return "Introuvable côté Hydracker (404)."
    if code == 429:
        return "Quota Hydracker atteint (429) : limite journalière ou 1 req/s — réessaie dans un instant."
    if code != 200:
        return f"Réponse inattendue de Hydracker (HTTP {code})."
    return None


def fetch_hydracker_torrents(title_id, token):
    """Interroge l'API Hydracker pour lister les torrents d'un titre. Renvoie
    (torrents, erreur) : `torrents` est une liste de dicts prêts pour l'affichage
    (nom, qualité, taille, seeders, download_url), `erreur` vaut None si tout va
    bien, sinon un message lisible.

    La `download_url` renvoyée par l'API est une URL *signée valable ~30 min* :
    on ne peut donc pas la résoudre au moment du fetch du flux et la stocker
    comme le .torrent de Torr9 — elle serait expirée. D'où la résolution à la
    demande (au clic sur "Télécharger"), qui a l'avantage de ne consommer le
    crédit API que pour les titres réellement téléchargés."""
    if not token:
        return [], "Aucun token API Hydracker configuré (Configuration → Hydracker)."

    url = f"{HYDRACKER_API_BASE}/titles/{title_id}/content/torrents"
    try:
        response = requests.get(url, headers=_hydracker_headers(token), timeout=10, allow_redirects=False)
    except Exception as exc:
        return [], f"Erreur réseau vers Hydracker : {exc}"

    error = _hydracker_http_error(response)
    if error:
        return [], error

    try:
        payload = response.json()
    except ValueError:
        return [], "Réponse Hydracker illisible (JSON invalide)."

    # L'API renvoie `torrents` à la racine (le schéma OpenAPI le place à tort
    # sous `data`) — on gère les deux par sécurité.
    raw_torrents = payload.get("torrents")
    if raw_torrents is None:
        raw_torrents = (payload.get("data") or {}).get("torrents")
    raw_torrents = raw_torrents or []

    torrents = []
    for item in raw_torrents:
        torrents.append(
            {
                "id": item.get("id"),
                "name": item.get("torrent_name") or item.get("name") or "",
                "quality": _qual_label(item),
                "size": _format_size(item.get("taille") or item.get("size")),
                "seeders": item.get("seeders") or 0,
                "leechers": item.get("leechers") or 0,
                "langs": _langs(item),
                "saison": item.get("saison"),
                "episode": item.get("episode"),
                "full_saison": item.get("full_saison"),
                "download_url": item.get("download_url"),
            }
        )

    # La liste ne contient pas download_url : on résout les URLs signées en un
    # seul appel groupé à /content/torrents/{ids} (jusqu'à 50 ids séparés par
    # des virgules).
    missing_ids = [str(t["id"]) for t in torrents if not t["download_url"] and t["id"] is not None]
    if missing_ids:
        resolved = _resolve_hydracker_download_urls(missing_ids, token)
        for t in torrents:
            if not t["download_url"]:
                t["download_url"] = resolved.get(t["id"])

    # On ne garde que les torrents effectivement téléchargeables, triés par
    # seeders décroissants (le plus sain proposé en tête).
    torrents = [t for t in torrents if t["download_url"]]
    torrents.sort(key=lambda t: t["seeders"], reverse=True)
    return torrents, None


def _resolve_hydracker_download_urls(ids, token):
    """Résout les URLs signées d'un lot de torrents via /content/torrents/{ids}
    (ids séparés par des virgules). Renvoie un dict {id: download_url}. Silencieux
    en cas d'échec : le fetch principal affichera simplement moins de torrents."""
    try:
        response = requests.get(
            f"{HYDRACKER_API_BASE}/content/torrents/{','.join(ids)}",
            headers=_hydracker_headers(token),
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}

    # Réponse à la racine : `torrents` (plusieurs ids) ou `torrent` (un seul).
    items = payload.get("torrents")
    if items is None:
        single = payload.get("torrent")
        items = [single] if single else []

    return {item.get("id"): item.get("download_url") for item in items if item and item.get("download_url")}


def fetch_hydracker_links(title_id, token):
    """Liste les liens de téléchargement *direct* (hébergeurs) d'un titre via
    /titles/{id}/content/liens. Renvoie (liens, erreur).

    Le champ `lien` renvoyé par l'API est *obfusqué* et pas utilisable tel quel :
    chaque entrée n'expose donc ici que ses métadonnées (qualité, taille, hôte,
    saison/épisode) + son `id`. L'URL de téléchargement direct réelle s'obtient
    ensuite à la demande, lien par lien, via resolve_hydracker_link (résolution
    debrid), ce qui évite de consommer le crédit pour des liens jamais utilisés."""
    if not token:
        return [], "Aucun token API Hydracker configuré (Configuration → Hydracker)."

    url = f"{HYDRACKER_API_BASE}/titles/{title_id}/content/liens"
    try:
        response = requests.get(url, headers=_hydracker_headers(token), timeout=10, allow_redirects=False)
    except Exception as exc:
        return [], f"Erreur réseau vers Hydracker : {exc}"

    error = _hydracker_http_error(response)
    if error:
        return [], error

    try:
        payload = response.json()
    except ValueError:
        return [], "Réponse Hydracker illisible (JSON invalide)."

    # `liens` à la racine (le schéma OpenAPI le place à tort sous `data`).
    raw_links = payload.get("liens")
    if raw_links is None:
        raw_links = (payload.get("data") or {}).get("liens")
    raw_links = raw_links or []

    links = []
    for item in raw_links:
        if item.get("id") is None:
            continue
        host = item.get("host") or {}
        try:
            size_bytes = float(item.get("taille"))
        except (TypeError, ValueError):
            size_bytes = None
        links.append(
            {
                "id": item.get("id"),
                "quality": _qual_label(item),
                "size": _format_size(item.get("taille")),
                "size_bytes": size_bytes,
                "host": host.get("name") or "",
                "langs": _langs(item),
                "saison": item.get("saison"),
                "episode": item.get("episode"),
                "full_saison": item.get("full_saison"),
            }
        )

    return links, None


def resolve_hydracker_link(link_id, token):
    """Résout un lien direct précis (avec debrid) via /content/liens/{id}.
    Renvoie (result, erreur). `result` est un dict :
      - direct_url    : URL de téléchargement direct réelle (None si debrid KO)
      - raw_url       : URL d'origine sur l'hébergeur (repli si pas de direct_url)
      - debrided      : bool
      - debrid_error  : code d'erreur debrid éventuel
      - quality, size : infos d'affichage
      - remaining_today : quota restant du jour (si fourni par l'API)
    Chaque résolution applique la politique de tier et peut débiter le crédit :
    c'est voulu qu'elle soit déclenchée par un clic explicite, un lien à la fois."""
    if not token:
        return None, "Aucun token API Hydracker configuré (Configuration → Hydracker)."

    url = f"{HYDRACKER_API_BASE}/content/liens/{link_id}"
    try:
        response = requests.get(url, headers=_hydracker_headers(token), timeout=20, allow_redirects=False)
    except Exception as exc:
        return None, f"Erreur réseau vers Hydracker : {exc}"

    error = _hydracker_http_error(response)
    if error:
        return None, error

    try:
        payload = response.json()
    except ValueError:
        return None, "Réponse Hydracker illisible (JSON invalide)."

    # Champs à la racine (le schéma OpenAPI les place à tort sous `data`).
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    lien = data.get("lien") or {}
    host = lien.get("host") or {}
    access = data.get("access") or {}
    result = {
        # raw_url = lien original chez l'hébergeur (à utiliser avec son propre
        # abonnement, sans passer par le débrideur Hydracker).
        "raw_url": data.get("raw_url"),
        # direct_url = lien résolu par le débrideur Hydracker (identique à raw_url
        # quand aucun débridage n'a lieu).
        "direct_url": data.get("directDL"),
        "debrided": bool(data.get("debrided")),
        "debrid_error": data.get("debrid_error"),
        "host": host.get("name") or "",
        "quality": _qual_label(lien),
        "size": _format_size(lien.get("taille")),
        "requires_account": bool(data.get("requires_account")),
        # Politique appliquée par Hydracker :
        #  - source "personal"   : lien généré via TON compte hébergeur (abonnement) — gratuit
        #  - source "direct_url" : lien direct public — gratuit
        #  - source "debrid"     : passé par le débrideur Hydracker — facturé (gb_pack)
        "source": access.get("source"),
        "billing": access.get("billing"),
        "charged_bytes": access.get("charged_bytes"),
        "tier": access.get("tier"),
        "remaining_today": access.get("remaining_today"),
    }
    return result, None


def hydracker_login(email, password, token_name="stream-app"):
    """Obtient un token API Hydracker via POST /auth/login à partir des
    identifiants du compte. Renvoie (token, erreur).

    On force token_name="stream-app" : la doc précise que la preuve captcha
    n'est *pas* requise dans ce cas (chemin app mobile) — avec n'importe quel
    autre nom, l'API exigerait un captcha qu'on ne peut pas résoudre côté
    serveur. Le mot de passe n'est jamais stocké : seul le token renvoyé l'est."""
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return None, "Email et mot de passe requis."

    url = f"{HYDRACKER_API_BASE}/auth/login"
    try:
        response = requests.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": _HYDRACKER_UA,
            },
            json={"email": email, "password": password, "token_name": token_name},
            timeout=15,
            allow_redirects=False,
        )
    except Exception as exc:
        return None, f"Erreur réseau vers Hydracker : {exc}"

    if response.status_code == 401:
        return None, "Identifiants Hydracker invalides."
    if response.status_code == 422:
        return None, "Connexion refusée (422) : données invalides ou preuve captcha requise."
    if response.status_code == 429:
        return None, "Trop de tentatives de connexion (429, max 10/min) — réessaie plus tard."
    if response.status_code != 200:
        return None, f"Réponse inattendue de Hydracker (HTTP {response.status_code})."

    try:
        payload = response.json()
    except ValueError:
        return None, "Réponse Hydracker illisible (JSON invalide)."

    token = (payload.get("user") or {}).get("token")
    if not token:
        return None, "Token absent de la réponse Hydracker."
    return token, None


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
    # Cache de résolution TMDB pour les items non-Hydracker (ex. Torr9) : on
    # associe un titre de release nettoyé à son (tmdb_id, type), pour ne pas
    # réinterroger TMDB à chaque fetch. tmdb_id NULL = "cherché, rien trouvé".
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS title_tmdb_cache (
            clean_title TEXT PRIMARY KEY COLLATE NOCASE,
            tmdb_id INTEGER,
            tmdb_type TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    # 1 = auto-download DDL déjà traité pour cet article (évite de re-résoudre /
    # re-télécharger à chaque cycle ou lors du déclenchement rétroactif).
    if "auto_downloaded" not in existing_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN auto_downloaded INTEGER DEFAULT 0")
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


# ===== Suivi de titres par référence TMDB (matching exact) =====

def tmdb_search(query, api_key):
    """Recherche TMDB (films + séries) pour la config. Renvoie (résultats, err).
    Chaque résultat : {tmdb_id, type ("movie"/"tv"), title, year, poster_url}."""
    query = (query or "").strip()
    if not query:
        return [], None
    if not api_key:
        return [], "Aucune clé TMDB configurée (Configuration → Affiches / TMDB)."
    try:
        response = requests.get(
            TMDB_SEARCH_URL,
            params={"api_key": api_key, "query": query, "language": "fr-FR", "include_adult": "false"},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return [], f"Erreur TMDB : {exc}"

    results = []
    for item in data.get("results", []):
        media_type = item.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        date = item.get("release_date") or item.get("first_air_date") or ""
        poster = item.get("poster_path")
        results.append(
            {
                "tmdb_id": item.get("id"),
                "type": media_type,
                "title": item.get("title") or item.get("name") or "",
                "year": (date or "")[:4],
                "poster_url": f"{TMDB_IMAGE_BASE}{poster}" if poster else None,
            }
        )
    return results, None


def _resolve_hydracker_id(tmdb_id, tmdb_type, token):
    """Id du titre Hydracker correspondant à un (tmdb_id, type), via
    GET /titles?tmdb_id=. None si Hydracker ne connaît pas ce titre ou si le
    token manque. Permet de matcher les items Hydracker par leur id (dans le
    lien RSS) sans résoudre le tmdb de chaque item un par un."""
    if not token or not tmdb_id:
        return None
    hydra_type = "series" if tmdb_type == "tv" else "movie"
    try:
        response = requests.get(
            f"{HYDRACKER_API_BASE}/titles",
            params={"tmdb_id": tmdb_id},
            headers=_hydracker_headers(token),
            timeout=10,
            allow_redirects=False,
        )
        response.raise_for_status()
        rows = (response.json().get("pagination") or {}).get("data") or []
    except Exception:
        return None

    for row in rows:
        if str(row.get("tmdb_id")) == str(tmdb_id) and row.get("type") == hydra_type:
            return row.get("id")
    # Repli : bon tmdb_id mais type non concordant (ex. champ type absent).
    for row in rows:
        if str(row.get("tmdb_id")) == str(tmdb_id):
            return row.get("id")
    return None


def add_tracked_title(tmdb_id, ttype, title, year="", poster=None, quality="",
                      auto_download=False, source="torrent", auto_season="", auto_episodes="",
                      auto_folder="", origin="manual", requested_by="",
                      review_status="", review_reason=""):
    """Ajoute un titre suivi (dédup par tmdb_id + type + qualité) et résout tout
    de suite son id Hydracker. `auto_download`/`source`/`auto_season`/
    `auto_episodes` pilotent le téléchargement automatique dès l'apparition dans
    les flux (voir auto_download_ddl). `auto_folder` force un dossier de
    destination JDownloader propre à ce titre (prime sur le dossier calculé par
    défaut) — utile pour ranger une série dans son propre dossier. `origin`
    ("manual"/"whatsapp") et `requested_by` identifient les demandes créées par
    le bot WhatsApp (voir create_bot_request), affichées sur leur propre page de
    configuration. `review_status`/`review_reason` signalent une demande bot
    dont l'existence dans Plex n'a pas pu être vérifiée avec certitude (voir
    check_plex_availability) : à contrôler manuellement avant de faire confiance
    à l'auto-download. Si le titre est déjà suivi, sa config d'auto-download est
    mise à jour (origin/requested_by ne sont pas écrasés). Renvoie l'entrée
    stockée (ou None)."""
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None
    quality = (quality or "").strip()
    # Seul "ddl" active un vrai téléchargement auto ; "torrent" = suivi seul.
    source = "ddl" if source == "ddl" else "torrent"
    auto_download = bool(auto_download)
    auto_season = (auto_season or "").strip()
    auto_episodes = (auto_episodes or "").strip()
    auto_folder = (auto_folder or "").strip()
    origin = "whatsapp" if origin == "whatsapp" else "manual"
    requested_by = (requested_by or "").strip()
    review_status = "needs_review" if review_status == "needs_review" else ""
    review_reason = (review_reason or "").strip() if review_status else ""
    config = load_config()
    token = config.get("hydracker_api_token", "").strip()
    tracked = config.setdefault("tracked_titles", [])
    for t in tracked:
        if t.get("tmdb_id") == tmdb_id and t.get("type") == ttype and (t.get("quality") or "") == quality:
            # Déjà suivi : on met simplement à jour sa config d'auto-download.
            t["auto_download"] = auto_download
            t["source"] = source
            t["auto_season"] = auto_season
            t["auto_episodes"] = auto_episodes
            t["auto_folder"] = auto_folder
            t["review_status"] = review_status
            t["review_reason"] = review_reason
            if t.get("hydracker_id") is None:
                t["hydracker_id"] = _resolve_hydracker_id(tmdb_id, ttype, token)
            save_config(config)
            return t
    entry = {
        "tmdb_id": tmdb_id,
        "type": ttype,
        "title": title,
        "year": year,
        "poster": poster,
        "quality": quality,
        "auto_download": auto_download,
        "source": source,
        "auto_season": auto_season,
        "auto_episodes": auto_episodes,
        "auto_folder": auto_folder,
        "origin": origin,
        "requested_by": requested_by,
        "requested_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") if origin == "whatsapp" else "",
        "review_status": review_status,
        "review_reason": review_reason,
        "hydracker_id": _resolve_hydracker_id(tmdb_id, ttype, token),
    }
    tracked.append(entry)
    save_config(config)
    return entry


def update_tracked_title(tmdb_id, ttype, orig_quality, quality="", auto_download=False,
                         source="torrent", auto_season="", auto_episodes="", auto_folder=""):
    """Modifie un titre suivi identifié par (tmdb_id, type, ancienne qualité) sans
    le supprimer/recréer : met à jour sa qualité et toute sa config d'auto-download.
    Renvoie (entry, error). `error` est un message si le titre est introuvable ou
    si la nouvelle qualité entre en collision avec une autre entrée du même titre."""
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return None, "identifiant invalide"
    orig_quality = (orig_quality or "").strip()
    quality = (quality or "").strip()
    source = "ddl" if source == "ddl" else "torrent"
    auto_download = bool(auto_download)
    auto_season = (auto_season or "").strip()
    auto_episodes = (auto_episodes or "").strip()
    auto_folder = (auto_folder or "").strip()

    config = load_config()
    tracked = config.get("tracked_titles", [])
    target = None
    for t in tracked:
        if t.get("tmdb_id") == tmdb_id and t.get("type") == ttype and (t.get("quality") or "") == orig_quality:
            target = t
            break
    if target is None:
        return None, "titre suivi introuvable"

    # Changer la qualité modifie la clé de dédup : on refuse si elle percute une
    # autre entrée existante du même titre.
    if quality != orig_quality:
        for t in tracked:
            if t is not target and t.get("tmdb_id") == tmdb_id and t.get("type") == ttype \
                    and (t.get("quality") or "") == quality:
                return None, "un titre suivi avec cette qualité existe déjà"

    target["quality"] = quality
    target["auto_download"] = auto_download
    target["source"] = source
    target["auto_season"] = auto_season
    target["auto_episodes"] = auto_episodes
    target["auto_folder"] = auto_folder
    # Éditer une demande vaut résolution : on efface le signalement "à
    # vérifier" éventuel (voir check_plex_availability), l'admin vient de le
    # faire en enregistrant ce formulaire.
    target["review_status"] = ""
    target["review_reason"] = ""
    if target.get("hydracker_id") is None:
        target["hydracker_id"] = _resolve_hydracker_id(tmdb_id, ttype, config.get("hydracker_api_token", "").strip())
    save_config(config)
    return target, None


# ===== Bot WhatsApp (demandes de titres via un canal de communauté) =====

DEFAULT_BOT_QUALITY = "1080p"


def get_bot_shared_secret():
    """Secret partagé avec le bot WhatsApp (header X-Bot-Secret sur l'API
    interne /api/bot/requests) : généré une fois puis persisté, pour que
    seul le processus du bot (qui le lit depuis sa propre config) puisse créer
    des demandes."""
    config = load_config()
    secret = config.get("bot_shared_secret", "").strip()
    if not secret:
        secret = secrets.token_hex(24)
        config["bot_shared_secret"] = secret
        save_config(config)
    return secret


def regenerate_bot_shared_secret():
    config = load_config()
    secret = secrets.token_hex(24)
    config["bot_shared_secret"] = secret
    save_config(config)
    return secret


def list_bot_requests():
    """Titres suivis créés par le bot WhatsApp (origin == "whatsapp"), les plus
    récents d'abord — pour la page de configuration dédiée."""
    config = load_config()
    requests_ = [t for t in config.get("tracked_titles", []) if t.get("origin") == "whatsapp"]
    requests_.sort(key=lambda t: t.get("requested_at") or "", reverse=True)
    return requests_


# ===== Tautulli / Plex (vérification de disponibilité avant téléchargement) =====
#
# Avant de créer une demande bot, on vérifie si le média (ou les épisodes
# demandés d'une série) est déjà dans la bibliothèque Plex, via l'API Tautulli
# (qui reflète le contenu réellement scanné par Plex). Objectif : ne jamais
# lancer un téléchargement automatique pour quelque chose qu'on a déjà.
#
# Cette intégration est écrite de façon défensive : n'ayant pas d'instance
# Tautulli sous la main pour la tester en conditions réelles, toute réponse
# API à la forme inattendue fait basculer vers un statut "needs_review"
# (demande créée mais signalée, auto-download désactivé) plutôt que de risquer
# une fausse conclusion (télécharger un doublon, ou au contraire bloquer à tort
# un téléchargement légitime).

TAUTULLI_TIMEOUT = 10
_TMDB_GUID_RE = re.compile(r"tmdb://(\d+)")


def _tautulli_configured(config):
    return bool((config.get("tautulli_url") or "").strip() and (config.get("tautulli_api_key") or "").strip())


def set_tautulli_settings(url, api_key):
    config = load_config()
    config["tautulli_url"] = (url or "").strip().rstrip("/")
    config["tautulli_api_key"] = (api_key or "").strip()
    save_config(config)


def _tautulli_call(config, cmd, params=None):
    """Appelle l'API Tautulli (GET /api/v2?cmd=...). Renvoie (data, erreur) —
    `data` est le contenu de la clé "data" de la réponse, tel quel (sa forme
    exacte varie selon la commande : liste nue, ou dict avec une clé imbriquée)."""
    url = (config.get("tautulli_url") or "").strip()
    api_key = (config.get("tautulli_api_key") or "").strip()
    if not url or not api_key:
        return None, "Tautulli non configuré"
    query = {"apikey": api_key, "cmd": cmd, "out_type": "json"}
    query.update(params or {})
    try:
        response = requests.get(f"{url}/api/v2", params=query, timeout=TAUTULLI_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return None, f"erreur réseau Tautulli ({exc})"
    result = (payload or {}).get("response") or {}
    if result.get("result") != "success":
        return None, result.get("message") or "réponse Tautulli invalide"
    return result.get("data"), None


def tautulli_test_connection(url, api_key):
    """Teste la connexion (nom du serveur, léger et toujours disponible).
    Renvoie (nom_ou_data, erreur)."""
    return _tautulli_call({"tautulli_url": url, "tautulli_api_key": api_key}, "get_server_friendly_name")


def _as_list_strict(data):
    """Extrait une liste d'une réponse Tautulli, quelle que soit l'enveloppe
    utilisée selon la commande/version (liste nue, ou dict avec "children_list"
    / "data" / "results"). Renvoie None (et non []) si la forme n'est reconnue
    dans AUCUN de ces cas — pour distinguer "vraiment vide" de "je ne sais pas
    lire cette réponse", et laisser l'appelant basculer vers needs_review
    plutôt que conclure à tort que Plex n'a rien."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("children_list", "data", "results"):
            if key in data:
                return data[key] if isinstance(data[key], list) else []
    return None


def _extract_tmdb_id(guids):
    for g in guids or []:
        m = _TMDB_GUID_RE.search(g or "")
        if m:
            return m.group(1)
    return None


def _tautulli_library_section_ids(config, plex_media_type):
    """Sections Plex ("movie" ou "show") connues de Tautulli. Renvoie
    (liste d'ids, erreur)."""
    data, error = _tautulli_call(config, "get_libraries")
    if error:
        return [], error
    sections = _as_list_strict(data)
    if sections is None:
        return [], "réponse Tautulli inattendue (bibliothèques)"
    return [
        s.get("section_id") for s in sections
        if s.get("section_type") == plex_media_type and s.get("section_id") is not None
    ], None


def _tautulli_find_by_tmdb(config, tmdb_id, plex_media_type, title, year):
    """Cherche un film/show dans Plex (via Tautulli) correspondant à ce
    tmdb_id. Pré-filtre par titre normalisé (+ année en repli) pour limiter le
    nombre d'appels get_metadata, mais ne conclut JAMAIS à une correspondance
    sur le seul titre : confirmation obligatoire par le guid tmdb exact.
    Renvoie ({"rating_key": ...} ou None si rien trouvé, erreur)."""
    section_ids, error = _tautulli_library_section_ids(config, plex_media_type)
    if error:
        return None, error
    if not section_ids:
        return None, None  # aucune bibliothèque de ce type : rien à trouver, pas une erreur

    nname = normalize(title)
    all_rows = []
    for section_id in section_ids:
        data, error = _tautulli_call(
            config, "get_library_media_info",
            {"section_id": section_id, "length": 5000},
        )
        if error:
            return None, error
        rows = _as_list_strict(data)
        if rows is None:
            return None, "réponse Tautulli inattendue (contenu de bibliothèque)"
        all_rows.extend(rows)

    exact = [r for r in all_rows if normalize(r.get("title") or "") == nname]
    loose = [
        r for r in all_rows
        if r not in exact and nname in normalize(r.get("title") or "")
        and (not year or str(r.get("year") or "") == str(year))
    ]

    for row in exact + loose:
        rating_key = row.get("rating_key")
        if rating_key is None:
            continue
        meta, error = _tautulli_call(config, "get_metadata", {"rating_key": rating_key})
        if error or not isinstance(meta, dict):
            continue  # un souci ponctuel sur un candidat ne bloque pas les autres
        found_tmdb = _extract_tmdb_id(meta.get("guids"))
        if found_tmdb and str(found_tmdb) == str(tmdb_id):
            return {"rating_key": rating_key}, None

    return None, None


def _tautulli_season_episodes(config, show_rating_key, season):
    """Numéros d'épisodes déjà présents dans Plex pour cette saison d'un show.
    Renvoie (set d'entiers, erreur). Une saison absente de Plex renvoie un set
    vide (aucun épisode dispo), pas une erreur."""
    seasons_data, error = _tautulli_call(config, "get_children_metadata", {"rating_key": show_rating_key})
    if error:
        return None, error
    seasons = _as_list_strict(seasons_data)
    if seasons is None:
        return None, "réponse Tautulli inattendue (saisons)"

    season_row = next((s for s in seasons if str(s.get("media_index")) == str(season)), None)
    if season_row is None:
        return set(), None

    season_rating_key = season_row.get("rating_key")
    if season_rating_key is None:
        return None, "réponse Tautulli inattendue (clé de saison absente)"

    episodes_data, error = _tautulli_call(config, "get_children_metadata", {"rating_key": season_rating_key})
    if error:
        return None, error
    episodes = _as_list_strict(episodes_data)
    if episodes is None:
        return None, "réponse Tautulli inattendue (épisodes)"

    numbers = set()
    for ep in episodes:
        try:
            numbers.add(int(ep.get("media_index")))
        except (TypeError, ValueError):
            continue
    return numbers, None


def fetch_tmdb_season_episode_count(tmdb_id, season, api_key):
    """Nombre total d'épisodes d'une saison via TMDB — nécessaire pour savoir
    si "tous les épisodes" (spec vide) demandés sont déjà tous dans Plex.
    Renvoie None en cas d'erreur."""
    if not api_key:
        return None
    try:
        response = requests.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}",
            params={"api_key": api_key},
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None
    episodes = data.get("episodes")
    return len(episodes) if isinstance(episodes, list) else None


def check_plex_availability(tmdb_id, media_type, season, episodes_spec, title, year, config):
    """Vérifie si un titre (ou les épisodes demandés d'une série) existe déjà
    dans Plex via Tautulli, avant de créer une demande de téléchargement auto.

    Renvoie un dict :
      - status : "missing" (rien trouvé, ou vérification non applicable —
        Tautulli non configuré — on procède normalement) / "exists" (tout ce
        qui est demandé est déjà là -> ne rien créer) / "partial" (une partie
        existe déjà -> seuls les épisodes manquants seront suivis) /
        "needs_review" (vérification impossible ou ambiguë -> demande créée
        quand même mais signalée, auto-download désactivé en attendant).
      - message : explication humaine (français), vide seulement pour
        "missing" en fonctionnement normal.
      - adjusted_episodes : nouvelle spec d'épisodes à suivre (uniquement pour
        "partial"), sinon None.
    """
    if not _tautulli_configured(config):
        return {"status": "missing", "message": "", "adjusted_episodes": None}

    plex_media_type = "movie" if media_type == "movie" else "show"
    found, error = _tautulli_find_by_tmdb(config, tmdb_id, plex_media_type, title, year)
    if error:
        return {
            "status": "needs_review",
            "message": f"Vérification Plex/Tautulli impossible ({error}) : ajouté quand même, à vérifier manuellement.",
            "adjusted_episodes": None,
        }
    if found is None:
        return {"status": "missing", "message": "", "adjusted_episodes": None}

    if media_type != "tv":
        return {
            "status": "exists",
            "message": f"« {title} » est déjà disponible sur Plex — rien à télécharger.",
            "adjusted_episodes": None,
        }

    if not season:
        return {
            "status": "needs_review",
            "message": (
                f"« {title} » existe déjà (au moins partiellement) sur Plex et aucune saison "
                "précise n'a été demandée : ajouté quand même, à vérifier manuellement pour éviter les doublons."
            ),
            "adjusted_episodes": None,
        }

    existing, error = _tautulli_season_episodes(config, found["rating_key"], season)
    if error:
        return {
            "status": "needs_review",
            "message": f"Vérification des épisodes Plex impossible ({error}) : ajouté quand même, à vérifier manuellement.",
            "adjusted_episodes": None,
        }

    requested = parse_episode_spec(episodes_spec)
    if requested is None:
        total = fetch_tmdb_season_episode_count(tmdb_id, season, config.get("tmdb_api_key", "").strip())
        if total is None:
            return {
                "status": "needs_review",
                "message": (
                    f"Impossible de connaître le nombre d'épisodes de la saison {season} de « {title} » "
                    "(TMDB indisponible) : ajouté quand même, à vérifier manuellement."
                ),
                "adjusted_episodes": None,
            }
        requested = set(range(1, total + 1))

    missing = sorted(requested - existing)
    if not missing:
        return {
            "status": "exists",
            "message": f"« {title} » saison {season} est déjà entièrement disponible sur Plex — rien à télécharger.",
            "adjusted_episodes": None,
        }
    if len(missing) == len(requested):
        return {"status": "missing", "message": "", "adjusted_episodes": None}

    return {
        "status": "partial",
        "message": (
            f"« {title} » saison {season} : {len(requested) - len(missing)} épisode(s) déjà disponible(s) "
            f"sur Plex, retiré(s) de la demande. Seuls les épisodes manquants ({_format_episode_spec(missing)}) "
            "seront suivis."
        ),
        "adjusted_episodes": _format_episode_spec(missing),
    }


def create_bot_request(tmdb_id, ttype, title, year="", poster=None, quality="",
                       auto_season="", auto_episodes="", requested_by=""):
    """Crée un titre suivi à partir d'une demande WhatsApp confirmée par
    l'utilisateur, après vérification de disponibilité Plex (voir
    check_plex_availability) : qualité par défaut 1080p si non précisée,
    source toujours "ddl" (le bot ne gère que le DDL auto — le torrent reste un
    choix manuel depuis la page Titres suivis), auto-download activé sauf si
    la vérification Plex est incertaine (auto-download alors désactivé, en
    attendant une correction manuelle).

    Renvoie (status, entry, message) :
      - status : "created" / "already_available" (entry=None, rien créé) /
        "needs_review" (entry créée mais signalée) / "error" (paramètres
        invalides, entry=None).
    """
    if not (tmdb_id and ttype and title):
        return "error", None, "tmdb_id, type et title sont requis"

    quality = (quality or "").strip() or DEFAULT_BOT_QUALITY
    config = load_config()

    availability = check_plex_availability(tmdb_id, ttype, auto_season, auto_episodes, title, year, config)

    if availability["status"] == "exists":
        return "already_available", None, availability["message"]

    final_episodes = availability["adjusted_episodes"] if availability["adjusted_episodes"] is not None else auto_episodes
    needs_review = availability["status"] == "needs_review"

    entry = add_tracked_title(
        tmdb_id, ttype, title, year=year, poster=poster, quality=quality,
        # Tant que la vérification Plex est incertaine, on désactive
        # l'auto-download par prudence (mieux vaut manquer un cycle que
        # télécharger un doublon) — l'admin le réactive en corrigeant la
        # demande, ce qui efface aussi le signalement (voir update_tracked_title).
        auto_download=not needs_review, source="ddl",
        auto_season=auto_season, auto_episodes=final_episodes,
        origin="whatsapp", requested_by=requested_by,
        review_status="needs_review" if needs_review else "",
        review_reason=availability["message"] if needs_review else "",
    )

    if needs_review:
        return "needs_review", entry, availability["message"]
    if availability["status"] == "partial":
        return "created", entry, availability["message"]

    season_bit = f" (saison {auto_season}, épisodes {auto_episodes or 'tous'})" if ttype == "tv" else ""
    return "created", entry, f"« {title} »{season_bit} ajouté en téléchargement automatique ({quality})."


def remove_tracked_title(tmdb_id, ttype, quality=""):
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return
    quality = (quality or "").strip()
    config = load_config()
    config["tracked_titles"] = [
        t for t in config.get("tracked_titles", [])
        if not (t.get("tmdb_id") == tmdb_id and t.get("type") == ttype and (t.get("quality") or "") == quality)
    ]
    save_config(config)


def refresh_tracked_hydracker_ids():
    """Re-résout l'id Hydracker des titres suivis qui n'en ont pas encore (titre
    pas encore présent sur Hydracker au moment de l'ajout). Appelé périodiquement."""
    config = load_config()
    tracked = config.get("tracked_titles", [])
    token = config.get("hydracker_api_token", "").strip()
    if not token:
        return
    changed = False
    for t in tracked:
        if t.get("hydracker_id") is None:
            hid = _resolve_hydracker_id(t.get("tmdb_id"), t.get("type"), token)
            if hid is not None:
                t["hydracker_id"] = hid
                changed = True
    if changed:
        save_config(config)


# ===== Téléchargement automatique DDL (JDownloader) =====

def parse_episode_spec(spec):
    """Transforme une spec d'épisodes ("1,3,5-8") en set d'entiers. Renvoie None
    si la spec est vide (= tous les épisodes de la ou des saison(s) choisie(s))."""
    spec = (spec or "").strip()
    if not spec:
        return None
    episodes = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, _, end_str = part.partition("-")
            try:
                start, end = int(start_str), int(end_str)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            episodes.update(range(start, end + 1))
        else:
            try:
                episodes.add(int(part))
            except ValueError:
                continue
    return episodes or None


def _format_episode_spec(numbers):
    """Inverse de parse_episode_spec : compacte une liste d'entiers en spec
    "1,3,5-8" (plages consécutives regroupées). Utilisé pour recalculer les
    épisodes restants après retrait de ceux déjà présents dans Plex."""
    numbers = sorted(set(numbers))
    if not numbers:
        return ""
    ranges = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((start, prev))
        start = prev = n
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def _episode_int(link):
    try:
        return int(link.get("episode"))
    except (TypeError, ValueError):
        return None


def jd_destination_for(media_type, clean_title, config):
    """Dossier de destination JDownloader selon le type de média et les réglages.
    Port serveur de la logique JS `jdDestination`, pour l'auto-download."""
    is_series = media_type == "series"
    base = (config.get("jd_series_folder") if is_series else config.get("jd_movies_folder")) or ""
    base = base.strip()
    if not base:
        return ""
    base = re.sub(r"[\\/]+$", "", base)
    sub = config.get("jd_series_subfolder", True) if is_series else config.get("jd_movies_subfolder", True)
    if sub and clean_title:
        sep = "\\" if "\\" in base else "/"
        return f"{base}{sep}{clean_title}"
    return base


def _lang_pref_key(link):
    """Clé de tri des liens : ceux en français/multi d'abord (0), le reste (1)."""
    langs = " ".join(link.get("langs") or [])
    return 0 if _FRENCH_LANG_RE.search(langs) else 1


def _size_rule_ok(link, media_type, size_rules):
    """Vérifie qu'un lien DDL respecte toutes les règles de taille globales
    (Configuration → Règles de taille) dont le filtre qualité correspond (même
    logique de sous-chaîne que le filtre qualité d'un titre suivi) et dont le
    type de média correspond ("any" = tous types) — ex. une règle "2160p" / 6 Go
    exclut un remux BluRay 2160p de 60 Go, sans toucher aux autres qualités.
    Une règle dont on ne connaît pas la taille du lien ne bloque jamais (mieux
    vaut autoriser que tout bloquer par excès de prudence sur une donnée absente)."""
    if not size_rules:
        return True
    size_bytes = link.get("size_bytes")
    lquality = normalize(link.get("quality") or "")
    for rule in size_rules:
        rquality = normalize(rule.get("quality") or "")
        rtype = rule.get("media_type") or "any"
        if rquality and rquality not in lquality:
            continue
        if rtype != "any" and rtype != media_type:
            continue
        max_gb = rule.get("max_gb")
        if not max_gb or size_bytes is None:
            continue
        if size_bytes > float(max_gb) * (1024 ** 3):
            return False
    return True


def _select_ddl_links(links, entry, media_type, size_rules=None):
    """Filtre et regroupe les liens DDL Hydracker selon la config d'auto-download
    d'un titre suivi (qualité voulue, et pour les séries saison/épisodes choisis)
    et les règles de taille globales (voir _size_rule_ok).

    Renvoie une liste de groupes [(clé, [liens...])] : un groupe par cible à
    télécharger (un film, un épisode, ou un pack saison complète), chaque groupe
    trié par préférence de langue — l'appelant n'envoie qu'un lien gratuit par
    groupe. Pour les séries sans épisodes précis demandés, un pack saison
    complète, s'il existe, est préféré aux épisodes un par un (évite les doublons)."""
    nquality = normalize((entry.get("quality") or "").strip())
    season = (entry.get("auto_season") or "").strip()
    episodes = parse_episode_spec(entry.get("auto_episodes"))
    size_rules = size_rules or []

    def ok_quality(link):
        return not nquality or nquality in normalize(link.get("quality") or "")

    def ok_season(link):
        return not season or str(link.get("saison")) == season

    def ok_size(link):
        return _size_rule_ok(link, media_type, size_rules)

    pool = [
        l for l in links
        if ok_quality(l) and ok_size(l) and (media_type != "series" or ok_season(l))
    ]

    if media_type != "series":
        return [(("single",), sorted(pool, key=_lang_pref_key))] if pool else []

    if episodes is not None:
        # Épisodes précis : on exclut les packs saison complète.
        chosen = [l for l in pool if not l.get("full_saison") and _episode_int(l) in episodes]
    else:
        packs = [l for l in pool if l.get("full_saison")]
        chosen = packs if packs else [l for l in pool if not l.get("full_saison")]

    groups = {}
    order = []
    for link in chosen:
        if link.get("full_saison"):
            key = ("full", str(link.get("saison")))
        else:
            key = ("ep", str(link.get("saison")), str(_episode_int(link)))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(link)

    return [(key, sorted(groups[key], key=_lang_pref_key)) for key in order]


def auto_download_ddl(article, entry, config):
    """Envoie à JDownloader les liens DDL Hydracker d'un article correspondant à
    un titre suivi configuré en auto-download DDL, selon sa config (qualité,
    saison/épisodes). N'envoie QUE des liens gratuits (compte hébergeur perso /
    lien direct) : un lien qui passerait par le débrideur facturé est sauté.

    Renvoie (status, message) avec status dans {"sent", "skipped", "error"}.
    "error" signale un souci transitoire (config/token/réseau) qu'il vaut la
    peine de réessayer plus tard ; "skipped" un cas définitif (rien de gratuit à
    prendre) qu'on ne réessaiera pas."""
    token = config.get("hydracker_api_token", "").strip()
    if not token:
        return "error", "token Hydracker manquant"
    jd_email = config.get("jd_email", "")
    jd_password = config.get("jd_password", "")
    jd_device = config.get("jd_device", "")
    if not (jd_email and jd_password and jd_device):
        return "error", "JDownloader non configuré"

    title_id = extract_hydracker_title_id(article.get("url"))
    if not title_id:
        # DDL = Hydracker uniquement (Torr9 ne fournit que des torrents).
        return "skipped", "article non-Hydracker (DDL indisponible)"

    media_type = detect_media_type(article.get("title", ""), article.get("feed_name", ""))
    clean_title = clean_media_name(article.get("title", ""), media_type)
    # Dossier propre au titre suivi s'il est renseigné (ex. ranger une série
    # dans son propre dossier), sinon le dossier calculé selon le type de média.
    custom_folder = (entry.get("auto_folder") or "").strip()
    destination = custom_folder or jd_destination_for(media_type, clean_title, config)

    links, err = fetch_hydracker_links(title_id, token)
    if err:
        return "error", err

    size_rules = config.get("size_rules", [])
    groups = _select_ddl_links(links, entry, media_type, size_rules)
    if not groups:
        return "skipped", "aucun lien DDL ne correspond (qualité/saison/épisodes/taille)"

    sent = 0
    skipped_paid = 0
    for _key, group in groups:
        for link in group:
            result, rerr = resolve_hydracker_link(link.get("id"), token)
            time.sleep(1.0)  # respecte la limite ~1 req/s de Hydracker
            if rerr or not result:
                continue
            url = result.get("direct_url") or result.get("raw_url")
            if not url:
                continue
            billing = result.get("billing")
            free = result.get("source") in ("personal", "direct_url") and (not billing or billing == "none")
            if not free:
                # "Gratuit uniquement" : on ne déclenche pas le débrideur facturé.
                skipped_paid += 1
                continue
            ok, _jderr = jd_send_links(
                jd_email, jd_password, jd_device, url,
                package_name=clean_title or None,
                destination_folder=destination or None,
                autostart=True,
            )
            if ok:
                sent += 1
                break  # une cible envoyée : on passe au groupe suivant

    if sent:
        msg = f"{sent} lien(s) envoyé(s) à JDownloader → {destination or 'dossier par défaut'}"
        if skipped_paid:
            msg += f" ({skipped_paid} sauté(s) car débrideur facturé)"
        return "sent", msg
    if skipped_paid:
        return "skipped", "liens disponibles mais seulement via débrideur facturé (sautés)"
    return "skipped", "aucun lien gratuit trouvé"


def _mark_auto_downloaded(conn, article_id):
    with _lock:
        conn.execute("UPDATE articles SET auto_downloaded = 1 WHERE id = ?", (article_id,))
        conn.commit()


def _run_and_mark_auto_download(conn, article, entry, config):
    """Lance auto_download_ddl pour un article et marque l'article comme traité
    (sauf erreur transitoire, qu'on laisse réessayable). Renvoie le status."""
    try:
        status, message = auto_download_ddl(article, entry, config)
    except Exception as exc:
        status, message = "error", str(exc)
    print(f"[auto-dl] {entry.get('title', '')} — {status} : {message}")
    if status in ("sent", "skipped"):
        _mark_auto_downloaded(conn, article["id"])
    return status


def trigger_auto_download_for_entry(entry):
    """Déclenche l'auto-download DDL pour les articles Hydracker déjà en base qui
    correspondent à ce titre suivi et n'ont pas encore été auto-téléchargés.
    Utilisé à l'activation de l'auto-download sur un titre déjà présent dans les
    flux (sans ça, un titre Hydracker déjà vu ne partirait jamais, la
    déduplication par URL l'empêchant de réapparaître). Renvoie (sent, skipped,
    errors)."""
    if not (entry.get("auto_download") and entry.get("source") == "ddl"):
        return 0, 0, 0
    hid = entry.get("hydracker_id")
    if hid is None:
        return 0, 0, 0

    config = load_config()
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, url, title, feed_name FROM articles "
        "WHERE auto_downloaded = 0 AND url LIKE '%hydracker.com/titles/%'"
    ).fetchall()

    sent = skipped = errors = 0
    for row in rows:
        if extract_hydracker_title_id(row["url"]) != str(hid):
            continue
        article = {"id": row["id"], "url": row["url"], "title": row["title"], "feed_name": row["feed_name"]}
        status = _run_and_mark_auto_download(conn, article, entry, config)
        if status == "sent":
            sent += 1
        elif status == "skipped":
            skipped += 1
        else:
            errors += 1
    conn.close()
    return sent, skipped, errors


def start_auto_download_for_entry(entry):
    """Lance trigger_auto_download_for_entry dans un thread (le déclenchement
    rétroactif enchaîne des appels réseau lents : on ne bloque pas la requête)."""
    if not (entry and entry.get("auto_download") and entry.get("source") == "ddl"
            and entry.get("hydracker_id") is not None):
        return False
    threading.Thread(target=trigger_auto_download_for_entry, args=(entry,), daemon=True).start()
    return True


def resolve_title_tmdb(raw_title, api_key):
    """(tmdb_id, tmdb_type) d'un titre de release non-Hydracker via recherche
    TMDB sur le titre nettoyé (préférence à l'année du titre si présente). Mis en
    cache (y compris les non-trouvés). (None, None) si rien/erreur."""
    if not api_key:
        return None, None
    clean = clean_release_title(raw_title)
    if not clean:
        return None, None

    conn = get_connection()
    row = conn.execute(
        "SELECT tmdb_id, tmdb_type FROM title_tmdb_cache WHERE clean_title = ? COLLATE NOCASE",
        (clean,),
    ).fetchone()
    if row is not None:
        conn.close()
        return row["tmdb_id"], row["tmdb_type"]

    results, _ = tmdb_search(clean, api_key)
    tmdb_id, tmdb_type = None, None
    if results:
        chosen = results[0]
        year = _YEAR_RE.search(raw_title or "")
        if year:
            for r in results:
                if r["year"] == year.group(0):
                    chosen = r
                    break
        tmdb_id, tmdb_type = chosen["tmdb_id"], chosen["type"]

    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO title_tmdb_cache (clean_title, tmdb_id, tmdb_type) VALUES (?, ?, ?)",
            (clean, tmdb_id, tmdb_type),
        )
        conn.commit()
    conn.close()
    return tmdb_id, tmdb_type


def match_tracked_titles(title, summary, link, tracked_titles, tmdb_key):
    """Identifie exactement à quel(s) titre(s) suivi(s) correspond un article.

    - Item Hydracker (id présent dans le lien) : correspond si son id Hydracker
      égale le `hydracker_id` résolu d'un titre suivi. Exact, aucun appel.
    - Item non-Hydracker (ex. Torr9) : pré-filtre par le nom du titre suivi (pour
      éviter une recherche TMDB inutile), puis confirmation exacte par (tmdb_id,
      type) résolu du titre de release.

    Renvoie (confirmed, quality_rejected) : deux listes d'*entrées* de titres
    suivis (les dicts de config, pas seulement leurs noms — l'appelant a ainsi
    accès à la config d'auto-download). Un titre avec une qualité imposée n'est
    confirmé que si cette qualité figure aussi dans le titre/résumé (sinon compté
    comme "qualité refusée")."""
    confirmed = []
    quality_rejected = []
    if not tracked_titles:
        return confirmed, quality_rejected

    hydra_id = extract_hydracker_title_id(link)
    haystack = normalize(f"{title} {summary}")
    article_tmdb = None  # (id, type) résolu à la demande, une seule fois par article

    for t in tracked_titles:
        if hydra_id is not None:
            identity = t.get("hydracker_id") is not None and str(t["hydracker_id"]) == str(hydra_id)
        else:
            name = normalize(t.get("title", ""))
            identity = False
            if name and name in haystack:
                if article_tmdb is None:
                    article_tmdb = resolve_title_tmdb(title, tmdb_key)
                identity = (
                    article_tmdb[0] is not None
                    and article_tmdb[0] == t.get("tmdb_id")
                    and article_tmdb[1] == t.get("type")
                )
        if not identity:
            continue

        quality = (t.get("quality") or "").strip()
        # La qualité ne filtre que les releases classiques (Torr9), dont le titre
        # porte le tag qualité. Pour Hydracker, la qualité vit au niveau des liens
        # (choisie au moment du téléchargement), donc on ne rejette jamais un item
        # Hydracker sur la qualité — sinon un titre Hydracker ne matcherait jamais.
        if quality and hydra_id is None and normalize(quality) not in haystack:
            quality_rejected.append(t)
        else:
            confirmed.append(t)

    return confirmed, quality_rejected


def rematch_all():
    """Recalcule les correspondances de tous les articles stockés selon les
    titres suivis actuels. Appelé après ajout/suppression d'un titre suivi pour
    que les articles déjà en base (re)basculent dans/hors des Correspondances."""
    config = load_config()
    tracked = config.get("tracked_titles", [])
    tmdb_key = config.get("tmdb_api_key", "").strip()

    conn = get_connection()
    rows = conn.execute("SELECT id, title, summary, url FROM articles").fetchall()
    for row in rows:
        confirmed, rejected = match_tracked_titles(
            row["title"], row["summary"] or "", row["url"] or "", tracked, tmdb_key
        )
        quality_ok = 0 if (rejected and not confirmed) else 1
        with _lock:
            conn.execute(
                "UPDATE articles SET matched_keywords = ?, quality_ok = ? WHERE id = ?",
                (", ".join(e.get("title", "") for e in confirmed), quality_ok, row["id"]),
            )
            conn.commit()
    conn.close()


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


def detect_media_type(title, feed_name=""):
    """Devine si un article est une série ou un film. Priorité au motif
    saison/épisode (SxxExx) dans le titre, sinon on se base sur le nom du flux
    ("Séries"/"Series" vs "Films"). Renvoie "series" ou "movie"."""
    if _SEASON_EPISODE_RE.search(title or ""):
        return "series"
    haystack = normalize(feed_name)  # accents retirés : "Séries" -> "series"
    if "seri" in haystack:
        return "series"
    return "movie"


def _sanitize_folder(name):
    """Nettoie un nom pour en faire un nom de dossier valide (retire les
    caractères interdits sous Windows/Unix)."""
    return re.sub(r'[\\/:*?"<>|]+', " ", name or "").strip().rstrip(".")


def clean_media_name(title, media_type):
    """Nom de dossier propre pour un média, ex. "Play Dirty (2025)" pour un
    film (avec l'année si présente), "House of the Dragon" pour une série."""
    name = clean_release_title(title)
    if media_type == "movie":
        year = _YEAR_RE.search(title or "")
        if year and year.group(0) not in name:
            name = f"{name} ({year.group(0)})"
    return _sanitize_folder(name)


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

    try:
        conn = get_connection()
    except sqlite3.OperationalError as exc:
        print(f"[poster] base de données indisponible, affiche ignorée pour ce cycle : {exc}")
        return None

    try:
        row = conn.execute(
            "SELECT poster_url FROM poster_cache WHERE clean_title = ? COLLATE NOCASE",
            (clean_title,),
        ).fetchone()
        if row is not None:
            return row["poster_url"]

        poster_url = fetch_poster_from_tmdb(clean_title, api_key)
        with _lock:
            conn.execute(
                "INSERT OR IGNORE INTO poster_cache (clean_title, poster_url) VALUES (?, ?)",
                (clean_title, poster_url),
            )
            conn.commit()
        return poster_url
    except sqlite3.OperationalError as exc:
        # Une affiche non mise en cache n'est pas grave (re-tentée au prochain
        # cycle) : ça ne doit jamais faire planter tout le fetch, encore moins
        # tout le démarrage de l'appli (ce cycle est appelé de façon
        # synchrone avant que le serveur ne réponde — voir app.py).
        print(f"[poster] erreur base de données, affiche ignorée pour ce cycle : {exc}")
        return None
    finally:
        conn.close()


def fetch_once():
    """Parcourt tous les flux configurés et enregistre tous les titres rencontrés
    (déduplication par URL). Chaque article garde la trace des mots-clés qui ont
    matché (vide si aucun) et si son nom passe les filtres de qualité — utilisé
    pour distinguer la page "Correspondances" de la page "Tous les titres".
    Relit config.json à chaque appel : éditer le fichier suffit, pas besoin de
    relancer l'appli."""
    config = load_config()
    tracked_titles = config.get("tracked_titles", [])
    tmdb_key = config.get("tmdb_api_key", "").strip()
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

            try:
                _process_feed_entry(
                    conn, config, tracked_titles, tmdb_key,
                    feed_name, feed_url, title, summary, link, entry,
                )
                new_count += 1
            except _SkipEntry:
                continue
            except sqlite3.OperationalError as exc:
                # Base momentanément verrouillée (accès concurrent, hoquet du
                # système de fichiers...) : cet article sera retenté au
                # prochain cycle plutôt que de faire planter tout le fetch —
                # et, pire, le démarrage de l'appli (voir app.py, où ce
                # premier cycle est appelé de façon synchrone).
                print(f"[fetch] base de données verrouillée, article ignoré pour ce cycle ({title!r}) : {exc}")
                continue

    conn.close()
    return new_count


class _SkipEntry(Exception):
    """Signal interne : cet article de flux n'a rien de nouveau à insérer
    (déjà vu/écarté, ou URL déjà connue) — ne compte pas comme une erreur."""


def _process_feed_entry(conn, config, tracked_titles, tmdb_key, feed_name, feed_url, title, summary, link, entry):
    """Traite un item de flux : matching, résolution d'affiche, insertion en
    base, puis déclenchement de l'auto-download DDL si applicable. Isolé de
    fetch_once() pour que chaque article soit protégé individuellement contre
    un verrou SQLite transitoire (voir l'appelant)."""
    if is_dismissed(conn, link):
        raise _SkipEntry

    confirmed, quality_rejected = match_tracked_titles(title, summary, link, tracked_titles, tmdb_key)
    matched = [e.get("title", "") for e in confirmed]
    quality_ok = not quality_rejected or bool(confirmed)
    published = entry.get("published", entry.get("updated", ""))
    published_at = extract_published_at(entry)
    download_url = extract_download_url(entry)
    # Affiche : d'abord une image déjà embarquée dans le flux (gratuit,
    # ex. Hydracker), sinon recherche TMDB pour les titres qui matchent
    # un titre suivi (pour limiter les appels API).
    poster_url = extract_poster_from_summary(summary)
    if not poster_url and matched:
        poster_url = get_poster_url(title, config)

    try:
        with _lock:
            cursor = conn.execute(
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
    except sqlite3.IntegrityError:
        raise _SkipEntry  # URL déjà connue

    # Téléchargement auto DDL : dès qu'un nouvel article correspond à un
    # titre suivi configuré en auto-download DDL, on lance l'envoi vers
    # JDownloader (Hydracker uniquement). Un seul déclenchement par
    # article suffit (même titre × qualités → même id Hydracker).
    auto_entry = next(
        (e for e in confirmed if e.get("auto_download") and e.get("source") == "ddl"),
        None,
    )
    if auto_entry is not None and extract_hydracker_title_id(link):
        article = {"id": cursor.lastrowid, "url": link, "title": title, "feed_name": feed_name}
        _run_and_mark_auto_download(conn, article, auto_entry, config)


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
            # Résout l'id Hydracker des titres suivis encore non résolus (titre
            # devenu disponible sur Hydracker depuis son ajout).
            refresh_tracked_hydracker_ids()
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
