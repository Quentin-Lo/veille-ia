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

import feedparser
import requests
from dotenv import load_dotenv
import google.generativeai as genai

# Force unbuffered stdout for real-time output in subprocess/CI
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

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
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def article_matches_keywords(article: dict) -> bool:
    text = (article.get("titre", "") + " " + article.get("resume", "")).lower()
    return any(kw.lower() in text for kw in KEYWORDS)


def score_composite(a: dict) -> float:
    return (
        a.get("score", 0) * 0.4
        + a.get("pertinence_bi_consultant", 0) * 0.25
        + a.get("pertinence_passionné_ia", 0) * 0.2
        + a.get("pertinence_entrepreneur", 0) * 0.15
    )


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
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                if pub is None or pub < cutoff:
                    continue
                link = entry.get("link", "")
                if not link:
                    continue
                resume = ""
                if hasattr(entry, "summary"):
                    resume = re.sub(r"<[^>]+>", "", entry.summary)[:500]
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
    for i, source in enumerate(sources, 1):
        try:
            safe_print(f"[{i}/{total}] Collecte : {source['name']}...")
        except Exception:
            pass
        articles = fetch_feed(source, start_date)
        new_arts = [a for a in articles if a["url_hash"] not in known_hashes]
        for a in new_arts:
            known_hashes.add(a["url_hash"])
        all_new.extend(new_arts)
        try:
            safe_print(f"  -> {len(new_arts)} nouveaux articles")
        except Exception:
            pass
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
        {"url": a["url"], "titre": a["titre"], "resume": a.get("resume", ""), "date": a.get("date", "")}
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

    scored_map = {}
    batches = [to_score[i:i+BATCH_SIZE] for i in range(0, len(to_score), BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        if stats.get("appels_gemini_aujourd_hui", 0) >= MAX_GEMINI_CALLS_PER_DAY:
            log.warning("Limite quotidienne Gemini atteinte, arret du scoring")
            break
        safe_print(f"  Batch {idx}/{len(batches)} ({len(batch)} articles)...")
        results = score_batch(model, batch, stats)
        for r in results:
            if "url" in r:
                r["score_composite"] = score_composite(r)
                scored_map[r["url"]] = r
        # Apply scored results incrementally to articles list
        for a in articles:
            if a["url"] in scored_map and a.get("score_pending", True):
                a.update(scored_map[a["url"]])
                a["score_pending"] = False
        # Save intermediate progress every 5 batches to survive crashes
        if history_file and idx % 5 == 0:
            save_json(history_file, articles)
            log.info(f"Sauvegarde intermediaire : {len(scored_map)} articles scores")
        # 8s sleep keeps us under 7 RPM, within Gemini free-tier 10 RPM limit
        time.sleep(8)

    save_stats(stats)
    return articles


# ── Génération article du jour ─────────────────────────────────────────────────
GENERATION_PROMPT = """Tu es un journaliste expert en IA, Data Engineering, Business Intelligence et entrepreneuriat tech, qui ecrit pour un consultant BI senior passionne d'IA, d'automatisation et de creation de valeur business.
Ton lecteur est expert technique. Il veut de la profondeur et de l'actionnable.

Redirige en FRANCAIS. Utilise du Markdown bien structure.

Article principal : {TITRE} ({URL})
Contexte : {RESUME}
Date : {DATE}
Score : {SCORE}/10 | Categorie : {CATEGORIE}
Pertinence BI : {SCORE_BI}/10 | IA : {SCORE_IA}/10 | Entrepreneur : {SCORE_E}/10

Articles secondaires pour la section "Autres sujets notables" :
{SECONDARY_JSON}

Articles de la semaine pour le Radar tendances (score >= 7) :
{WEEKLY_JSON}

Structure OBLIGATOIRE (utilise exactement ces sections) :

## {TITRE_ACCROCHEUR}

> **{PHRASE_ACCROCHE_EN_UNE_LIGNE}**

| Metrique | Valeur |
|----------|--------|
| Score d'impact | {SCORE}/10 |
| Categorie | {CATEGORIE} |
| Pertinence Consultant BI | {SCORE_BI}/10 |
| Pertinence Passionne IA | {SCORE_IA}/10 |
| Pertinence Entrepreneur | {SCORE_E}/10 |
| Date | {DATE} |
| Source | [{SOURCE}]({URL}) |

### Pourquoi c'est important maintenant
[2-3 paragraphes denses]

### Ce qui s'est passe exactement
[Faits precis, chiffres, benchmarks, architecture si disponibles]

### Impact pour le Consultant BI
**Ce que ca change sur tes missions clients :**
- [Point concret 1]
- [Point concret 2]

**Outils/pratiques a tester immediatement :**
- [Outil specifique avec lien]

**Question a poser a ton prochain client :**
- [Question strategique]

### Impact pour le Passionne IA/Automatisation
**Ce que tu peux experimenter des maintenant :**
- [Experience concrete faisable avec outils gratuits]

**Connexions avec ton stack :**
- [Lien avec n8n / Python / Gemini / Claude Code]

**Ressource a bookmarker :**
- [Lien repo GitHub, paper, tutorial]

### Impact pour l'Entrepreneur
**Opportunite business identifiee :**
- [Idee concrete de produit, service ou automatisation monetisable]

**Outils gratuits ou low-cost pour tester l'idee :**
- [Stack minimal viable]

**Risque ou menace a anticiper :**
- [Ce que ca menace ou rend obsolete]

### Signaux faibles a surveiller
[2-3 tendances emergentes, prochaines etapes attendues dans 30-90 jours]

---

## Autres sujets notables aujourd'hui

[Pour chacun des articles secondaires fournis, une mini-analyse de 2-3 phrases avec le score et les pertinences]

---

## Radar tendances - 7 derniers jours

**Tendance #1 : {NOM}**
[Ce qui monte en frequence et en score - 3 phrases]

**Tendance #2 : {NOM}**
[Tendance de fond qui s'installe - 3 phrases]

**Signal faible : {NOM}**
[Sujet peu couvert mais qui pourrait exploser - 2 phrases]

---

## Ressources de la semaine
[3 ressources concretes extraites des articles scores >= 7 cette semaine]
- [{TITRE}]({URL}) - {1 phrase de valeur}
"""


def generate_article_du_jour(model, ranking: list, stats: dict, test_mode: bool = False):
    scored = [a for a in ranking if not a.get("score_pending", True) and not a.get("article_du_jour", False)]
    if not scored:
        log.warning("Aucun article disponible pour generation")
        return None

    scored.sort(key=lambda x: x.get("score_composite", 0), reverse=True)
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
    prompt = prompt.replace("{SECONDARY_JSON}", json.dumps(secondary, ensure_ascii=False))
    prompt = prompt.replace("{WEEKLY_JSON}", json.dumps(weekly, ensure_ascii=False))

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
<title>{TITLE}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; color: #1a1a1a; line-height: 1.7; }}
  h1, h2, h3 {{ color: #0f172a; }}
  h2 {{ border-bottom: 2px solid #3b82f6; padding-bottom: 8px; margin-top: 40px; }}
  h3 {{ color: #1e40af; margin-top: 28px; }}
  blockquote {{ border-left: 4px solid #3b82f6; margin: 0; padding: 12px 20px; background: #eff6ff; border-radius: 0 8px 8px 0; font-style: italic; }}
  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
  th, td {{ padding: 10px 14px; text-align: left; border: 1px solid #e2e8f0; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  tr:nth-child(even) {{ background: #f8fafc; }}
  a {{ color: #3b82f6; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
  pre {{ background: #1e293b; color: #e2e8f0; padding: 20px; border-radius: 8px; overflow-x: auto; }}
  pre code {{ background: none; color: inherit; }}
  .meta {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin: 20px 0; }}
  .date {{ color: #64748b; font-size: 0.9em; }}
  hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 40px 0; }}
  ul, ol {{ padding-left: 24px; }}
  li {{ margin: 6px 0; }}
  strong {{ color: #0f172a; }}
</style>
</head>
<body>
{CONTENT}
<hr>
<p class="date">Article genere le {DATE_GENERATION} par Veille IA Bot | <a href="index.html">Retour a l'index</a></p>
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
    # Lists
    html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
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
    titre_court = article.get("titre", "")[:60]
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
  .header {{ background: linear-gradient(135deg, #1e40af 0%, #3b82f6 100%); color: white; padding: 28px 32px; }}
  .header h1 {{ margin: 0 0 8px 0; font-size: 1.1em; font-weight: 400; opacity: 0.9; }}
  .header h2 {{ margin: 0; font-size: 1.4em; line-height: 1.4; }}
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
    <h1>Veille IA - {date_str}</h1>
    <h2>{article.get('titre', '')}</h2>
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

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
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
