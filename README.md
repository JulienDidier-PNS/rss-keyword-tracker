# Veille RSS par mots-clés

Surveille une liste de flux RSS, et garde les articles dont le titre ou le résumé
contient un de tes mots-clés. Dashboard web pour consulter les résultats, et
gestion des flux/mots-clés/filtres de qualité directement depuis l'interface
(pas besoin d'éditer `config.json` à la main, mais ça marche aussi).

## Les deux pages

- **Correspondances** (`/`) : uniquement les articles qui matchent un
  mot-clé et passent les filtres de qualité, si configurés.
- **Tous les titres** (`/all`) : tous les articles vus dans tes flux, avec un
  champ de recherche par titre et un filtre par flux. Les articles qui ont
  matché un mot-clé mais qui ont été écartés par un filtre de qualité y
  apparaissent avec un badge "qualité ignorée", pour comprendre pourquoi ils
  ne sont pas dans les correspondances.

Note : les filtres de qualité s'appliquent aux articles au moment où ils sont
récupérés — modifier la liste de filtres ne change pas rétroactivement le
statut des articles déjà enregistrés, seulement les prochains.

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

⚠️ Les `quality_filters` sont globaux à tous les flux : si tu en configures
(ex. `1080p`) et que tu ajoutes un flux dont les titres ne contiennent pas ce
genre de tag (ex. Hydracker, qui a des titres déjà propres comme "Badh"), ses
articles ne passeront jamais le filtre qualité et n'apparaîtront pas dans les
correspondances. Laisse `quality_filters` vide si tu mélanges des flux
torrent (noms de release) et des flux "propres" comme Hydracker.

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
  "keywords": ["intelligence artificielle", "cybersecurite"],
  "quality_filters": [],
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
- `quality_filters` : termes recherchés dans le nom (résolution, langue,
  codec…), ex. `1080p`, `MULTi`, `VOSTFR`. Vide = aucun filtrage. Sinon, un
  article n'apparaît dans les correspondances que si son nom contient au
  moins un de ces termes (liste blanche).
- Le matching est insensible aux accents, à la casse, et aux séparateurs de
  type release (points/tirets/underscores) : "Cybersécurité" == "cybersecurite",
  et "house of the dragon" matche "House.of.the.Dragon.S02E05.FRENCH.1080p".
- Le matching est un "contient" (substring) : le mot-clé "climat" matchera aussi
  "climatique" ou "climatiseur". Utilise des mots-clés plus précis si tu veux
  éviter les faux positifs.
- Le fichier est relu à chaque cycle de vérification : pas besoin de relancer
  l'appli après modification, le changement est pris en compte au prochain
  passage (ou en cliquant sur "Vérifier maintenant" dans le dashboard).

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
Tu peux supprimer une ligne directement depuis le dashboard (icône ✕).
