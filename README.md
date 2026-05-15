# Veille IA — Agent quotidien autonome

Système de veille automatisée sur l'IA, Data, BI, Automatisation et Entrepreneuriat.
**100% gratuit**, fonctionne en local ou via GitHub Actions.

## Stack
- Python 3.10+
- Gemini 2.5 Flash (Google AI Studio — free tier)
- Flux RSS publics (aucune clé API requise)
- Gmail SMTP avec mot de passe d'application
- GitHub Actions pour l'exécution quotidienne

## Prérequis

1. Python 3.10+
2. Compte Google (pour Gemini + Gmail)
3. Compte GitHub (pour l'automatisation)

## Installation

```bash
git clone <repo-url>
cd veille-ia
pip install -r requirements.txt
```

## Configuration

### 1. Clé Gemini
Obtenir sur : https://aistudio.google.com/app/apikey

### 2. Mot de passe d'application Gmail
Activer la validation en 2 étapes, puis créer sur : https://myaccount.google.com/apppasswords

### 3. Fichier .env
```
cp .env.example .env
# Éditer .env avec vos valeurs
```

## Utilisation

### Test rapide (3 sources, 10 articles)
```bash
python veille_ia.py --test
```

### Collecte initiale complète (depuis janvier 2026)
```bash
python veille_ia.py
```

### Mise à jour quotidienne manuelle
```bash
python veille_update.py
```

## Automatisation GitHub Actions

1. Créer un repo GitHub privé
2. Pousser le projet : `git push`
3. Ajouter les 4 secrets dans **Settings > Secrets > Actions** :
   - `GEMINI_API_KEY`
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `EMAIL_DESTINATAIRE`
4. GitHub Actions se déclenche automatiquement chaque jour à 10h00 (Paris)

## Structure des fichiers

```
veille-ia/
├── veille_ia.py          # Script initialisation complète
├── veille_update.py      # Script mise à jour quotidienne
├── config.py             # Configuration, sources RSS, mots-clés
├── .env                  # Secrets (non versionné)
├── requirements.txt
└── data/
    ├── articles_history.json   # Tous les articles collectés
    ├── ranking_global.json     # Articles scorés, triés
    ├── articles_du_jour.json   # Historique des envois
    ├── stats.json              # Statistiques d'utilisation
    └── articles_generes/
        ├── index.html          # Index des articles générés
        └── YYYY-MM-DD_*.html   # Articles générés
```

## FAQ

**Q: Un flux RSS est bloqué ?**
Le script continue automatiquement (3 tentatives, backoff exponentiel). Les erreurs sont loguées dans `logs/veille.log`.

**Q: Rate limit Gemini ?**
Le script s'arrête automatiquement à 200 appels/jour (limite free tier : 250). Les articles non scorés sont traités progressivement.

**Q: L'email n'arrive pas ?**
Vérifier le mot de passe d'application Gmail. L'email de secours est sauvegardé dans `data/email_failed/`.

**Q: L'article HTML est introuvable ?**
Vérifier dans `data/articles_generes/`. L'index est dans `data/articles_generes/index.html`.
