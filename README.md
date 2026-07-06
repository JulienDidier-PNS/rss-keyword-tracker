# Veille RSS par mots-clés

Surveille une liste de flux RSS, et garde les articles dont le titre ou le résumé
contient un de tes mots-clés. Dashboard web pour consulter les résultats, et
gestion des flux/mots-clés/filtres de qualité directement depuis l'interface
(pas besoin d'éditer `config.json` à la main, mais ça marche aussi).

## Les deux pages

- **Correspondances** (`/`) : uniquement les articles qui matchent un
  mot-clé (et sa qualité associée, si définie).
- **Tous les titres** (`/all`) : tous les articles vus dans tes flux, avec un
  champ de recherche par titre et un filtre par flux. Les articles dont le
  texte matche un mot-clé mais pas sa qualité associée y apparaissent avec un
  badge "qualité ignorée", pour comprendre pourquoi ils ne sont pas dans les
  correspondances.

Les deux pages affichent une grille d'affiches paginée (24 par page, voir
`PAGE_SIZE` dans `app.py`), triée par date de publication réelle de l'article
(la plus récente en haut — pas la date à laquelle on l'a récupérée, pour que
plusieurs épisodes récupérés dans le même cycle restent dans le bon ordre),
avec les filtres actifs conservés en changeant de page. L'en-tête affiche
aussi un décompte en direct avant la prochaine vérification automatique des
flux. Cliquer sur une carte ouvre une fiche détaillée (affiche en grand,
date de publication et d'ajout, qualité détectée dans le nom, résumé, lien
vers la source).

Note : la qualité associée à un mot-clé s'applique aux articles au moment où
ils sont récupérés — la modifier ne change pas rétroactivement le statut des
articles déjà enregistrés, seulement les prochains.

## Sécurité

`config.json` (flux avec passkey, clé TMDB) n'est jamais commité — il est dans
`.gitignore`. Si tu clones ce repo sur un nouveau poste, recrée-le à partir de
`config.example.json` (voir plus bas) et n'y mets que tes propres secrets.
Ne mets jamais de vraie clé/passkey dans `config.example.json` ni dans le code.

## Affiches

Deux sources, utilisées dans cet ordre :

1. **Image embarquée dans le flux** — certains flux (ex. Hydracker) incluent
   déjà une `<img>` dans la description de chaque article. Récupérée
   automatiquement, gratuitement, sans configuration.
2. **TMDB** — pour les flux qui n'embarquent pas d'image (ex. les flux
   torrent avec des noms de release comme `House.of.the.Dragon.S03E03...`),
   utilisé seulement pour les articles qui matchent un mot-clé (pour limiter
   les appels API) :
   1. Créer un compte gratuit sur [themoviedb.org](https://www.themoviedb.org/)
      puis récupérer une clé API sur
      [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api).
   2. La coller dans le bloc "Affiches" en bas du dashboard, puis "Enregistrer".
   3. Cliquer sur "Récupérer les affiches manquantes" pour les articles déjà
      enregistrés — les nouveaux récupèrent leur affiche automatiquement dès
      leur découverte.

   Le titre de recherche est extrait automatiquement du nom de la release
   (coupé avant le numéro de saison/épisode, l'année, ou le premier tag de
   qualité rencontré), par ex. `House.of.the.Dragon.S03E03.MULTi.1080p...`
   devient `House of the Dragon`. Les résultats sont mis en cache par titre
   pour éviter d'interroger TMDB à chaque nouvel épisode d'une même série.

Sans image embarquée ni clé TMDB, l'appli fonctionne normalement, juste sans
affiche (icône 🎬 à la place).

## Configuration (`config.json`)

`config.json` contient tes flux (parfois avec passkey), tes mots-clés et ta
clé TMDB — il n'est **pas** versionné (voir `.gitignore`) pour éviter de
publier des secrets sur GitHub. `config.example.json` est le modèle versionné,
sans secrets :

```json
{
  "poll_interval_seconds": 300,
  "feeds": [
    { "name": "Le Monde", "url": "https://www.lemonde.fr/rss/une.xml" }
  ],
  "keywords": [
    { "keyword": "intelligence artificielle", "quality": "" },
    { "keyword": "house of the dragon", "quality": "1080p" }
  ],
  "tmdb_api_key": ""
}
```

Au premier lancement, si `config.json` n'existe pas encore, l'appli le crée
automatiquement à partir de `config.example.json`. Tu peux aussi le faire
toi-même :

```powershell
copy config.example.json config.json
```

Puis personnalise `config.json` (ou utilise la page Configuration du
dashboard, qui écrit dans ce même fichier).

- `poll_interval_seconds` : fréquence de vérification des flux (300 = 5 minutes).
- Chaque mot-clé porte sa propre qualité optionnelle (`quality`), choisie
  parmi une liste prédéfinie (`720p`, `1080p`, `2160p` — voir
  `QUALITY_CHOICES` dans `app.py` pour l'étendre). Laisse `quality` vide pour
  accepter le mot-clé dans n'importe quelle qualité (utile pour un flux
  "propre" comme Hydracker qui n'a pas ce genre de tag dans ses titres). Un
  article n'est gardé dans les correspondances que si **son mot-clé ET sa
  qualité associée** (si définie) sont tous les deux présents. Pour accepter
  plusieurs qualités pour un même mot-clé (ex. 1080p ou 2160p), ajoute-le deux
  fois avec une qualité différente à chaque fois.
- Le matching est insensible aux accents, à la casse, et aux séparateurs de
  type release (points/tirets/underscores) : "Cybersécurité" == "cybersecurite",
  et "house of the dragon" matche "House.of.the.Dragon.S02E05.FRENCH.1080p".
- Le matching est un "contient" (substring) : le mot-clé "climat" matchera aussi
  "climatique" ou "climatiseur". Utilise des mots-clés plus précis si tu veux
  éviter les faux positifs.
- Le fichier est relu à chaque cycle de vérification : pas besoin de relancer
  l'appli après modification, le changement est pris en compte au prochain
  passage (ou en cliquant sur "Vérifier maintenant" dans le dashboard).
- Les anciennes config (`"keywords": ["..."]` + `"quality_filters": [...]`
  séparés) sont migrées automatiquement au premier chargement.

## Installation sur Windows

1. Installer [Python 3.11+](https://www.python.org/downloads/) en cochant
   "Add python.exe to PATH" pendant l'installation.
2. Ouvrir PowerShell dans le dossier du projet.
3. Créer et activer l'environnement virtuel :
   ```powershell
   python -m venv venv
   venv\Scripts\activate
   ```
4. Installer les dépendances :
   ```powershell
   pip install -r requirements.txt
   ```
5. Adapter `config.json` à tes flux et mots-clés.

## Lancer l'appli

**En test/dev** (rechargement facile, accessible sur `http://localhost:8000`) :
```powershell
python app.py
```

**En hébergement plus robuste** (via waitress, un vrai serveur WSGI pour
Windows), en double-cliquant sur `run.bat`, ou en ligne de commande :
```powershell
waitress-serve --host=0.0.0.0 --port=8000 app:app
```

Une fois lancé, ouvrir `http://localhost:8000` (ou `http://IP_DU_SERVEUR:8000`
depuis un autre appareil du réseau — penser à autoriser le port 8000 dans le
pare-feu Windows si besoin).

## Faire tourner l'appli en permanence

Deux options simples, du plus léger au plus robuste :

- **Planificateur de tâches Windows** : créer une tâche déclenchée "au
  démarrage de l'ordinateur" ou "à l'ouverture de session", qui exécute
  `run.bat`. L'appli redémarre automatiquement si le serveur reboote.
- **NSSM** ([nssm.cc](https://nssm.cc)) : enregistre `run.bat` (ou directement
  `waitress-serve`) comme un vrai service Windows, avec redémarrage
  automatique en cas de plantage. Recommandé si le serveur doit tourner sans
  session utilisateur ouverte.

## Données

Les articles trouvés sont stockés dans `articles.db` (SQLite), à la racine du
projet. Chaque URL n'est enregistrée qu'une fois (déduplication automatique).

- **Persistance** : `articles.db` est un fichier normal qui survit aux
  redémarrages de l'appli (arrêt/relance du process, reboot du serveur…). Il
  n'est jamais vidé par le code. Il est aussi gitignoré (comme `config.json`)
  car c'est une donnée locale, pas du code — si ton processus de mise à jour
  supprime et recrée le dossier du projet (au lieu d'un `git pull` sur un
  dossier existant), ce fichier non versionné disparaît avec, ce qui donne
  l'impression que tout est "reparti à 0". Mets à jour via `git pull` dans le
  même dossier pour conserver l'historique.
- **Rétention** : les articles vus il y a plus de 30 jours sont supprimés
  automatiquement à chaque cycle de vérification (voir `RETENTION_DAYS` dans
  `fetcher.py` pour changer cette valeur).
- **Suppression = définitif** : supprimer une ligne depuis le dashboard
  (icône ✕) retient son URL de façon permanente (table `dismissed_urls`) —
  elle ne réapparaîtra jamais, même si le flux la republie plus tard. Utile
  pour écarter un faux positif une bonne fois pour toutes.
- **Disponible dès le démarrage** : un premier pull est fait de façon
  synchrone avant que le serveur ne commence à répondre, pour que les
  résultats soient là dès le premier chargement de la page (pas besoin de
  cliquer sur "Vérifier maintenant" après un redémarrage).
