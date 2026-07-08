"""Communication avec JDownloader via l'API My.JDownloader (relais cloud
officiel api.jdownloader.org). On s'authentifie avec l'email + mot de passe du
compte my.jdownloader.org, puis on cible un "device" (l'instance JDownloader
enregistrée) par son nom pour lui envoyer des liens.

Le protocole (chiffrement AES + signature HMAC des requêtes) est entièrement
géré par la lib myjdapi ; ce module n'expose que ce dont l'appli a besoin :
tester la connexion / lister les appareils, et envoyer des liens au linkgrabber
avec un dossier de destination.
"""

import myjdapi
from myjdapi.myjdapi import MYJDException

# Identifiant d'application transmis à My.JDownloader (libre, sert à distinguer
# ce client des autres connectés au même compte).
APP_KEY = "rss-keyword-tracker"


def _connect(email, password):
    """Ouvre une session My.JDownloader et rafraîchit la liste des appareils.
    Lève MYJDException en cas d'échec (identifiants invalides, etc.)."""
    jd = myjdapi.Myjdapi()
    jd.set_app_key(APP_KEY)
    jd.connect(email, password)
    jd.update_devices()
    return jd


_MYJD_ERROR_MESSAGES = {
    "EMAIL_INVALID": "Email My.JDownloader invalide.",
    "EMAIL_FORBIDDEN": "Email My.JDownloader non confirmé ou bloqué.",
    "BAD_LOGIN": "Email ou mot de passe My.JDownloader incorrect.",
    "AUTH_FAILED": "Authentification My.JDownloader échouée (mot de passe ?).",
    "OFFLINE": "L'appareil JDownloader est hors ligne.",
    "TOO_MANY_REQUESTS": "Trop de requêtes vers My.JDownloader — réessaie plus tard.",
    "UNKNOWN": "Erreur inconnue côté My.JDownloader.",
}


def _friendly_error(exc):
    """Message court à partir d'une exception myjdapi. Les erreurs MYJD sont
    multi-lignes (SOURCE/TYPE/REQUEST_URL...) et l'URL contient la signature :
    on n'en garde que le TYPE, sans jamais réafficher l'URL signée."""
    text = str(exc).strip() or exc.__class__.__name__
    etype = None
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("TYPE:"):
            etype = line.split(":", 1)[1].strip().upper()
            break
    if etype:
        return _MYJD_ERROR_MESSAGES.get(etype, f"Erreur My.JDownloader ({etype}).")
    return f"Erreur My.JDownloader : {text.splitlines()[0][:120]}"


def jd_test_connection(email, password):
    """Teste la connexion et renvoie (devices, erreur). `devices` est la liste
    des appareils JDownloader du compte, chacun {name, id, type}."""
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return None, "Email et mot de passe My.JDownloader requis."
    try:
        jd = _connect(email, password)
        devices = jd.list_devices()
    except MYJDException as exc:
        return None, _friendly_error(exc)
    except Exception as exc:
        return None, f"Erreur My.JDownloader : {exc}"
    return devices, None


def jd_send_links(email, password, device_name, links, package_name=None,
                  destination_folder=None, autostart=False):
    """Envoie un ou plusieurs liens au linkgrabber de l'appareil `device_name`,
    avec un dossier de destination optionnel. `links` peut être une chaîne (une
    URL) ou une liste d'URLs. Renvoie (ok, erreur).

    overwritePackagizerRules=True : notre dossier de destination prime sur les
    règles Packagizer éventuellement configurées dans JDownloader."""
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False, "Email et mot de passe My.JDownloader requis."
    if not device_name:
        return False, "Aucun appareil JDownloader sélectionné."

    if isinstance(links, (list, tuple)):
        links = "\n".join(u for u in links if u)
    if not links:
        return False, "Aucun lien à envoyer."

    try:
        jd = _connect(email, password)
        device = jd.get_device(device_name)
        device.linkgrabber.add_links(
            [
                {
                    "autostart": bool(autostart),
                    "links": links,
                    "packageName": package_name or None,
                    "destinationFolder": destination_folder or None,
                    "overwritePackagizerRules": True,
                }
            ]
        )
    except MYJDException as exc:
        return False, _friendly_error(exc)
    except Exception as exc:
        return False, f"Erreur My.JDownloader : {exc}"
    return True, None
