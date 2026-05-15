from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
ARTICLES_GENERES_DIR = DATA_DIR / "articles_generes"
EMAIL_FAILED_DIR = DATA_DIR / "email_failed"
LOGS_DIR = BASE_DIR / "logs"

ARTICLES_HISTORY_FILE = DATA_DIR / "articles_history.json"
RANKING_GLOBAL_FILE = DATA_DIR / "ranking_global.json"
ARTICLES_DU_JOUR_FILE = DATA_DIR / "articles_du_jour.json"
STATS_FILE = DATA_DIR / "stats.json"
INDEX_HTML_FILE = ARTICLES_GENERES_DIR / "index.html"

GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
]

MAX_GEMINI_CALLS_PER_DAY = 200
BATCH_SIZE = 10
MAX_INITIAL_SCORING = 600
HISTORY_START_DATE = "2026-01-01"
RSS_TIMEOUT = 10
RSS_MAX_RETRIES = 3

KEYWORDS = [
    # IA & Modèles
    "LLM", "GPT", "Claude", "Gemini", "Llama", "Mistral", "Qwen", "DeepSeek",
    "multimodal", "fine-tuning", "fine tuning", "finetuning", "embedding",
    "RAG", "vector", "transformer", "diffusion", "RLHF", "SFT", "tokenizer",
    "benchmark", "frontier model", "open source model", "open-source model",
    # Agents & Dev assisté
    "agent", "multi-agent", "multi agent", "agentic", "MCP", "Cursor",
    "Copilot", "Devin", "computer use", "AI coding", "LLMOps", "orchestration",
    "AutoGPT", "CrewAI", "LangChain", "LangGraph",
    # BI & Data
    "Power BI", "DAX", "Fabric", "Tableau", "Looker", "Metabase", "Evidence",
    "dbt", "Databricks", "Snowflake", "BigQuery", "DuckDB", "lakehouse",
    "data mesh", "ETL", "ELT", "data warehouse", "data lake", "Spark",
    "data engineering", "data platform", "semantic layer", "data governance",
    # Automatisation & No-code
    "n8n", "Make.com", "Zapier", "automation", "workflow", "RPA",
    "Bubble", "Retool", "Glide", "AppSheet", "no-code", "nocode",
    "low-code", "lowcode", "citizen developer", "API-first",
    # Cloud
    "Azure", "AWS", "GCP", "serverless", "cloud native", "kubernetes",
    "microservices", "infrastructure as code",
    # Business & Entrepreneurship
    "startup", "SaaS", "indie hacker", "ARR", "MRR", "fundraising",
    "Series A", "Series B", "Y Combinator", "Product Hunt", "levée de fonds",
    "bootstrapped", "solopreneur", "B2B SaaS",
    # Sécurité & Réglementation
    "EU AI Act", "GDPR", "alignment", "red team", "red teaming",
    "AI safety", "hallucination", "bias", "privacy",
]

RSS_SOURCES = [
    # IA & Recherche fondamentale
    {"url": "https://openai.com/blog/rss.xml", "name": "OpenAI Blog", "categorie": "IA_MODELES"},
    {"url": "https://deepmind.google/blog/rss.xml", "name": "DeepMind Blog", "categorie": "IA_MODELES"},
    {"url": "https://huggingface.co/blog/feed.xml", "name": "HuggingFace Blog", "categorie": "IA_MODELES"},
    {"url": "https://www.anthropic.com/rss.xml", "name": "Anthropic News", "categorie": "IA_MODELES"},
    {"url": "https://ai.meta.com/blog/feed/rss/", "name": "Meta AI Blog", "categorie": "IA_MODELES"},
    {"url": "https://blogs.microsoft.com/ai/feed/", "name": "Microsoft AI Blog", "categorie": "IA_MODELES"},
    {"url": "https://research.google/blog/rss/", "name": "Google Research Blog", "categorie": "IA_MODELES"},
    {"url": "https://www.marktechpost.com/feed/", "name": "MarkTechPost", "categorie": "IA_MODELES"},
    {"url": "https://paperswithcode.com/trending.rss", "name": "Papers With Code", "categorie": "IA_MODELES"},
    {"url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "name": "MIT Tech Review AI", "categorie": "IA_MODELES"},
    # IA Appliquée & Agents
    {"url": "https://feeds.feedburner.com/venturebeat/SZYF", "name": "VentureBeat AI", "categorie": "IA_AGENTS"},
    {"url": "https://bdtechtalks.com/feed/", "name": "BD Tech Talks", "categorie": "IA_APPLIQUEE"},
    {"url": "https://www.latent.space/feed", "name": "Latent Space", "categorie": "IA_AGENTS"},
    {"url": "https://therundown.ai/feed", "name": "The Rundown AI", "categorie": "IA_APPLIQUEE"},
    {"url": "https://aisnakeoil.substack.com/feed", "name": "AI Snake Oil", "categorie": "SECURITE_IA"},
    {"url": "https://magazine.sebastianraschka.com/feed", "name": "Sebastian Raschka", "categorie": "IA_AGENTS"},
    {"url": "https://eugeneyan.com/rss/", "name": "Eugene Yan", "categorie": "IA_APPLIQUEE"},
    # Data Platform
    {"url": "https://www.databricks.com/feed", "name": "Databricks Blog", "categorie": "DATA_PLATFORM"},
    {"url": "https://docs.getdbt.com/blog/rss.xml", "name": "dbt Blog", "categorie": "DATA_PLATFORM"},
    {"url": "https://www.datacamp.com/blog/rss/", "name": "DataCamp Blog", "categorie": "DATA_PLATFORM"},
    {"url": "https://towardsdatascience.com/feed", "name": "Towards Data Science", "categorie": "DATA_PLATFORM"},
    {"url": "https://motherduck.com/rss.xml", "name": "MotherDuck Blog", "categorie": "DATA_PLATFORM"},
    # BI & Visualisation
    {"url": "https://powerbi.microsoft.com/en-us/blog/feed/", "name": "Power BI Blog", "categorie": "BI_OUTILS"},
    {"url": "https://www.sqlbi.com/feed/", "name": "SQLBI", "categorie": "BI_OUTILS"},
    {"url": "https://data-mozart.com/feed/", "name": "Data Mozart", "categorie": "BI_OUTILS"},
    {"url": "https://www.storytellingwithdata.com/blog?format=rss", "name": "Storytelling With Data", "categorie": "BI_PRATIQUES"},
    {"url": "https://medium.com/feed/tag/power-bi", "name": "Medium Power BI", "categorie": "BI_OUTILS"},
    # Automatisation & No-code
    {"url": "https://blog.n8n.io/rss/", "name": "n8n Blog", "categorie": "AUTOMATISATION"},
    {"url": "https://www.make.com/en/blog/feed", "name": "Make Blog", "categorie": "AUTOMATISATION"},
    {"url": "https://zapier.com/blog/feeds/latest/", "name": "Zapier Blog", "categorie": "AUTOMATISATION"},
    # Dev assisté par IA
    {"url": "https://github.blog/feed/", "name": "GitHub Blog", "categorie": "DEV_ASSISTE"},
    {"url": "https://code.visualstudio.com/feed.xml", "name": "VS Code Blog", "categorie": "DEV_ASSISTE"},
    # Entrepreneuriat & Startups
    {"url": "https://www.producthunt.com/feed", "name": "Product Hunt", "categorie": "ENTREPRENEURIAT"},
    {"url": "https://feeds.feedburner.com/ycombinator", "name": "Y Combinator", "categorie": "ENTREPRENEURIAT"},
    {"url": "https://www.indiehackers.com/feed.xml", "name": "Indie Hackers", "categorie": "ENTREPRENEURIAT"},
    {"url": "https://newsletter.pragmaticengineer.com/feed", "name": "Pragmatic Engineer", "categorie": "ENTREPRENEURIAT"},
    {"url": "https://www.saastr.com/feed/", "name": "SaaStr", "categorie": "ENTREPRENEURIAT"},
    {"url": "https://entrepreneurshandbook.co/feed", "name": "Entrepreneurs Handbook", "categorie": "ENTREPRENEURIAT"},
    # Business & Stratégie IA
    {"url": "https://techcrunch.com/feed/", "name": "TechCrunch", "categorie": "BUSINESS_IA"},
    {"url": "https://www.wired.com/feed/rss", "name": "Wired", "categorie": "BUSINESS_IA"},
    {"url": "https://a16z.com/feed/", "name": "a16z Blog", "categorie": "BUSINESS_IA"},
]

TEST_SOURCES = [
    {"url": "https://openai.com/blog/rss.xml", "name": "OpenAI Blog", "categorie": "IA_MODELES"},
    {"url": "https://huggingface.co/blog/feed.xml", "name": "HuggingFace Blog", "categorie": "IA_MODELES"},
    {"url": "https://techcrunch.com/feed/", "name": "TechCrunch", "categorie": "BUSINESS_IA"},
]

SCORING_PROMPT = """Tu es un expert en IA, Data Engineering, Business Intelligence, automatisation et entrepreneuriat tech.

Analyse ces articles et attribue à chacun un score d'impact de 1 à 10 :

SCORE 9-10 : Rupture majeure
- Nouveau modèle frontier redéfinissant les capacités
- Percée scientifique publiée et vérifiée
- Changement de paradigme architectural
- Annonce réglementaire majeure (EU AI Act, RGPD IA)
- Levée de fonds ou acquisition qui redessine un secteur entier

SCORE 7-8 : Avancée significative
- Nouveau modèle open-source compétitif
- Mise à jour majeure outil BI/Data (Power BI Fabric, dbt Core...)
- Nouvelle capacité agent documentée et reproductible
- Étude quantifiée sur l'impact IA sur les métiers data/BI
- Lancement outil automatisation disruptif gratuit
- Nouveau framework ou pratique entrepreneuriale IA documentée

SCORE 5-6 : Évolution notable
- Mise à jour importante outil existant
- Nouveau cas d'usage BI/Data/Startup documenté avec métriques
- Tutoriel avancé sur RAG, agents, DAX, dbt, automatisation
- Intégration IA dans outil data ou business mainstream

SCORE 3-4 : Information utile
- Article de fond sur pratique établie
- Comparatif d'outils avec données chiffrées
- Retour d'expérience entreprise sur déploiement IA/BI

SCORE 1-2 : Faible valeur ajoutée
- Contenu marketing sans substance technique
- Redite d'information déjà couverte

Catégories possibles :
IA_MODELES, IA_AGENTS, IA_APPLIQUEE, DATA_PLATFORM, BI_OUTILS, BI_PRATIQUES,
AUTOMATISATION, DEV_ASSISTE, NOCODE_LOWCODE, CLOUD_DATA, SECURITE_IA, BUSINESS_IA, ENTREPRENEURIAT

Retourne UNIQUEMENT ce JSON valide, sans markdown, sans commentaire :
[
  {
    "url_hash": "...",
    "url": "...",
    "titre": "...",
    "date": "YYYY-MM-DD",
    "score": 7,
    "categorie": "BI_OUTILS",
    "raison_score": "2 phrases max",
    "tags": ["tag1", "tag2"],
    "pertinence_bi_consultant": 8,
    "pertinence_passionné_ia": 6,
    "pertinence_entrepreneur": 5
  }
]

Articles à analyser :
{ARTICLES_JSON}
"""
