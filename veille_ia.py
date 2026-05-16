#!/usr/bin/env python3
"""
veille_ia.py — Initialisation complète du système de veille IA.
Collecte historique depuis janvier 2026, scoring Gemini, génération article du jour, email.
"""

import sys
import os
import json
import hashlib
import time
import re
import argparse
import logging
import smtplib
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import html as html_lib
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

# Force unbuffered stdout for real-time output in subprocess/CI
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

load_dotenv(Path(__file__).parent / ".env")

from config import (
    BASE_DIR, DATA_DIR, ARTICLES_GENERES_DIR, EMAIL_FAILED_DIR, LOGS_DIR,
    ARTICLES_HISTORY_FILE, RANKING_GLOBAL_FILE, ARTICLES_DU_JOUR_FILE,
    STATS_FILE, INDEX_HTML_FILE,
    GEMINI_MODELS, MAX_GEMINI_CALLS_PER_DAY, BATCH_SIZE, MAX_INITIAL_SCORING,
    HISTORY_START_DATE, RSS_TIMEOUT, RSS_MAX_RETRIES,
    KEYWORDS, RSS_SOURCES, TEST_SOURCES, SCORING_PROMPT
)

# ── Logging ──────────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "veille.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def safe_print(msg: str):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:max_len].rstrip("-")


def url_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        backup = path.with_suffix(f".corrupt.{int(time.time())}.json")
        path.rename(backup)
        log.error(f"JSON corrompu {path.name}, sauvegarde en {backup.name}: {e}")
        return default
    except Exception as e:
        log.error(f"Erreur lecture {path.name}: {e}")
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_stats() -> dict:
    default = {
        "total_articles_traites": 0,
        "total_appels_gemini": 0,
        "appels_gemini_aujourd_hui": 0,
        "derniere_date_reset": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "streak_jours": 0,
        "dernier_envoi": None,
    }
    return load_json(STATS_FILE, default)


def save_stats(stats: dict):
    save_json(STATS_FILE, stats)


def reset_daily_stats_if_needed(stats: dict) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if stats.get("derniere_date_reset") != today:
        stats["appels_gemini_aujourd_hui"] = 0
        stats["derniere_date_reset"] = today
    return stats


_KEYWORDS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b",
    re.IGNORECASE,
)

def article_matches_keywords(article: dict) -> bool:
    text = (article.get("titre", "") or "") + " " + (article.get("resume", "") or "")
    return bool(_KEYWORDS_RE.search(text))


def score_composite(a: dict) -> float:
    return (
        a.get("score", 0) * 0.4
        + a.get("pertinence_bi_consultant", 0) * 0.25
        + a.get("pertinence_passionné_ia", 0) * 0.2
        + a.get("pertinence_entrepreneur", 0) * 0.15
    )


# ── Article scraping ──────────────────────────────────────────────────────────
_SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0 (VeilleIA/1.0; +https://github.com/Quentin-Lo/veille-ia)"}
_SCRAPE_SELECTORS = ["article", "main", "[role='main']", ".post-content",
                     ".entry-content", ".article-body", ".content", "body"]

def scrape_article_text(url: str, max_chars: int = 2000) -> str:
    try:
        resp = requests.get(url, timeout=10, headers=_SCRAPE_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
            tag.decompose()
        for selector in _SCRAPE_SELECTORS:
            container = soup.select_one(selector)
            if container:
                text = re.sub(r'\s+', ' ', container.get_text(separator=" ", strip=True))
                if len(text) > 300:
                    return text[:max_chars]
        return ""
    except Exception:
        return ""


def enrich_articles_with_full_text(articles: list) -> list:
    """Scrape full text for articles that don't have it yet (parallel, best-effort)."""
    to_scrape = [a for a in articles if not a.get("full_text")]
    if not to_scrape:
        return articles

    safe_print(f"  Scraping du texte complet pour {len(to_scrape)} articles...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(scrape_article_text, a["url"]): a for a in to_scrape}
        for fut in as_completed(futures):
            article = futures[fut]
            try:
                text = fut.result()
                if text:
                    article["full_text"] = text
            except Exception:
                pass
    return articles


# ── Topic deduplication ───────────────────────────────────────────────────────
def deduplicate_by_topic(articles: list) -> list:
    """Keep only the top-scored article per topic (category + title word overlap > 40%)."""
    selected = []
    selected_words = []

    for article in articles:
        title_words = set(re.findall(r'\b\w{4,}\b', article.get("titre", "").lower()))
        category = article.get("categorie", "")
        duplicate = False
        for i, sw in enumerate(selected_words):
            if not sw or not title_words:
                continue
            overlap = len(title_words & sw) / max(len(title_words | sw), 1)
            if overlap > 0.4 and selected[i].get("categorie", "") == category:
                duplicate = True
                break
        if not duplicate:
            selected.append(article)
            selected_words.append(title_words)

    return selected


# ── RSS Collecte ──────────────────────────────────────────────────────────────
def fetch_feed(source: dict, start_date: str) -> list:
    url = source["url"]
    headers = {"User-Agent": "Mozilla/5.0 (VeilleIA/1.0; +https://github.com/veille-ia)"}
    for attempt in range(RSS_MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=RSS_TIMEOUT, headers=headers)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            articles = []
            cutoff = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            for entry in feed.entries:
                pub = None
                try:
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                        pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pub = None
                if pub is None or pub < cutoff:
                    continue
                link = entry.get("link", "")
                if not link:
                    continue
                resume = ""
                if hasattr(entry, "summary"):
                    resume = html_lib.unescape(re.sub(r"<[^>]+>", "", entry.summary))[:500]
                articles.append({
                    "url": link,
                    "url_hash": url_hash(link),
                    "titre": entry.get("title", "Sans titre"),
                    "resume": resume,
                    "date": pub.strftime("%Y-%m-%d"),
                    "source_name": source["name"],
                    "categorie_source": source["categorie"],
                    "score_pending": True,
                    "article_du_jour": False,
                })
            return articles
        except Exception as e:
            wait = 2 ** attempt
            log.warning(f"Flux {source['name']} tentative {attempt+1}/{RSS_MAX_RETRIES} echouee: {e}. Attente {wait}s")
            time.sleep(wait)
    return []


def collect_history(sources: list, start_date: str, test_mode: bool = False) -> list:
    history = load_json(ARTICLES_HISTORY_FILE, [])
    known_hashes = {a["url_hash"] for a in history}
    all_new = []
    total = len(sources)
    results = {}

    workers = min(8, total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_feed, s, start_date): s for s in sources}
        for fut in as_completed(futures):
            source = futures[fut]
            try:
                results[source["name"]] = fut.result()
            except Exception as e:
                log.warning(f"Erreur collecte {source['name']}: {e}")
                results[source["name"]] = []

    for i, source in enumerate(sources, 1):
        articles = results.get(source["name"], [])
        new_arts = [a for a in articles if a["url_hash"] not in known_hashes]
        for a in new_arts:
            known_hashes.add(a["url_hash"])
        all_new.extend(new_arts)
        safe_print(f"[{i}/{total}] {source['name']}: {len(new_arts)} nouveaux")

    history.extend(all_new)
    save_json(ARTICLES_HISTORY_FILE, history)
    log.info(f"Collecte terminee : {len(all_new)} nouveaux articles, total historique : {len(history)}")
    return history


# ── Gemini Scoring ────────────────────────────────────────────────────────────
def init_gemini() -> genai.GenerativeModel:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY manquant dans .env")
    genai.configure(api_key=api_key)

    # Log available models for diagnostics (no API call needed)
    try:
        available_names = [
            m.name.replace("models/", "")
            for m in genai.list_models()
            if "generateContent" in getattr(m, "supported_generation_methods", [])
            and any(tag in m.name for tag in ["flash", "pro"])
            and "tts" not in m.name and "image" not in m.name
            and "robotics" not in m.name and "lyria" not in m.name
        ]
        log.info(f"Modeles disponibles : {available_names[:10]}")
    except Exception:
        available_names = []

    for model_name in GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            test_resp = model.generate_content(
                "Reply with exactly: OK",
                generation_config={"max_output_tokens": 5}
            )
            if test_resp and test_resp.text:
                log.info(f"Modele Gemini utilise : {model_name}")
                safe_print(f"Modele Gemini : {model_name}")
                return model
        except Exception as e:
            err_short = str(e)[:120]
            log.warning(f"Modele {model_name} indisponible : {err_short}")
            time.sleep(2)
    raise RuntimeError("Aucun modele Gemini disponible")


def call_gemini_with_retry(model, prompt: str, stats: dict, max_retries: int = 4) -> str:
    for attempt in range(max_retries):
        try:
            stats["total_appels_gemini"] = stats.get("total_appels_gemini", 0) + 1
            stats["appels_gemini_aujourd_hui"] = stats.get("appels_gemini_aujourd_hui", 0) + 1
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower() or "Resource" in err_str:
                wait = 65 + (attempt * 30)
                log.warning(f"Rate limit Gemini, attente {wait}s (tentative {attempt+1}/{max_retries})")
                safe_print(f"  Rate limit Gemini, pause {wait}s...")
                time.sleep(wait)
            else:
                log.error(f"Erreur Gemini (tentative {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
    raise RuntimeError("Echec appel Gemini apres retries")


def extract_json_from_response(text: str):
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


def score_batch(model, batch: list, stats: dict) -> list:
    articles_for_prompt = [
        {
            "url_hash": a["url_hash"],
            "url": a["url"],
            "titre": a["titre"],
            "resume": a.get("full_text") or a.get("resume", ""),
            "date": a.get("date", ""),
        }
        for a in batch
    ]
    prompt = SCORING_PROMPT.replace("{ARTICLES_JSON}", json.dumps(articles_for_prompt, ensure_ascii=False))
    for attempt in range(2):
        try:
            raw = call_gemini_with_retry(model, prompt, stats)
            scored = extract_json_from_response(raw)
            if isinstance(scored, list):
                return scored
        except Exception as e:
            if attempt == 0:
                log.warning(f"JSON invalide, retry avec instruction explicite : {e}")
                prompt = "JSON only, no markdown, no comments.\n\n" + prompt
            else:
                log.error(f"Scoring batch echoue : {e}")
    return []


def score_articles(model, articles: list, stats: dict, max_articles: int = MAX_INITIAL_SCORING, history_file=None) -> list:
    to_score = [a for a in articles if a.get("score_pending", True)]
    to_score = [a for a in to_score if article_matches_keywords(a)]
    to_score.sort(key=lambda x: x.get("date", ""), reverse=True)
    to_score = to_score[:max_articles]

    safe_print(f"\nScoring de {len(to_score)} articles via Gemini (batches de {BATCH_SIZE})...")
    log.info(f"Scoring : {len(to_score)} articles a scorer")
    enrich_articles_with_full_text(to_score)

    scored_map = {}
    # Index by url_hash for O(1) lookup instead of O(n) scan per batch
    by_hash = {a["url_hash"]: a for a in articles}
    batches = [to_score[i:i+BATCH_SIZE] for i in range(0, len(to_score), BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        if stats.get("appels_gemini_aujourd_hui", 0) >= MAX_GEMINI_CALLS_PER_DAY:
            log.warning("Limite quotidienne Gemini atteinte, arret du scoring")
            break
        safe_print(f"  Batch {idx}/{len(batches)} ({len(batch)} articles)...")
        results = score_batch(model, batch, stats)
        for r in results:
            h = r.get("url_hash") or url_hash(r.get("url", ""))
            if h and h in by_hash:
                r["score_composite"] = score_composite(r)
                scored_map[h] = r
                a = by_hash[h]
                if a.get("score_pending", True):
                    a.update(r)
                    a["score_pending"] = False
        # Save intermediate progress every 5 batches to survive crashes
        if history_file and idx % 5 == 0:
            save_json(history_file, articles)
            log.info(f"Sauvegarde intermediaire : {len(scored_map)} articles scores")
        # 12s sleep → 5 RPM max, safe buffer for Gemini free-tier limits
        if idx < len(batches):
            time.sleep(12)

    save_stats(stats)
    return articles


# ── Génération article du jour ─────────────────────────────────────────────────
GENERATION_PROMPT = """Tu rediges une newsletter quotidienne de veille tech en francais. Ton lecteur est curieux et actif dans la tech, il lit vite et veut comprendre l'essentiel sans perdre de temps.

REGLES DE STYLE — applique-les sans exception :
- Ton naturel, direct, humain. Ni scolaire ni commercial.
- Phrases courtes. Une idee par phrase. Pas de longueurs inutiles.
- ZERO preamble : ne commence pas par "Bien sur", "Voici", "Absolument", "Bonjour". Commence directement par le titre.
- Pas de point d'exclamation a chaque phrase. Reserve-les aux vraies annonces importantes.
- Gras uniquement sur les mots vraiment cles, pas pour decorer.
- Listes avec tirets (-), jamais d'asterisques (*).
- Maximum 2 emojis par section, places avec intention, pas en decoration.
- Evite les superlatifs vides ("revolutionnaire", "incroyable", "fascinant"). Dis ce qui change concretement.

CONTENU :
- Article principal : {TITRE}
  Source : {SOURCE} | Date : {DATE} | Score : {SCORE}/10 | Categorie : {CATEGORIE}
  Lien : {URL}
  Contexte : {RESUME}

- Articles secondaires du jour : {SECONDARY_JSON}

- Tendances de la semaine (score >= 7) : {WEEKLY_JSON}

STRUCTURE OBLIGATOIRE — produis exactement ce Markdown :

## [Titre en francais, direct et precis — pas de jeu de mots force, pas de majuscules inutiles]

> [Une phrase d'accroche : l'enjeu central en 15 mots max. Factuelle et intrigante.]

### Ce qui s'est passe 🔍

[2-3 paragraphes. Raconte les faits dans l'ordre logique. Si un concept est technique, glisse une analogie courte entre parentheses ou apres un tiret. Pas de suspense artificiel : dis le principal des le premier paragraphe.]

### Pourquoi ca compte 🎯

[1-2 paragraphes. Dis precisement ce que ca change, pour qui, et pourquoi maintenant plutot qu'avant. Concret et sobre — pas "ca va tout changer", mais "ca permet de faire X sans avoir besoin de Y".]

### Ce que tu peux en faire 🛠️

- **[Action 1]** : une ligne, avec lien si disponible
- **[Action 2]** : outil ou ressource gratuite accessible aujourd'hui
- **Defi rapide** : une experience faisable en moins de 30 minutes

### A retenir 📖

- **[Terme 1]** : definition en une phrase simple, sans jargon dans la definition
- **[Terme 2]** : definition en une phrase simple

### Pour aller plus loin

- [[Titre de la ressource]]({URL}) — une ligne sur ce qu'on y apprend concretement

---

## Autres actus du jour 📰

[Pour chaque article secondaire : **Titre reformule en francais** suivi d'une ou deux phrases directes sur le contenu. Score entre parentheses a la fin. Pas d'exclamation systematique. Pas de jargon non explique.]

---

## Ce qui monte 📈

**[Tendance 1]** — deux phrases : ce qui se passe et pourquoi c'est a suivre.

**[Tendance 2]** — deux phrases concretes sur cette dynamique.

**A surveiller** — un sujet emergent explique en deux phrases max.
"""


def generate_article_du_jour(model, ranking: list, stats: dict, test_mode: bool = False):
    scored = [a for a in ranking if not a.get("score_pending", True) and not a.get("article_du_jour", False)]
    if not scored:
        log.warning("Aucun article disponible pour generation")
        return None

    scored.sort(key=lambda x: x.get("score_composite", 0), reverse=True)
    scored = deduplicate_by_topic(scored)
    main_article = scored[0]
    secondary = scored[1:7]

    from datetime import timedelta
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    weekly = [a for a in ranking if a.get("date", "") >= week_ago and a.get("score", 0) >= 7][:10]

    prompt = GENERATION_PROMPT.replace("{TITRE}", main_article.get("titre", ""))
    prompt = prompt.replace("{URL}", main_article.get("url", ""))
    prompt = prompt.replace("{RESUME}", main_article.get("resume", "")[:300])
    prompt = prompt.replace("{DATE}", main_article.get("date", ""))
    prompt = prompt.replace("{SCORE}", str(main_article.get("score", 0)))
    prompt = prompt.replace("{CATEGORIE}", main_article.get("categorie", ""))
    prompt = prompt.replace("{SCORE_BI}", str(main_article.get("pertinence_bi_consultant", 0)))
    prompt = prompt.replace("{SCORE_IA}", str(main_article.get("pertinence_passionné_ia", 0)))
    prompt = prompt.replace("{SCORE_E}", str(main_article.get("pertinence_entrepreneur", 0)))
    prompt = prompt.replace("{SOURCE}", main_article.get("source_name", ""))
    def slim(articles):
        return [{"titre": a.get("titre", ""), "url": a.get("url", ""),
                 "score": a.get("score", 0), "categorie": a.get("categorie", "")}
                for a in articles]

    prompt = prompt.replace("{SECONDARY_JSON}", json.dumps(slim(secondary), ensure_ascii=False))
    prompt = prompt.replace("{WEEKLY_JSON}", json.dumps(slim(weekly), ensure_ascii=False))

    safe_print("\nGeneration de l'article du jour via Gemini...")
    raw_content = call_gemini_with_retry(model, prompt, stats)
    save_stats(stats)

    return {
        "article": main_article,
        "contenu_markdown": raw_content,
    }


# ── HTML Generation ───────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{TITLE} — Veille IA</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Merriweather:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #f0f4f8;
    color: #1e293b;
    line-height: 1.75;
    font-size: 16px;
  }}

  /* Header */
  .site-header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
    color: white;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .site-header .logo {{ font-size: 1.1rem; font-weight: 700; letter-spacing: -0.3px; }}
  .site-header .tagline {{ font-size: 0.8rem; opacity: 0.65; margin-left: auto; }}

  /* Hero */
  .hero {{
    background: linear-gradient(135deg, #1e3a5f 0%, #1d4ed8 100%);
    color: white;
    padding: 52px 24px 44px;
    text-align: center;
  }}
  .hero .badge {{
    display: inline-block;
    background: rgba(255,255,255,0.18);
    border: 1px solid rgba(255,255,255,0.3);
    border-radius: 20px;
    padding: 4px 14px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    margin-bottom: 20px;
  }}
  .hero h1 {{
    font-family: 'Merriweather', Georgia, serif;
    font-size: clamp(1.5rem, 4vw, 2.2rem);
    font-weight: 700;
    line-height: 1.3;
    max-width: 720px;
    margin: 0 auto 20px;
  }}
  .hero .date-line {{
    font-size: 0.82rem;
    opacity: 0.7;
    margin-top: 16px;
  }}

  /* Container */
  .container {{
    max-width: 720px;
    margin: 0 auto;
    padding: 0 20px 60px;
  }}

  /* Blockquote accroche */
  blockquote {{
    background: #eff6ff;
    border-left: 4px solid #3b82f6;
    border-radius: 0 10px 10px 0;
    padding: 18px 24px;
    margin: 32px 0;
    font-family: 'Merriweather', serif;
    font-style: italic;
    font-size: 1.05rem;
    color: #1e40af;
    line-height: 1.7;
  }}

  /* Sections */
  h2 {{
    font-size: 1.35rem;
    font-weight: 700;
    color: #0f172a;
    margin: 44px 0 18px;
    padding-bottom: 10px;
    border-bottom: 2px solid #e2e8f0;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  h3 {{
    font-size: 1.1rem;
    font-weight: 700;
    color: #1e40af;
    margin: 32px 0 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  p {{ margin: 12px 0; color: #334155; }}

  /* Cards */
  .card {{
    background: white;
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
    padding: 24px 28px;
    margin: 20px 0;
    border: 1px solid #f1f5f9;
  }}

  /* Lists */
  ul, ol {{ padding-left: 22px; margin: 12px 0; }}
  li {{
    margin: 8px 0;
    color: #334155;
    padding-left: 4px;
  }}
  li::marker {{ color: #3b82f6; }}

  /* Links */
  a {{ color: #2563eb; text-decoration: none; font-weight: 500; }}
  a:hover {{ text-decoration: underline; color: #1d4ed8; }}

  /* Code */
  code {{
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    padding: 2px 7px;
    border-radius: 5px;
    font-size: 0.88em;
    font-family: 'Fira Code', 'Cascadia Code', monospace;
    color: #0f172a;
  }}
  pre {{
    background: #0f172a;
    color: #e2e8f0;
    padding: 20px 24px;
    border-radius: 10px;
    overflow-x: auto;
    margin: 16px 0;
    font-size: 0.88em;
    line-height: 1.6;
  }}
  pre code {{ background: none; border: none; color: inherit; padding: 0; }}

  /* Tables */
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 20px 0;
    background: white;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07);
    font-size: 0.93rem;
  }}
  th {{
    background: #1e3a5f;
    color: white;
    padding: 12px 16px;
    text-align: left;
    font-weight: 600;
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }}
  td {{
    padding: 11px 16px;
    border-bottom: 1px solid #f1f5f9;
    color: #334155;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:nth-child(even) td {{ background: #f8fafc; }}

  /* Strong */
  strong {{ color: #0f172a; font-weight: 600; }}
  em {{ color: #475569; font-style: italic; }}

  /* HR */
  hr {{
    border: none;
    border-top: 2px dashed #e2e8f0;
    margin: 44px 0;
  }}

  /* Footer */
  .footer {{
    background: #0f172a;
    color: #94a3b8;
    text-align: center;
    padding: 28px 20px;
    font-size: 0.82rem;
    margin-top: 60px;
  }}
  .footer a {{ color: #60a5fa; font-weight: 400; }}

  /* Responsive */
  @media (max-width: 600px) {{
    .hero {{ padding: 36px 16px 32px; }}
    .container {{ padding: 0 12px 40px; }}
    h2 {{ font-size: 1.15rem; }}
  }}
</style>
</head>
<body>

<header class="site-header">
  <span class="logo">⚡ Veille IA</span>
  <span class="tagline">Ta dose quotidienne d'actu tech</span>
</header>

<div class="hero">
  <div class="badge">Article du jour</div>
  <h1>{TITLE}</h1>
  <div class="date-line">
    📅 Article source du <strong style="color:white">{ARTICLE_DATE}</strong>
    &nbsp;·&nbsp; Veille generee le {DATE_GENERATION}
    &nbsp;·&nbsp; <a href="index.html" style="color:rgba(255,255,255,0.75)">Tous les articles</a>
  </div>
</div>

<div class="container">
{CONTENT}
</div>

<footer class="footer">
  Genere automatiquement par <strong style="color:#e2e8f0">Veille IA Bot</strong> le {DATE_GENERATION}<br>
  <a href="index.html">← Retour a l'index</a>
</footer>

</body>
</html>"""


def markdown_to_html(md: str) -> str:
    html = md
    # Headers
    html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)
    # Bold/italic
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
    # Blockquote
    html = re.sub(r'^> (.+)$', r'<blockquote>\1</blockquote>', html, flags=re.MULTILINE)
    # Code blocks
    html = re.sub(r'```[\w]*\n(.*?)\n```', r'<pre><code>\1</code></pre>', html, flags=re.DOTALL)
    html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)
    # Links
    html = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', html)
    # HR
    html = re.sub(r'^---$', '<hr>', html, flags=re.MULTILINE)
    # Tables
    lines = html.split('\n')
    result_lines = []
    in_table = False
    for line in lines:
        if '|' in line and line.strip().startswith('|'):
            if not in_table:
                result_lines.append('<table>')
                in_table = True
            if re.match(r'^\|[-| :]+\|$', line.strip()):
                continue
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if result_lines and '<table>' in result_lines[-1]:
                tag = 'th'
            else:
                tag = 'td'
            row = '<tr>' + ''.join(f'<{tag}>{c}</{tag}>' for c in cells) + '</tr>'
            result_lines.append(row)
        else:
            if in_table:
                result_lines.append('</table>')
                in_table = False
            result_lines.append(line)
    if in_table:
        result_lines.append('</table>')
    html = '\n'.join(result_lines)
    # Lists (handle both - and * bullet styles)
    html = re.sub(r'^\*{1,3}\s+(.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
    html = re.sub(r'^-\s+(.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
    html = re.sub(r'(<li>.*</li>\n?)+', lambda m: '<ul>' + m.group() + '</ul>', html)
    # Paragraphs
    paragraphs = html.split('\n\n')
    processed = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if p.startswith('<'):
            processed.append(p)
        else:
            processed.append(f'<p>{p}</p>')
    return '\n'.join(processed)


def save_html_article(article_info: dict, test_mode: bool = False) -> Path:
    article = article_info["article"]
    content_md = article_info["contenu_markdown"]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    titre_slug = slugify(article.get("titre", "article"))
    filename = f"{date_str}_{titre_slug}.html"
    filepath = ARTICLES_GENERES_DIR / filename

    content_html = markdown_to_html(content_md)
    full_html = HTML_TEMPLATE.format(
        TITLE=article.get("titre", "Article du jour"),
        CONTENT=content_html,
        ARTICLE_DATE=article.get("date", "date inconnue"),
        DATE_GENERATION=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full_html)

    log.info(f"Article HTML sauvegarde : {filepath}")
    safe_print(f"Article HTML sauvegarde : {filepath}")
    return filepath


def update_index_html(article_info: dict, html_filename: str):
    article = article_info["article"]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing_entries = []
    if INDEX_HTML_FILE.exists():
        with open(INDEX_HTML_FILE, encoding="utf-8") as f:
            content = f.read()
        matches = re.findall(r'<tr data-date="([^"]+)">(.*?)</tr>', content, re.DOTALL)
        for m in matches:
            existing_entries.append((m[0], m[1]))

    new_entry = (
        date_str,
        f"""
      <td>{date_str}</td>
      <td><a href="{html_filename}">{article.get('titre', '')}</a></td>
      <td>{article.get('score', 0)}/10</td>
      <td>{article.get('pertinence_bi_consultant', 0)}/10</td>
      <td>{article.get('pertinence_passionné_ia', 0)}/10</td>
      <td>{article.get('pertinence_entrepreneur', 0)}/10</td>
      <td>{article.get('categorie', '')}</td>
    """
    )
    all_entries = [new_entry] + existing_entries
    all_entries.sort(key=lambda x: x[0], reverse=True)

    rows = "\n".join(f'<tr data-date="{e[0]}">{e[1]}</tr>' for e in all_entries)

    index_html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Veille IA - Index des articles</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; color: #1a1a1a; }}
  h1 {{ color: #0f172a; border-bottom: 3px solid #3b82f6; padding-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
  th {{ background: #1e40af; color: white; padding: 12px; text-align: left; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; }}
  tr:hover {{ background: #f0f9ff; }}
  a {{ color: #3b82f6; text-decoration: none; font-weight: 500; }}
  a:hover {{ text-decoration: underline; }}
  .score-high {{ color: #059669; font-weight: bold; }}
  .score-mid {{ color: #d97706; }}
  .updated {{ color: #64748b; font-size: 0.85em; margin-top: 8px; }}
</style>
</head>
<body>
<h1>Veille IA - Index des articles generes</h1>
<p class="updated">Mis a jour le {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</p>
<table>
  <thead>
    <tr>
      <th>Date</th>
      <th>Titre</th>
      <th>Score</th>
      <th>BI</th>
      <th>IA</th>
      <th>Entrepreneur</th>
      <th>Categorie</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>
</body>
</html>"""

    with open(INDEX_HTML_FILE, "w", encoding="utf-8") as f:
        f.write(index_html)
    log.info(f"Index HTML mis a jour : {INDEX_HTML_FILE}")


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(article_info: dict, html_filepath: Path, test_mode: bool = False):
    gmail_addr = os.getenv("GMAIL_ADDRESS")
    gmail_pwd = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("EMAIL_DESTINATAIRE")

    article = article_info["article"]
    content_md = article_info["contenu_markdown"]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    titre_court = (article.get("titre", "") or "").strip().replace("\n", " ")[:60]
    score = article.get("score", 0)

    prefix = "[TEST] " if test_mode else ""
    subject = f"{prefix}Veille IA - {date_str} | {titre_court} [{score}/10]"
    preheader = article.get("raison_score", article.get("resume", "")[:150])

    content_html = markdown_to_html(content_md)
    body_html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: #f8fafc; }}
  .container {{ max-width: 680px; margin: 20px auto; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
  .header {{ background: #1e40af; color: white; padding: 28px 32px; }}
  .content {{ padding: 28px 32px; }}
  h2 {{ color: #1e40af; border-bottom: 2px solid #dbeafe; padding-bottom: 8px; }}
  h3 {{ color: #1e40af; }}
  blockquote {{ border-left: 4px solid #3b82f6; margin: 0 0 20px 0; padding: 12px 20px; background: #eff6ff; border-radius: 0 8px 8px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  th, td {{ padding: 9px 12px; border: 1px solid #e2e8f0; text-align: left; font-size: 0.95em; }}
  th {{ background: #f1f5f9; }}
  a {{ color: #3b82f6; }}
  code {{ background: #f1f5f9; padding: 2px 5px; border-radius: 3px; font-size: 0.88em; }}
  hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 28px 0; }}
  .footer {{ background: #f8fafc; padding: 16px 32px; color: #64748b; font-size: 0.85em; border-top: 1px solid #e2e8f0; }}
  ul {{ padding-left: 20px; }}
  li {{ margin: 6px 0; }}
</style>
</head>
<body>
<span style="display:none;font-size:1px;color:#fff;max-height:0;overflow:hidden;">{preheader}</span>
<div class="container">
  <div class="header">
    <h1 style="margin:0 0 8px 0;font-size:1em;font-weight:400;color:rgba(255,255,255,0.85);">Veille IA &mdash; {date_str}</h1>
    <h2 style="margin:0;font-size:1.4em;line-height:1.4;color:#ffffff;font-weight:700;">{article.get('titre', '')}</h2>
  </div>
  <div class="content">
    {content_html}
  </div>
  <div class="footer">
    Article genere automatiquement par Veille IA Bot | Score: {score}/10
  </div>
</div>
</body>
</html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = gmail_addr
        msg["To"] = recipient
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_addr, gmail_pwd)
            server.sendmail(gmail_addr, recipient, msg.as_string())

        log.info(f"Email envoye a {recipient} : {subject}")
        safe_print(f"Email envoye avec succes a {recipient}")
        return True
    except Exception as e:
        log.error(f"Echec envoi email : {e}")
        safe_print(f"ERREUR envoi email : {e}")
        try:
            EMAIL_FAILED_DIR.mkdir(parents=True, exist_ok=True)
            failed_path = EMAIL_FAILED_DIR / f"{date_str}.html"
            with open(failed_path, "w", encoding="utf-8") as f:
                f.write(body_html)
            log.info(f"Email sauvegarde dans {failed_path}")
        except Exception as e2:
            log.error(f"Impossible de sauvegarder l'email echoue : {e2}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Veille IA - Initialisation complete")
    parser.add_argument("--test", action="store_true", help="Mode test (3 sources, 10 articles)")
    args = parser.parse_args()
    test_mode = args.test

    for d in [DATA_DIR, ARTICLES_GENERES_DIR, EMAIL_FAILED_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    safe_print("\n" + "="*60)
    safe_print("VEILLE IA - " + ("MODE TEST" if test_mode else "INITIALISATION COMPLETE"))
    safe_print("="*60)

    stats = load_stats()
    stats = reset_daily_stats_if_needed(stats)

    sources = TEST_SOURCES if test_mode else RSS_SOURCES
    start_date = HISTORY_START_DATE
    max_scoring = 10 if test_mode else MAX_INITIAL_SCORING

    safe_print(f"\nPhase 1 : Collecte RSS ({len(sources)} sources depuis {start_date})")
    history = collect_history(sources, start_date, test_mode)
    safe_print(f"Total articles dans historique : {len(history)}")

    safe_print("\nPhase 2 : Initialisation Gemini")
    model = init_gemini()

    safe_print(f"\nPhase 3 : Scoring (max {max_scoring} articles)")
    history_file_for_save = None if test_mode else ARTICLES_HISTORY_FILE
    try:
        history = score_articles(model, history, stats, max_articles=max_scoring, history_file=history_file_for_save)
    except Exception as e:
        log.error(f"Erreur scoring : {e}")
        safe_print(f"Scoring interrompu : {e}")
    stats["total_articles_traites"] = len([a for a in history if not a.get("score_pending", True)])
    save_stats(stats)

    if not test_mode:
        save_json(ARTICLES_HISTORY_FILE, history)

    scored = [a for a in history if not a.get("score_pending", True)]
    safe_print(f"\nArticles scores : {len(scored)}")

    if not scored:
        safe_print("Aucun article score disponible, arret.")
        log.warning("Aucun article score, arret du script")
        return

    ranking = sorted(scored, key=lambda x: x.get("score_composite", 0), reverse=True)
    if not test_mode:
        save_json(RANKING_GLOBAL_FILE, ranking)

    safe_print("\nPhase 4 : Generation de l'article du jour")
    try:
        article_info = generate_article_du_jour(model, ranking, stats, test_mode)
    except Exception as e:
        log.error(f"Erreur generation article du jour : {e}")
        safe_print(f"Generation echouee (exception) : {e}")
        article_info = None
    if not article_info:
        safe_print("Generation echouee, arret.")
        if not test_mode and scored:
            safe_print(f"{len(scored)} articles scores sauvegardes dans ranking_global.json")
        return

    safe_print("\nPhase 5 : Sauvegarde HTML et envoi email")
    html_path = save_html_article(article_info, test_mode)
    update_index_html(article_info, html_path.name)

    email_sent = send_email(article_info, html_path, test_mode)

    if not test_mode:
        for a in history:
            if a["url"] == article_info["article"]["url"]:
                a["article_du_jour"] = True
        save_json(ARTICLES_HISTORY_FILE, history)

        articles_du_jour = load_json(ARTICLES_DU_JOUR_FILE, [])
        articles_du_jour.append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "url": article_info["article"]["url"],
            "titre": article_info["article"].get("titre", ""),
            "score": article_info["article"].get("score", 0),
            "score_composite": article_info["article"].get("score_composite", 0),
            "html_file": html_path.name,
            "email_sent": email_sent,
        })
        save_json(ARTICLES_DU_JOUR_FILE, articles_du_jour)

        stats["streak_jours"] = stats.get("streak_jours", 0) + 1
        stats["dernier_envoi"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        save_stats(stats)

    safe_print("\n" + "="*60)
    safe_print("EXECUTION TERMINEE")
    safe_print("="*60)
    safe_print(f"  Articles collectes    : {len(history)}")
    safe_print(f"  Articles scores       : {len(scored)}")
    safe_print(f"  Article du jour       : {article_info['article'].get('titre', '')[:60]}")
    safe_print(f"  Score                 : {article_info['article'].get('score', 0)}/10")
    safe_print(f"  HTML sauvegarde       : {html_path}")
    safe_print(f"  Email envoye          : {'OUI' if email_sent else 'NON'}")
    safe_print(f"  Appels Gemini total   : {stats.get('total_appels_gemini', 0)}")


if __name__ == "__main__":
    main()
