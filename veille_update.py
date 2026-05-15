#!/usr/bin/env python3
"""
veille_update.py — Mise à jour quotidienne de la veille IA.
Collecte les 36 dernières heures, score, génère l'article du jour, envoie l'email.
"""

import sys
import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from config import (
    BASE_DIR, DATA_DIR, ARTICLES_GENERES_DIR, EMAIL_FAILED_DIR, LOGS_DIR,
    ARTICLES_HISTORY_FILE, RANKING_GLOBAL_FILE, ARTICLES_DU_JOUR_FILE,
    STATS_FILE, INDEX_HTML_FILE,
    GEMINI_MODELS, MAX_GEMINI_CALLS_PER_DAY, BATCH_SIZE,
    KEYWORDS, RSS_SOURCES
)

from veille_ia import (
    safe_print, log, fetch_feed, url_hash, load_json, save_json,
    load_stats, save_stats, reset_daily_stats_if_needed,
    article_matches_keywords, score_composite,
    init_gemini, call_gemini_with_retry, score_batch, extract_json_from_response,
    generate_article_du_jour, save_html_article, update_index_html, send_email,
)

try:
    JOURS_RETENTION = int(os.getenv("JOURS_RETENTION", "120"))
except Exception:
    JOURS_RETENTION = 120

MAX_NEW_SCORING_PER_DAY = 20
MAX_BACKLOG_SCORING_PER_DAY = 20


def collect_recent_articles(sources: list, hours: int = 36) -> list:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d")
    history = load_json(ARTICLES_HISTORY_FILE, [])
    known_hashes = {a["url_hash"] for a in history}

    all_new = []
    for i, source in enumerate(sources, 1):
        safe_print(f"[{i}/{len(sources)}] Mise a jour : {source['name']}...")
        articles = fetch_feed(source, cutoff_str)
        new_arts = [a for a in articles if a["url_hash"] not in known_hashes]
        for a in new_arts:
            known_hashes.add(a["url_hash"])
        all_new.extend(new_arts)

    history.extend(all_new)
    save_json(ARTICLES_HISTORY_FILE, history)
    log.info(f"Collecte quotidienne : {len(all_new)} nouveaux articles")
    return history, all_new


def purge_old_ranking(ranking: list, jours: int = JOURS_RETENTION) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=jours)).strftime("%Y-%m-%d")
    before = len(ranking)
    ranking = [a for a in ranking if a.get("date", "9999") >= cutoff]
    removed = before - len(ranking)
    if removed > 0:
        log.info(f"Purge ranking : {removed} articles > {jours} jours supprimes")
    return ranking


def main():
    for d in [DATA_DIR, ARTICLES_GENERES_DIR, EMAIL_FAILED_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    safe_print("\n" + "="*60)
    safe_print("VEILLE IA - MISE A JOUR QUOTIDIENNE")
    safe_print("="*60)

    stats = load_stats()
    stats = reset_daily_stats_if_needed(stats)

    if stats.get("appels_gemini_aujourd_hui", 0) >= MAX_GEMINI_CALLS_PER_DAY:
        log.warning("Limite quotidienne Gemini atteinte, arret")
        safe_print("Limite quotidienne Gemini atteinte.")
        return

    safe_print("\nPhase 1 : Collecte RSS (36 dernieres heures)")
    history, new_articles = collect_recent_articles(RSS_SOURCES, hours=36)
    safe_print(f"Nouveaux articles : {len(new_articles)}")

    safe_print("\nPhase 2 : Initialisation Gemini")
    model = init_gemini()

    # Score new articles (max 20)
    new_to_score = [a for a in new_articles if article_matches_keywords(a)][:MAX_NEW_SCORING_PER_DAY]
    safe_print(f"\nPhase 3a : Scoring nouveaux articles ({len(new_to_score)})")
    new_scored_map = {}
    if new_to_score:
        batches = [new_to_score[i:i+BATCH_SIZE] for i in range(0, len(new_to_score), BATCH_SIZE)]
        for batch in batches:
            if stats.get("appels_gemini_aujourd_hui", 0) >= MAX_GEMINI_CALLS_PER_DAY:
                break
            results = score_batch(model, batch, stats)
            for r in results:
                if "url" in r:
                    r["score_composite"] = score_composite(r)
                    new_scored_map[r["url"]] = r
            time.sleep(12)

    # Score backlog (max 20)
    pending = [a for a in history if a.get("score_pending", True) and article_matches_keywords(a)]
    pending.sort(key=lambda x: x.get("date", ""), reverse=True)
    pending = pending[:MAX_BACKLOG_SCORING_PER_DAY]
    safe_print(f"Phase 3b : Scoring backlog ({len(pending)} articles en attente)")
    if pending:
        batches = [pending[i:i+BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
        for batch in batches:
            if stats.get("appels_gemini_aujourd_hui", 0) >= MAX_GEMINI_CALLS_PER_DAY:
                break
            results = score_batch(model, batch, stats)
            for r in results:
                if "url" in r:
                    r["score_composite"] = score_composite(r)
                    new_scored_map[r["url"]] = r
            time.sleep(12)

    for a in history:
        if a["url"] in new_scored_map:
            a.update(new_scored_map[a["url"]])
            a["score_pending"] = False

    save_json(ARTICLES_HISTORY_FILE, history)
    stats["total_articles_traites"] = len([a for a in history if not a.get("score_pending", True)])
    save_stats(stats)

    # Update ranking
    scored = [a for a in history if not a.get("score_pending", True)]
    ranking = sorted(scored, key=lambda x: x.get("score_composite", 0), reverse=True)
    ranking = purge_old_ranking(ranking, JOURS_RETENTION)
    save_json(RANKING_GLOBAL_FILE, ranking)
    safe_print(f"Ranking mis a jour : {len(ranking)} articles")

    # Generate article du jour
    safe_print("\nPhase 4 : Generation de l'article du jour")
    article_info = generate_article_du_jour(model, ranking, stats)
    if not article_info:
        safe_print("Aucun article disponible pour generation.")
        return

    safe_print("\nPhase 5 : Sauvegarde HTML et envoi email")
    html_path = save_html_article(article_info)
    update_index_html(article_info, html_path.name)
    email_sent = send_email(article_info, html_path)

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
    safe_print("MISE A JOUR TERMINEE")
    safe_print(f"  Nouveaux articles     : {len(new_articles)}")
    safe_print(f"  Articles scoring      : {len(new_scored_map)}")
    safe_print(f"  Article du jour       : {article_info['article'].get('titre', '')[:60]}")
    safe_print(f"  Email envoye          : {'OUI' if email_sent else 'NON'}")
    safe_print(f"  Appels Gemini/jour    : {stats.get('appels_gemini_aujourd_hui', 0)}")
    safe_print("="*60)


if __name__ == "__main__":
    main()
