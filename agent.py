#!/usr/bin/env python3
"""
pioneer-agent-001 — agente Haiku que monitorea el ecosistema Giskard
y delega alertas al creador vía Telegram.

Tareas:
- Monitoreo GitHub: nuevos comentarios en issues/PRs/discussions
- Monitoreo Moltbook: replies y DMs
- Health check de servicios locales
- Resumen diario a las 9:00
- Clasificación: spam / relevante / urgente
- Drafts para aprobación antes de publicar

Corre cada 30 minutos como systemd timer.
"""

import os
import json
import hashlib
import httpx
import requests
import anthropic
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY")
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN")
MOLTBOOK_KEY   = os.getenv("MOLTBOOK_API_KEY")

BASE           = "http://localhost"
STATE_FILE     = Path(__file__).parent / "state.json"
LOG_FILE       = Path(__file__).parent / "pioneer.log"

AGENT_ID       = "pioneer-agent-001"
MODEL          = "claude-haiku-4-5-20251001"
OASIS_URL      = "http://localhost:8003"
PIONEER_SIGNING_KEY = os.getenv("PIONEER_SIGNING_KEY", "")

# Submolts de Moltbook a rastrear en busca de insights para el stack
COMMUNITY_SUBMOLTS = ["agents", "builds", "infrastructure", "agentfinance", "philosophy"]

# GitHub: repos/issues/PRs/discussions a monitorear
GITHUB_WATCH = [
    {"type": "pr",    "repo": "modelcontextprotocol/servers",    "number": 3697, "label": "MCP Registry"},
    {"type": "issue", "repo": "modelcontextprotocol/servers",    "number": 503,  "label": "Agent-to-Agent"},
    {"type": "issue", "repo": "modelcontextprotocol/servers",    "number": 2007, "label": "SEP Payment"},
    {"type": "issue", "repo": "modelcontextprotocol/python-sdk", "number": 1561, "label": "Bug -32601"},
    {"type": "pr",    "repo": "modelcontextprotocol/python-sdk", "number": 2344, "label": "SEP-2164 error codes"},
]

# Repos to watch for new releases
STACKER_WATCH = [
    {"id": "1460259", "title": "MCP servers with Lightning paywall"},
    {"id": "1461075", "title": "Physical robots are agents"},
]

RELEASE_WATCH = [
    {"repo": "msaleme/red-team-blue-team-agent-fabric", "label": "msaleme harness", "target_version": "v3.8.0"},
]

SERVICES = [
    {"name": "Search",     "port": 8004},
    {"name": "Memory",     "port": 8005},
    {"name": "Oasis",      "port": 8003},
    {"name": "Origin",     "port": 8007},
    {"name": "Marks",      "port": 8015},
    {"name": "AnimaCore",  "port": 8009},
    {"name": "CraftCore",  "port": 8010},
    {"name": "RaceCore",   "port": 8013},
    {"name": "ARGENTUM",   "port": 8017},
    {"name": "Soma",       "port": 8022},
]

OWNER_WALLET   = "0xDcc84E9798E8eB1b1b48A31B8f35e5AA7b83DBF4"
WALLET_LOW_ETH = 0.015  # alerta si baja de este umbral

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── State ──────────────────────────────────────────────────────────────────

def load_state():
    defaults = {
        "github_seen": {},
        "moltbook_seen": {},
        "stacker_seen": {},
        "last_daily": None,
        "pending_drafts": {},
        "traction_log": [],
    }
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        # Migración: agregar claves nuevas si no existen
        for k, v in defaults.items():
            state.setdefault(k, v)
        return state
    return defaults

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Logging ────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Telegram ───────────────────────────────────────────────────────────────

def tg_send(text, parse_mode=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text[:4000]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")

def tg_send_with_buttons(text, buttons, parse_mode=None):
    """Envía mensaje con inline keyboard."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    keyboard = {"inline_keyboard": [[
        {"text": b["text"], "callback_data": b["data"]}
        for b in buttons
    ]]}
    payload = {"chat_id": CHAT_ID, "text": text[:4000], "reply_markup": keyboard}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log(f"Telegram error: {e}")


# ── Clasificación con Haiku ────────────────────────────────────────────────

CLASSIFY_SYSTEM = """You are pioneer-agent-001, a monitoring agent for the Giskard MCP ecosystem.
Classify incoming messages/comments as:
- SPAM: promotional, unrelated, bot-generated
- RELEVANT: technical discussion, feedback, questions about Giskard
- URGENT: security issues, critical bugs, important partnership inquiries
- OPORTUNIDAD: ecosystem pain/gap/news that our stack already solves (memory, identity, payments, reputation). Examples: platforms losing features we provide, agents asking for tools we built, infrastructure breaking that we can replace.

Reply with exactly one word: SPAM, RELEVANT, URGENT, or OPORTUNIDAD."""

def classify(text: str) -> str:
    try:
        r = client.messages.create(
            model=MODEL,
            max_tokens=10,
            system=CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": text[:500]}]
        )
        return r.content[0].text.strip().upper()
    except Exception as e:
        log(f"classify error: {e}")
        return "RELEVANT"

INSIGHT_KEYWORDS = [
    "mcp", "memory", "lightning", "payment", "sats", "reputation", "karma",
    "attestation", "identity", "agent-to-agent", "zk", "proof", "protocol",
    "sdk", "token", "stake", "sybil", "coordination", "registry", "mark",
    "embedding", "episodic", "semantic", "inference cost", "paywall",
    "autonomy", "economic", "earn", "spend", "nara", "l402", "x402",
    "trust", "credential", "did", "verifiable", "wallet", "arbitrum",
]

# ── M12 — clasificacion 3 categorias para community scan ───────────────────

# (1) MENCION_DIRECTA — keywords que apuntan a nosotros
MENTION_KEYWORDS = [
    "giskard", "mycelium", "argentum", "giskard-marks", "giskard-memory",
    "giskard-search", "giskard-oasis", "giskard-origin", "giskardmcp",
    "giskard09", "rgiskard.xyz", "argentum.rgiskard", "rgiskard",
    "soma agents", "argt token",
]

# (2) DOLOR_CONOCIDO — pares (sintoma, capability_nuestra). Match si
# aparecen ambos lados o una frase compuesta que implica el dolor.
PAIN_PATTERNS = [
    # (any of these tokens) y (any of these tokens) en mismo texto
    {"capability": "memory",      "symptoms": ["forgets", "no memory", "session lost", "context window", "amnesia", "cant remember", "can't remember", "loses context", "perdida de contexto"]},
    {"capability": "identity",    "symptoms": ["no identity", "anonymous agent", "verify agent", "agent identity gap", "who is this agent", "identidad agente"]},
    {"capability": "reputation",  "symptoms": ["no reputation", "agent reputation", "trust agent", "rate agent", "verified action"]},
    {"capability": "payments",    "symptoms": ["agents pay", "agent payments", "no monetization", "agent billing", "x402", "l402", "lightning paywall", "agent revenue"]},
    {"capability": "discovery",   "symptoms": ["find agents", "discover agents", "agent registry", "reachability", "how to find agent", "agent endpoint"]},
    {"capability": "coordination","symptoms": ["agent coordination", "agents collaborate", "handoff between agents", "agent to agent", "a2a"]},
]

# (3) ALERTA_OP — operacional/seguridad/breaking
OP_ALERT_KEYWORDS = [
    "outage", "shutdown", "deprecated", "breaking change", "security advisory",
    "cve-", "vulnerability", "rate limit", "deprecation", "sunset",
    "service discontinued",
]

# C2 — keywords competitivos para Meridian-like scan
COMPETITIVE_KEYWORDS = [
    "agent identity", "agent reputation", "erc-8004", "erc8004",
    "trust layer agents", "karma agents", "privy agent",
    "coinbase verifications agents", "lit protocol agents",
    "worldcoin agents", "agent trust", "verifiable agent",
    "soulbound agent", "agent attestation", "agent credential",
]

# Capabilities Mycelium para overlap funcional
MYCELIUM_CAPS = [
    "memory", "identity", "reputation", "payments", "discovery",
    "coordination", "marks", "karma", "attestation", "lightning",
    "ed25519", "soul-bound",
]


def _has_any(text_lower: str, tokens: list) -> list:
    return [t for t in tokens if t in text_lower]


def _classify_categories(title: str, content: str) -> dict:
    """Devuelve dict con flags por categoria + matches (sin Haiku, fast path)."""
    text = (title + " " + content).lower()
    out = {
        "mention":   _has_any(text, MENTION_KEYWORDS),
        "ops":       _has_any(text, OP_ALERT_KEYWORDS),
        "pains":     [],
        "kw_match":  _has_any(text, INSIGHT_KEYWORDS),
        "competitive": _has_any(text, COMPETITIVE_KEYWORDS),
    }
    for p in PAIN_PATTERNS:
        hits = _has_any(text, p["symptoms"])
        if hits:
            out["pains"].append({"capability": p["capability"], "symptoms": hits})
    return out


PIONEER_CLASSIFY_SYSTEM = """You are pioneer-agent-001, scanning agent ecosystem content for the Mycelium stack (Giskard).

Categories:
- MENCION_DIRECTA: post explicitly mentions us (Giskard/Mycelium/ARGENTUM/Marks/Oasis/etc)
- DOLOR_CONOCIDO: post describes a real pain that our stack already solves (memory loss, no identity, no reputation, no payments, discovery gap, coordination)
- ALERTA_OP: operational/security/breaking change relevant to agent infra
- INSIGHT_GENERICO: technically interesting but not directly actionable
- NOISE: marketing fluff, hype, off-topic

Reply EXACTLY one line:
<CATEGORY>: <one short concrete takeaway in Spanish>
or
NOISE"""


def classify_pioneer(title: str, content: str) -> dict:
    """
    M12 — clasificador unificado por categoria. Devuelve dict:
      {"category": str, "takeaway": str, "matches": dict}
    Categorias: MENCION_DIRECTA, DOLOR_CONOCIDO, ALERTA_OP,
                INSIGHT_GENERICO, NOISE.

    Fast-path: si hay match keyword fuerte (mention u ops), no llama Haiku.
    Slow-path: para DOLOR_CONOCIDO o INSIGHT_GENERICO consulta Haiku.
    """
    cats = _classify_categories(title, content)

    # Fast path 1: mencion directa siempre dispara
    if cats["mention"]:
        return {
            "category": "MENCION_DIRECTA",
            "takeaway": f"mencion directa keywords: {', '.join(cats['mention'][:4])}",
            "matches": cats,
        }

    # Fast path 2: alerta operacional
    if cats["ops"]:
        return {
            "category": "ALERTA_OP",
            "takeaway": f"alerta op keywords: {', '.join(cats['ops'][:3])}",
            "matches": cats,
        }

    # Fast path 3: nada relevante
    if not cats["kw_match"] and not cats["pains"] and not cats["competitive"]:
        return {"category": "NOISE", "takeaway": "", "matches": cats}

    # Slow path: Haiku decide entre DOLOR_CONOCIDO / INSIGHT_GENERICO / NOISE
    try:
        text = f"Title: {title}\n\nContent: {content[:1200]}"
        if cats["pains"]:
            hint_caps = list({p["capability"] for p in cats["pains"]})
            text += f"\n\n(hint: pain pattern matched for capabilities: {', '.join(hint_caps)})"
        r = client.messages.create(
            model=MODEL,
            max_tokens=80,
            system=PIONEER_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": text}]
        )
        raw = r.content[0].text.strip()
        if raw.upper() == "NOISE" or raw.upper().startswith("NOISE"):
            return {"category": "NOISE", "takeaway": "", "matches": cats}
        if ":" in raw:
            cat_str, takeaway = raw.split(":", 1)
            cat = cat_str.strip().upper()
            if cat not in ("MENCION_DIRECTA", "DOLOR_CONOCIDO", "ALERTA_OP", "INSIGHT_GENERICO"):
                cat = "INSIGHT_GENERICO"
            return {"category": cat, "takeaway": takeaway.strip(), "matches": cats}
        return {"category": "INSIGHT_GENERICO", "takeaway": raw, "matches": cats}
    except Exception as e:
        log(f"classify_pioneer error (Haiku): {e} — usando keyword fallback")
        # Fallback sin Haiku: si hay pain match → DOLOR_CONOCIDO; sino → INSIGHT_GENERICO
        if cats["pains"]:
            caps = ", ".join({p["capability"] for p in cats["pains"]})
            return {
                "category": "DOLOR_CONOCIDO",
                "takeaway": f"dolor matcheado: {caps}",
                "matches": cats,
            }
        return {
            "category": "INSIGHT_GENERICO",
            "takeaway": f"keywords: {', '.join(cats['kw_match'][:4])}",
            "matches": cats,
        }


def _functional_overlap_pct(matches: dict) -> int:
    """C2 — % de overlap funcional con capabilities Mycelium.
    Heuristica: cuantos caps de MYCELIUM_CAPS aparecen en el texto + pains
    matcheados. 0..100."""
    text_caps = set(matches.get("kw_match", [])) | {p["capability"] for p in matches.get("pains", [])}
    overlap = text_caps & set(MYCELIUM_CAPS)
    if not text_caps:
        return 0
    return int(round(100 * len(overlap) / max(1, len(text_caps))))

TRACTION_SYSTEM = """You are pioneer-agent-001, analyzing community signals for the Giskard MCP ecosystem.

We build: MCP servers for agents (search, memory, identity, payments via Lightning/Arbitrum), ARGENTUM karma economy, Giskard Marks.

Analyze the message and reply with EXACTLY this format (3 lines, no extra text):
TIPO: POSITIVO|TECNICO|NEUTRO
ACCION: INFRA|RESPONDER|OBSERVAR|ARCHIVAR
RAZON: <one sentence in Spanish>

TIPO definitions:
- POSITIVO: validates our approach, endorses the project, genuine enthusiasm or alignment
- TECNICO: hard question, gap identified, improvement suggestion, insight that helps us build better
- NEUTRO: generic, low-signal, off-topic

ACCION definitions:
- INFRA: technical feedback worth implementing — pass to infra team
- RESPONDER: engage this user (check their profile first if unknown)
- OBSERVAR: interesting but no immediate action needed
- ARCHIVAR: low priority, move on"""

def classify_traction(text: str) -> dict:
    """Clasifica tracción y sugiere acción. Retorna dict con tipo, accion, razon."""
    default = {"tipo": "NEUTRO", "accion": "ARCHIVAR", "razon": "Sin señal relevante"}
    try:
        r = client.messages.create(
            model=MODEL,
            max_tokens=80,
            system=TRACTION_SYSTEM,
            messages=[{"role": "user", "content": text[:600]}]
        )
        lines = r.content[0].text.strip().splitlines()
        result = {}
        for line in lines:
            if line.startswith("TIPO:"):
                result["tipo"] = line.split(":", 1)[1].strip().upper()
            elif line.startswith("ACCION:"):
                result["accion"] = line.split(":", 1)[1].strip().upper()
            elif line.startswith("RAZON:"):
                result["razon"] = line.split(":", 1)[1].strip()
        # Validar
        if result.get("tipo") not in ("POSITIVO", "TECNICO", "NEUTRO"):
            result["tipo"] = "NEUTRO"
        if result.get("accion") not in ("INFRA", "RESPONDER", "OBSERVAR", "ARCHIVAR"):
            result["accion"] = "ARCHIVAR"
        return {**default, **result}
    except Exception as e:
        log(f"classify_traction error: {e}")
        return default


def check_github_profile(username: str) -> str:
    """Verifica si un usuario tiene perfil GitHub activo. Retorna resumen breve."""
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(f"https://api.github.com/users/{username}", headers=headers, timeout=8)
        if r.status_code != 200:
            return "sin perfil GitHub verificable"
        u = r.json()
        repos = u.get("public_repos", 0)
        followers = u.get("followers", 0)
        created = u.get("created_at", "")[:4]
        if repos == 0 and followers == 0:
            return f"GitHub existe ({created}) pero sin actividad pública"
        return f"GitHub activo: {repos} repos, {followers} seguidores (desde {created})"
    except Exception:
        return "no se pudo verificar GitHub"


DRAFT_SYSTEM = """You are pioneer-agent-001, helping draft responses for the Giskard MCP ecosystem.
Write a technical reply in English. Be precise, concise, no fluff. Max 3 short paragraphs.
Do not use emojis. Do not start with 'Hi' or 'Hello'."""

def generate_draft(context: str, new_comment: str) -> str:
    try:
        r = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=DRAFT_SYSTEM,
            messages=[{"role": "user", "content": f"Context: {context}\n\nNew comment: {new_comment}"}]
        )
        return r.content[0].text.strip()
    except Exception as e:
        log(f"draft error: {e}")
        return ""


# ── Giskard Memory ─────────────────────────────────────────────────────────

def store_memory(content: str):
    try:
        r = httpx.post(f"{BASE}:8005/store_direct",
            json={"content": content, "agent_id": AGENT_ID,
                  "metadata": {"source": "pioneer-agent", "ts": datetime.now().isoformat()}},
            timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def store_decision(problem: str, options: list, chosen: str, reason: str, discarded: dict = None):
    """Guarda una decisión con su razonamiento completo — no solo el resultado.

    problem:   qué problema se estaba resolviendo
    options:   lista de opciones evaluadas
    chosen:    la opción elegida
    reason:    por qué se eligió
    discarded: dict opcional — {opcion: motivo_descarte} para opciones que se consideraron y rechazaron
    """
    options_text = "\n".join([f"- {o}" for o in options])
    content = (
        f"DECISIÓN\n"
        f"PROBLEMA: {problem}\n"
        f"OPCIONES EVALUADAS:\n{options_text}\n"
        f"ELEGIDA: {chosen}\n"
        f"RAZÓN: {reason}"
    )
    if discarded:
        disc_text = "\n".join([f"- {op}: {motivo}" for op, motivo in discarded.items()])
        content += f"\nDESCARTADAS:\n{disc_text}"
    return store_memory(content)


# ── Health Check ───────────────────────────────────────────────────────────

def check_services():
    results = []
    for svc in SERVICES:
        status = "DOWN"
        for attempt in range(3):          # 3 intentos antes de declarar caído
            try:
                httpx.get(f"{BASE}:{svc['port']}/health", timeout=8)
                status = "OK"
                break
            except Exception:
                if attempt < 2:
                    import time; time.sleep(2)  # espera 2s entre intentos
        results.append({"name": svc["name"], "port": svc["port"], "status": status})
    return results


# ── GitHub Monitor ─────────────────────────────────────────────────────────

PIZARRA_FILE = Path("/home/dell7568/Downloads/pizarra.txt")
MENTIONS_THRESHOLD_HOURS = 6


def check_github_mentions(state: dict) -> None:
    """Consulta notificaciones GitHub con reason=mention.
    Si hay menciones sin respuesta nuestra en +6hs → escribe en pizarra.txt.
    """
    if not GITHUB_TOKEN:
        return
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.get(
            "https://api.github.com/notifications",
            headers=headers,
            params={"all": "false", "participating": "true", "per_page": 50},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"mentions: API error {r.status_code}")
            return
        notifications = r.json()
    except Exception as e:
        log(f"mentions: fetch error {e}")
        return

    now = datetime.utcnow()
    pending = []
    seen_mentions = state.setdefault("seen_mentions", {})

    for n in notifications:
        if n.get("reason") != "mention":
            continue
        nid = str(n["id"])
        updated_at_str = n.get("updated_at", "")
        try:
            updated_at = datetime.strptime(updated_at_str, "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
        age_hours = (now - updated_at).total_seconds() / 3600
        if age_hours < MENTIONS_THRESHOLD_HOURS:
            continue
        if seen_mentions.get(nid) == "deposited":
            continue

        subject = n.get("subject", {})
        repo_name = n.get("repository", {}).get("full_name", "")
        title = subject.get("title", "")
        url = subject.get("url", "").replace("https://api.github.com/repos/", "https://github.com/").replace("/pulls/", "/pull/")
        pending.append({"id": nid, "repo": repo_name, "title": title, "url": url, "age_h": round(age_hours, 1)})
        seen_mentions[nid] = "deposited"

    if not pending:
        return

    # Escribir en pizarra.txt bajo sección TAGS SIN RESPUESTA
    try:
        pizarra = PIZARRA_FILE.read_text() if PIZARRA_FILE.exists() else ""
        section_header = "\n====================================================================\nTAGS SIN RESPUESTA (pioneer)\n====================================================================\n"
        lines = [f"- [{p['repo']}] {p['title'][:80]} — {p['age_h']}h sin respuesta\n  {p['url']}" for p in pending]
        block = section_header + "\n".join(lines) + "\n"

        if "TAGS SIN RESPUESTA (pioneer)" in pizarra:
            # reemplazar sección existente
            import re
            pizarra = re.sub(
                r"\n====================================================================\nTAGS SIN RESPUESTA \(pioneer\)\n====================================================================\n.*?(?=\n====================================================================|\Z)",
                block,
                pizarra,
                flags=re.DOTALL,
            )
        else:
            pizarra = block + pizarra

        PIZARRA_FILE.write_text(pizarra)
        log(f"mentions: {len(pending)} tags sin respuesta depositados en pizarra.txt")
        tg_send(f"[pioneer] {len(pending)} tag(s) sin respuesta en GitHub (+{MENTIONS_THRESHOLD_HOURS}h):\n" +
                "\n".join(f"• {p['repo']} — {p['title'][:60]}" for p in pending))
    except Exception as e:
        log(f"mentions: pizarra write error {e}")

    save_state(state)


def get_github_comments(repo: str, kind: str, number: int):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    if kind == "discussion":
        # GraphQL
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            discussion(number: $number) {
              title
              comments(last: 10) {
                nodes { id body author { login } createdAt }
              }
            }
          }
        }"""
        owner, reponame = repo.split("/")
        r = requests.post("https://api.github.com/graphql",
            headers=headers,
            json={"query": query, "variables": {"owner": owner, "repo": reponame, "number": number}},
            timeout=10)
        if r.status_code != 200:
            return [], ""
        rjson = r.json()
        if not rjson:
            return [], ""
        data = rjson.get("data", {}).get("repository", {}).get("discussion", {})
        title = data.get("title", "")
        comments = [{"id": c["id"], "body": c["body"], "user": c["author"]["login"]}
                    for c in data.get("comments", {}).get("nodes", [])]
        return comments, title
    else:
        endpoint = "pulls" if kind == "pr" else "issues"
        r = requests.get(f"https://api.github.com/repos/{repo}/{endpoint}/{number}/comments",
            headers=headers, timeout=10)
        if r.status_code != 200:
            return [], ""
        # Get title
        r2 = requests.get(f"https://api.github.com/repos/{repo}/{endpoint}/{number}",
            headers=headers, timeout=10)
        title = r2.json().get("title", "") if r2.status_code == 200 else ""
        return r.json(), title

def post_github_comment(repo: str, kind: str, number: int, body: str):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    endpoint = "pulls" if kind == "pr" else "issues"
    r = requests.post(f"https://api.github.com/repos/{repo}/{endpoint}/{number}/comments",
        headers=headers, json={"body": body}, timeout=10)
    return r.status_code == 201

def check_github(state):
    alerts = []
    for watch in GITHUB_WATCH:
        repo, kind, number, label = watch["repo"], watch["type"], watch["number"], watch["label"]
        comments, title = get_github_comments(repo, kind, number)
        for c in comments:
            cid = str(c.get("id", c.get("body", "")[:20]))
            key = f"{repo}:{number}:{cid}"
            if key in state["github_seen"]:
                continue
            state["github_seen"][key] = True
            user = c.get("user", {})
            author = user.get("login", "unknown") if isinstance(user, dict) else str(user)
            if author in ("giskardmcp", "giskard09", "github-actions[bot]"):
                continue
            body = c.get("body", "")
            classification = classify(body)
            log(f"GitHub [{label}] new comment from {author}: {classification}")
            if classification == "SPAM":
                continue
            alerts.append({
                "source": "github",
                "label": label,
                "repo": repo,
                "kind": kind,
                "number": number,
                "title": title,
                "author": author,
                "body": body,
                "classification": classification,
            })
    return alerts


# ── Moltbook Monitor ───────────────────────────────────────────────────────

def check_stacker(state):
    alerts = []
    if "stacker_seen" not in state:
        state["stacker_seen"] = {}
    for item in STACKER_WATCH:
        try:
            r = requests.get(
                f"https://stacker.news/api/graphql",
                json={"query": f'{{ item(id: {item["id"]}) {{ title sats comments {{ id text createdAt user {{ name }} }} }} }}'},
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if r.status_code != 200:
                continue
            data = r.json().get("data", {}).get("item", {})
            if not data:
                continue
            sats = data.get("sats", 0)
            sats_key = f'{item["id"]}:sats:{sats}'
            if sats > 0 and sats_key not in state["stacker_seen"]:
                state["stacker_seen"][sats_key] = True
                alerts.append({
                    "source": "stacker",
                    "post_title": item["title"],
                    "author": "—",
                    "body": f"{sats} sats recibidos en el post",
                    "url": f'https://stacker.news/items/{item["id"]}',
                })
            for comment in data.get("comments", []):
                cid = comment.get("id", "")
                key = f'{item["id"]}:{cid}'
                if key in state["stacker_seen"]:
                    continue
                state["stacker_seen"][key] = True
                author = comment.get("user", {}).get("name", "unknown")
                text = comment.get("text", "")
                classification = classify(text)
                log(f"Stacker new comment from {author}: {classification}")
                if classification == "SPAM":
                    continue
                alerts.append({
                    "source": "stacker",
                    "post_title": item["title"],
                    "author": author,
                    "body": text,
                    "url": f'https://stacker.news/items/{item["id"]}',
                })
        except Exception as e:
            log(f"Stacker check error: {e}")
    return alerts


def check_moltbook(state):
    alerts = []
    headers = {"Authorization": f"Bearer {MOLTBOOK_KEY}"}
    try:
        r = requests.get("https://www.moltbook.com/api/v1/home",
            headers=headers, timeout=10)
        if r.status_code != 200:
            return alerts
        items = r.json().get("posts", r.json().get("items", []))
        for item in items[:20]:
            pid = item.get("id", "")
            for comment in item.get("comments", []):
                cid = comment.get("id", "")
                key = f"{pid}:{cid}"
                if key in state["moltbook_seen"]:
                    continue
                state["moltbook_seen"][key] = True
                author = comment.get("author", {}).get("name", "unknown")
                if author == "giskardmcp":
                    continue
                body = comment.get("content", "")
                classification = classify(body)
                log(f"Moltbook new comment from {author}: {classification}")
                if classification == "SPAM":
                    continue
                alerts.append({
                    "source": "moltbook",
                    "post_title": item.get("title", ""),
                    "author": author,
                    "body": body,
                    "classification": classification,
                })
    except Exception as e:
        log(f"Moltbook check error: {e}")
    return alerts


# ── Community scan ────────────────────────────────────────────────────────────

def scan_moltbook_community(state):
    """
    M12 — rastrilla submolts de Moltbook buscando contenido relevante.
    Corre una vez por dia. Clasifica por 4 categorias:
      MENCION_DIRECTA → alerta inmediata Telegram (prioridad maxima)
      DOLOR_CONOCIDO  → alerta Telegram con tag oportunidad
      ALERTA_OP       → alerta operacional Telegram
      INSIGHT_GENERICO → no Telegram, queda en weekly_insights para digest lunes
      NOISE → descartar
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("community_scan_last") == today:
        return  # ya corrió hoy

    headers = {"Authorization": f"Bearer {MOLTBOOK_KEY}"}
    if "community_seen" not in state:
        state["community_seen"] = {}
    if "weekly_insights" not in state:
        state["weekly_insights"] = []

    counts = {"MENCION_DIRECTA": 0, "DOLOR_CONOCIDO": 0, "ALERTA_OP": 0,
              "INSIGHT_GENERICO": 0, "NOISE": 0}
    immediate_alerts = []  # acumulamos para mandar 1 mensaje por categoria

    for submolt in COMMUNITY_SUBMOLTS:
        try:
            r = requests.get(
                "https://www.moltbook.com/api/v1/posts",
                headers=headers,
                params={"submolt": submolt, "limit": 10},
                timeout=15
            )
            if r.status_code != 200:
                log(f"community scan: {submolt} returned {r.status_code}")
                continue
            posts = r.json().get("posts", [])
            for post in posts:
                pid = post.get("id", "")
                if pid in state["community_seen"]:
                    continue
                state["community_seen"][pid] = today

                author_name = post.get("author", {}).get("name", "")
                if author_name in ("giskardmcp",) or post.get("is_spam"):
                    continue

                title = post.get("title", "")
                content = post.get("content", "")
                cls = classify_pioneer(title, content)
                cat = cls["category"]
                counts[cat] = counts.get(cat, 0) + 1

                log(f"community [{submolt}] @{author_name}: {cat} — {cls['takeaway'][:50]}")

                if cat == "NOISE":
                    continue

                entry = {
                    "submolt":  submolt,
                    "author":   author_name,
                    "title":    title[:80],
                    "takeaway": cls["takeaway"],
                    "post_id":  pid,
                    "url":      f"https://www.moltbook.com/p/{pid}",
                    "upvotes":  post.get("upvotes", 0),
                    "category": cat,
                    "matches":  cls.get("matches", {}),
                    "date":     today,
                }

                if cat == "MENCION_DIRECTA":
                    store_memory(
                        f"mencion_directa [{submolt}] @{author_name}: {cls['takeaway']} — post: {pid}"
                    )
                    immediate_alerts.append(entry)
                elif cat == "DOLOR_CONOCIDO":
                    pains = entry["matches"].get("pains", [])
                    caps = ", ".join({p["capability"] for p in pains}) if pains else "?"
                    store_memory(
                        f"dolor_conocido [{submolt}] @{author_name} caps={caps}: "
                        f"{cls['takeaway']} — post: {pid}"
                    )
                    immediate_alerts.append(entry)
                elif cat == "ALERTA_OP":
                    store_memory(
                        f"alerta_op [{submolt}] @{author_name}: {cls['takeaway']} — post: {pid}"
                    )
                    immediate_alerts.append(entry)
                else:  # INSIGHT_GENERICO
                    state["weekly_insights"].append(entry)
                    store_memory(
                        f"insight_generico [{submolt}] @{author_name}: {cls['takeaway']} — post: {pid}"
                    )

        except Exception as e:
            log(f"community scan error [{submolt}]: {e}")

    # Limpiar seen antiguo (mantener solo últimos 500)
    if len(state["community_seen"]) > 500:
        keys = list(state["community_seen"].keys())
        state["community_seen"] = {k: state["community_seen"][k] for k in keys[-500:]}

    state["community_scan_last"] = today

    # Alertas inmediatas — agrupadas por categoria
    if immediate_alerts:
        by_cat = {}
        for e in immediate_alerts:
            by_cat.setdefault(e["category"], []).append(e)

        cat_order = ["MENCION_DIRECTA", "DOLOR_CONOCIDO", "ALERTA_OP"]
        cat_labels = {
            "MENCION_DIRECTA": "🎯 MENCION DIRECTA",
            "DOLOR_CONOCIDO":  "💡 DOLOR CONOCIDO",
            "ALERTA_OP":       "⚠ ALERTA OP",
        }
        for cat in cat_order:
            if cat not in by_cat:
                continue
            entries = by_cat[cat]
            lines = [f"[pioneer] {cat_labels.get(cat, cat)} — {len(entries)} hit(s)\n"]
            for i, e in enumerate(entries[:10], 1):
                extra = ""
                if cat == "DOLOR_CONOCIDO":
                    pains = e.get("matches", {}).get("pains", [])
                    if pains:
                        caps = ", ".join({p["capability"] for p in pains})
                        extra = f" [caps: {caps}]"
                lines.append(
                    f"{i}. @{e['author']} en /{e['submolt']}{extra}\n"
                    f"   \"{e['title']}\"\n"
                    f"   => {e['takeaway']}\n"
                    f"   {e['url']}"
                )
            tg_send("\n".join(lines))

    log(f"community scan done — {counts}")


# ── C2 — Competitive intel scan (Meridian-like, semanal) ──────────────────

COMPETITIVE_GH_QUERIES = [
    "topic:agents+ERC-8004",
    "topic:erc-8004",
    "topic:agent-identity",
    "topic:agent-reputation",
    "agent+identity+karma+in:readme",
]


def _gh_search_repos(query: str, since_iso: str = "", limit: int = 5) -> list:
    """GitHub repo search via REST. since_iso es opcional (created/pushed)."""
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    q = query
    if since_iso:
        q = f"{query}+pushed:>{since_iso}"
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params={"q": q, "sort": "updated", "order": "desc", "per_page": limit},
            timeout=12,
        )
        if r.status_code != 200:
            log(f"gh search {query}: status {r.status_code}")
            return []
        return r.json().get("items", [])
    except Exception as e:
        log(f"gh search error {query}: {e}")
        return []


def _moltbook_search_competitive(state: dict, days: int = 7) -> list:
    """Busca posts en submolts con keywords competitivos en los ultimos N dias."""
    headers = {"Authorization": f"Bearer {MOLTBOOK_KEY}"}
    hits = []
    seen_pids = set()
    for submolt in COMMUNITY_SUBMOLTS:
        try:
            r = requests.get(
                "https://www.moltbook.com/api/v1/posts",
                headers=headers,
                params={"submolt": submolt, "limit": 25},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            posts = r.json().get("posts", [])
            for post in posts:
                pid = post.get("id", "")
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                title = post.get("title", "")
                content = post.get("content", "")
                text = (title + " " + content).lower()
                comp_hits = _has_any(text, COMPETITIVE_KEYWORDS)
                if not comp_hits:
                    continue
                cats = _classify_categories(title, content)
                overlap = _functional_overlap_pct(cats)
                hits.append({
                    "source": "moltbook",
                    "submolt": submolt,
                    "title": title[:80],
                    "url": f"https://www.moltbook.com/p/{pid}",
                    "competitive_keywords": comp_hits,
                    "overlap_pct": overlap,
                    "matches": cats,
                })
        except Exception as e:
            log(f"competitive moltbook error [{submolt}]: {e}")
    return hits


OVERLAP_THRESHOLD = 50  # calibrar según output: >5 flags→70, 0-1 flags→50, 2-4→dejar


def competitive_intel_scan(state: dict) -> dict:
    """C2 — corre solo lunes 09:00. Genera reporte competitivo."""
    from datetime import date, timedelta
    week_ago = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")

    gh_hits = []
    for q in COMPETITIVE_GH_QUERIES:
        repos = _gh_search_repos(q, since_iso=week_ago, limit=5)
        for repo in repos:
            full_name = repo.get("full_name", "")
            desc = repo.get("description", "") or ""
            stars = repo.get("stargazers_count", 0)
            text = (full_name + " " + desc).lower()
            comp_hits = _has_any(text, COMPETITIVE_KEYWORDS)
            cats = _classify_categories(full_name, desc)
            overlap = _functional_overlap_pct(cats)
            gh_hits.append({
                "source": "github",
                "query": q,
                "full_name": full_name,
                "desc": desc[:120],
                "stars": stars,
                "url": repo.get("html_url", ""),
                "competitive_keywords": comp_hits,
                "overlap_pct": overlap,
                "matches": cats,
            })

    mb_hits = _moltbook_search_competitive(state, days=7)
    all_hits = gh_hits + mb_hits

    # Dedup por url
    seen_urls = set()
    deduped = []
    for h in all_hits:
        u = h.get("url", "")
        if u and u in seen_urls:
            continue
        seen_urls.add(u)
        deduped.append(h)

    flagged = [h for h in deduped if h.get("overlap_pct", 0) > OVERLAP_THRESHOLD]

    report = {
        "week_ending": datetime.now().strftime("%Y-%m-%d"),
        "total_hits": len(deduped),
        "flagged_count": len(flagged),
        "flagged": flagged,
        "all": deduped,
    }
    state["competitive_last_report"] = report
    return report


def send_competitive_report(report: dict):
    flagged = report.get("flagged", [])
    total = report.get("total_hits", 0)
    if total == 0:
        tg_send(f"[pioneer] COMPETITIVE INTEL — semana {report['week_ending']}: 0 hits.")
        return

    lines = [
        f"[pioneer] COMPETITIVE INTEL — semana {report['week_ending']}",
        f"Total hits: {total}. Flagged (>60% overlap): {len(flagged)}\n",
    ]
    if flagged:
        lines.append("FLAGGED — competidores con alto overlap funcional:")
        for i, h in enumerate(flagged[:8], 1):
            label = h.get("full_name") or h.get("title", "?")
            kws = ", ".join(h.get("competitive_keywords", [])[:3])
            lines.append(
                f"{i}. [{h['source']}] {label} — overlap {h['overlap_pct']}%\n"
                f"   kw: {kws}\n"
                f"   {h.get('url','')}"
            )
    else:
        lines.append("Sin flags >60% esta semana. Hits abajo (informativo):")
        for h in report["all"][:5]:
            label = h.get("full_name") or h.get("title", "?")
            lines.append(f"  - [{h['source']}] {label} — overlap {h['overlap_pct']}%")

    tg_send("\n".join(lines))
    store_memory(
        f"competitive_intel weekly {report['week_ending']}: total={total}, "
        f"flagged={len(flagged)}, top={[h.get('full_name') or h.get('title') for h in flagged[:3]]}"
    )


# ── Weekly digest — insights genericos acumulados ─────────────────────────

def weekly_digest(state: dict):
    """Lunes 09:00 — agrupa todos los INSIGHT_GENERICO de la semana en un
    digest consolidado a Telegram + memoria. Limpia weekly_insights."""
    insights = state.get("weekly_insights", [])
    if not insights:
        tg_send(f"[pioneer] WEEKLY DIGEST {datetime.now().strftime('%Y-%m-%d')}: sin insights nuevos esta semana.")
        return

    # Top por upvotes
    top = sorted(insights, key=lambda e: e.get("upvotes", 0), reverse=True)[:8]
    lines = [
        f"[pioneer] WEEKLY DIGEST — {datetime.now().strftime('%Y-%m-%d')}",
        f"{len(insights)} insight(s) genericos acumulados esta semana\n",
    ]
    for i, e in enumerate(top, 1):
        lines.append(
            f"{i}. @{e.get('author','?')} en /{e.get('submolt','?')} ({e.get('upvotes',0)} upvotes)\n"
            f"   \"{e.get('title','')}\"\n"
            f"   => {e.get('takeaway','')}\n"
            f"   {e.get('url','')}"
        )
    tg_send("\n".join(lines))
    store_memory(
        f"weekly_digest {datetime.now().strftime('%Y-%m-%d')}: {len(insights)} insights, "
        f"top authors: {[e.get('author') for e in top[:3]]}"
    )
    # Limpiar tras enviar
    state["weekly_insights"] = []


def should_send_weekly(state) -> bool:
    """Lunes 09:xx Bs As, una sola vez por semana."""
    now = datetime.now()
    if now.weekday() != 0 or now.hour != 9:  # 0 = lunes
        return False
    week_key = now.strftime("%Y-W%W")
    if state.get("last_weekly") == week_key:
        return False
    state["last_weekly"] = week_key
    return True


# ── Proceso de alertas ─────────────────────────────────────────────────────

def process_alerts(alerts, state):
    for alert in alerts:
        src = alert["source"]
        classification = alert.get("classification", "RELEVANT")
        author = alert["author"]
        body = alert["body"][:300]

        if src == "github":
            label = alert["label"]
            context = f"GitHub {alert['kind'].upper()} #{alert['number']}: {alert['title']}"
            draft = generate_draft(context, body)
            draft_id = hashlib.md5(f"{label}{body}".encode()).hexdigest()[:8]
            state["pending_drafts"][draft_id] = {
                "draft": draft,
                "source": "github",
                "repo": alert["repo"],
                "kind": alert["kind"],
                "number": alert["number"],
            }

            # Anthropic/MCP maintainers → LAB
            lab_gh_users = {"olaservo", "cliffhall", "henroger", "dsp-ant",
                            "maheshmurag", "jerome3o-anthropic", "ashwin-ant", "msaleme"}
            lab_tag = "[LAB] " if author.lower() in lab_gh_users else ""
            oport_tag = "[OPORTUNIDAD] " if classification == "OPORTUNIDAD" else ""
            urgency = "URGENTE" if classification == "URGENT" else ("OPORTUNIDAD" if classification == "OPORTUNIDAD" else "NUEVO")
            if classification == "OPORTUNIDAD":
                store_memory(f"oportunidad [github {label}] @{author}: {body[:300]}")
            msg = (
                f"[pioneer] {lab_tag}{oport_tag}{urgency} — GitHub {label}\n"
                f"@{author}: {body}\n\n"
                f"Borrador:\n{draft}\n\n"
                f"ID: {draft_id}"
            )
            tg_send_with_buttons(msg, [
                {"text": "Publicar", "data": f"p_gh_send:{draft_id}"},
                {"text": "Editar",   "data": f"p_gh_edit:{draft_id}"},
                {"text": "Ignorar",  "data": f"p_gh_ignore:{draft_id}"},
            ])
            if lab_tag:
                store_memory(f"LAB contact activity — @{author} en GitHub {label}: {body[:200]}")

        elif src == "moltbook":
            urgency = "URGENTE" if classification == "URGENT" else ("OPORTUNIDAD" if classification == "OPORTUNIDAD" else "NUEVO")
            if classification == "OPORTUNIDAD":
                store_memory(f"oportunidad [moltbook] @{author}: {body[:300]}")
            # Contactos estratégicos → tag LAB
            lab_contacts = {"oceantiger", "feri-sanyi", "feri-sanyi-agent", "msaleme",
                            "petchevere", "fransdev", "francesdevelopment"}
            lab_tag = "[LAB] " if author.lower() in lab_contacts else ""

            # Clasificar tracción + acción sugerida
            tr = classify_traction(body)
            traction, accion, razon = tr["tipo"], tr["accion"], tr["razon"]
            traction_tag = f" [{traction}]" if traction in ("POSITIVO", "TECNICO") else ""
            if traction in ("POSITIVO", "TECNICO"):
                # Verificar perfil si es acción RESPONDER
                perfil = ""
                if accion == "RESPONDER":
                    perfil = check_github_profile(author)
                # Detectar recurrencia
                prev = [e for e in state.get("traction_log", []) if e.get("author") == author]
                recurrente = len(prev) >= 2
                entry = {
                    "date":      datetime.now().strftime("%Y-%m-%d"),
                    "channel":   "moltbook",
                    "author":    author,
                    "post":      alert['post_title'],
                    "type":      traction,
                    "accion":    accion,
                    "razon":     razon,
                    "perfil":    perfil,
                    "recurrente": recurrente,
                    "body":      body[:300],
                }
                state.setdefault("traction_log", []).append(entry)
                mem_tag = "traction_signal"
                if accion == "INFRA":
                    mem_tag = "infra_signal"
                store_memory(
                    f"{mem_tag} [{traction}|{accion}] @{author} en Moltbook/{alert['post_title']}: {body[:200]}. Razón: {razon}"
                )

            msg = (
                f"[pioneer] {lab_tag}{urgency}{traction_tag} — Moltbook\n"
                f"Post: {alert['post_title']}\n"
                f"@{author}: {body}"
            )
            tg_send(msg)
            if lab_tag:
                store_memory(f"LAB contact activity — @{author} en Moltbook: {body[:200]}")

        elif src == "stacker":
            tr = classify_traction(body)
            traction, accion, razon = tr["tipo"], tr["accion"], tr["razon"]
            if traction in ("POSITIVO", "TECNICO"):
                perfil = check_github_profile(author) if accion == "RESPONDER" else ""
                prev = [e for e in state.get("traction_log", []) if e.get("author") == author]
                entry = {
                    "date":      datetime.now().strftime("%Y-%m-%d"),
                    "channel":   "stacker",
                    "author":    author,
                    "post":      alert['post_title'],
                    "type":      traction,
                    "accion":    accion,
                    "razon":     razon,
                    "perfil":    perfil,
                    "recurrente": len(prev) >= 2,
                    "body":      body[:300],
                }
                state.setdefault("traction_log", []).append(entry)
                mem_tag = "infra_signal" if accion == "INFRA" else "traction_signal"
                store_memory(
                    f"{mem_tag} [{traction}|{accion}] @{author} en Stacker/{alert['post_title']}: {body[:200]}. Razón: {razon}"
                )

            msg = (
                f"[pioneer] STACKER NEWS\n"
                f"Post: {alert['post_title']}\n"
                f"@{author}: {body}\n"
                f"{alert.get('url', '')}"
            )
            tg_send(msg)


# ── Daily report ───────────────────────────────────────────────────────────

def should_send_daily(state):
    now = datetime.now()
    if now.hour != 9:
        return False
    today = now.strftime("%Y-%m-%d")
    if state.get("last_daily") == today:
        return False
    return True

def send_daily_report(state):
    services = check_services()
    ok = [s for s in services if s["status"] == "OK"]
    down = [s for s in services if s["status"] != "OK"]

    wallet_eth = state.get("wallet_eth", -1)
    wallet_str = f"{wallet_eth:.4f} ETH" if wallet_eth >= 0 else "N/A"
    wallet_warn = " ⚠" if 0 <= wallet_eth < WALLET_LOW_ETH else ""

    lines = ["[pioneer] Reporte diario\n"]
    lines.append(f"Servicios OK: {len(ok)}/{len(services)}")
    if down:
        lines.append("Caídos: " + ", ".join(f"{s['name']}:{s['port']}" for s in down))
    lines.append(f"Wallet: {wallet_str}{wallet_warn}")
    lines.append(f"GitHub monitoreando: {len(GITHUB_WATCH)} fuentes")
    lines.append(f"Borradores pendientes: {len(state.get('pending_drafts', {}))}")

    tg_send("\n".join(lines))
    state["last_daily"] = datetime.now().strftime("%Y-%m-%d")
    store_memory(
        f"Daily report. services: {len(ok)}/{len(services)}. "
        f"wallet: {wallet_str}. drafts: {len(state.get('pending_drafts', {}))}."
    )

    # daily_intel para giskard-self — resumen que Giskard recupera al inicio de sesión
    daily_intel = build_daily_intel(state, ok, down, wallet_str)
    try:
        httpx.post(f"{BASE}:8005/store_direct",
            json={"content": daily_intel, "agent_id": "giskard-self",
                  "metadata": {"source": "pioneer-daily-intel", "tag": "daily_intel",
                               "date": datetime.now().strftime("%Y-%m-%d"),
                               "ts": datetime.now().isoformat()}},
            timeout=10)
        log("daily_intel stored for giskard-self")
    except Exception as e:
        log(f"daily_intel store error: {e}")

    # Community report en el daily
    community_report = build_community_report(state)
    tg_send(community_report)


def build_daily_intel(state, ok_services, down_services, wallet_str) -> str:
    """Resumen diario para giskard-self. Giskard lo recupera con recall_direct al inicio de sesión."""
    today = datetime.now().strftime("%Y-%m-%d")
    tlog = state.get("traction_log", [])
    recent = [e for e in tlog if e.get("date", "") == today]

    lines = [f"daily_intel {today}"]
    lines.append(f"servicios: {len(ok_services)} OK, {len(down_services)} caídos")
    if down_services:
        lines.append(f"caídos: {', '.join(s['name'] for s in down_services)}")
    lines.append(f"wallet: {wallet_str}")
    lines.append(f"señales hoy: {len(recent)} ({sum(1 for e in recent if e.get('type') == 'POSITIVO')} positivas, {sum(1 for e in recent if e.get('type') == 'TECNICO')} técnicas)")
    lines.append(f"borradores pendientes: {len(state.get('pending_drafts', {}))}")

    # Oportunidades recientes (últimas 48h)
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    oportunidades = [e for e in tlog if e.get("date", "") >= cutoff and e.get("classification") == "OPORTUNIDAD"]
    if oportunidades:
        lines.append(f"OPORTUNIDADES detectadas: {len(oportunidades)}")
        for o in oportunidades[:3]:
            lines.append(f"  - @{o.get('author', '?')} en {o.get('channel', '?')}: {o.get('body', '')[:100]}")

    # Insights del community scan de hoy
    insights_today = [k for k, v in state.get("community_seen", {}).items() if v == today]
    if insights_today:
        lines.append(f"community scan: {len(insights_today)} posts procesados hoy")

    return "\n".join(lines)


# ── Community report ──────────────────────────────────────────────────────

def build_community_report(state) -> str:
    """
    Genera el reporte estructurado de comunidad para Giskard.
    Incluye señales por tipo, acción sugerida, verificación de perfil y pendientes.
    """
    from datetime import date, timedelta
    tlog = state.get("traction_log", [])
    cutoff_7  = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    cutoff_48 = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    recent = [e for e in tlog if e.get("date", "") >= cutoff_7]

    positivos = [e for e in recent if e.get("type") == "POSITIVO"]
    tecnicos  = [e for e in recent if e.get("type") == "TECNICO"]

    # Contactos recurrentes (mismo autor 2+ señales en el log completo)
    from collections import Counter
    author_counts = Counter(e.get("author") for e in tlog if e.get("type") in ("POSITIVO", "TECNICO"))
    recurrentes = {a for a, c in author_counts.items() if c >= 2}

    # Sin respuesta (RESPONDER, últimas 48h — marcamos como pendiente)
    sin_respuesta = [e for e in tlog if e.get("accion") == "RESPONDER"
                     and e.get("date", "") >= cutoff_48]

    lines = [f"COMUNIDAD — {date.today().strftime('%Y-%m-%d')} (últimos 7 días)\n"]

    def fmt_entry(e):
        rec = " [RECURRENTE]" if e.get("author") in recurrentes else ""
        perfil = f" — {e['perfil']}" if e.get("perfil") else ""
        accion = e.get("accion", "OBSERVAR")
        razon  = e.get("razon", "")
        return (
            f"  @{e['author']}{rec} en {e['channel']}/{e['post']}\n"
            f"  → {e['body'][:100]}\n"
            f"  ACCION: {accion} — {razon}{perfil}"
        )

    if positivos:
        lines.append(f"POSITIVOS ({len(positivos)}):")
        for e in positivos[-5:]:
            lines.append(fmt_entry(e))
    else:
        lines.append("POSITIVOS (0): —")

    if tecnicos:
        lines.append(f"\nTECNICOS ({len(tecnicos)}):")
        for e in tecnicos[-5:]:
            lines.append(fmt_entry(e))
    else:
        lines.append("\nTECNICOS (0): —")

    if sin_respuesta:
        lines.append(f"\nPENDIENTES SIN RESPUESTA (48h):")
        for e in sin_respuesta:
            lines.append(f"  @{e['author']} en {e['channel']}/{e['post']} — {e['date']}")

    if recurrentes:
        lines.append(f"\nCONTACTOS RECURRENTES: {', '.join(f'@{a}' for a in recurrentes)}")

    report = "\n".join(lines)
    store_memory(f"community_report\n{report}")
    return report


# ── Main ───────────────────────────────────────────────────────────────────

# ── Market movement detector ───────────────────────────────────────────────

def get_eth_price() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
            timeout=5
        )
        return float(r.json()["ethereum"]["usd"])
    except Exception:
        return 0.0


def get_wallet_balance_eth() -> float:
    """Retorna balance ETH del owner wallet en Arbitrum One. -1 si falla."""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://arb1.arbitrum.io/rpc"))
        bal = w3.eth.get_balance(OWNER_WALLET)
        return float(Web3.from_wei(bal, "ether"))
    except Exception as e:
        log(f"wallet balance error: {e}")
        return -1.0


def check_wallet_balance(state: dict) -> float:
    """Chequea balance y alerta si baja del umbral. Corre cada ciclo."""
    balance = get_wallet_balance_eth()
    if balance < 0:
        return balance
    state["wallet_eth"] = round(balance, 6)
    if balance < WALLET_LOW_ETH:
        msg = f"[pioneer] ALERTA WALLET — balance bajo: {balance:.4f} ETH (mínimo: {WALLET_LOW_ETH} ETH)"
        tg_send(msg)
        log(msg)
    return balance


def check_market_movement(state: dict) -> float:
    """Retorna delta % del precio ETH desde el último ciclo."""
    price_now = get_eth_price()
    if price_now == 0:
        return 0.0

    price_last = state.get("eth_price_last", 0.0)
    state["eth_price_last"] = price_now

    if price_last == 0:
        return 0.0

    delta_pct = abs((price_now - price_last) / price_last) * 100
    log(f"ETH price: ${price_now:.0f} | delta: {delta_pct:.2f}%")
    return delta_pct


def trigger_arb(delta_pct: float):
    """Notifica al arb monitor que hay movimiento de precio."""
    try:
        r = requests.post(
            "http://localhost:8020/trigger",
            json={"delta_pct": delta_pct, "source": "pioneer"},
            timeout=5
        )
        if r.status_code == 200:
            log(f"arb trigger enviado | delta: {delta_pct:.2f}%")
    except Exception as e:
        log(f"arb trigger error: {e}")


# ── Release monitor ────────────────────────────────────────────────────────

def check_releases(state):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    for watch in RELEASE_WATCH:
        repo = watch["repo"]
        label = watch["label"]
        target = watch["target_version"]
        key = f"release:{repo}"
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            release = r.json()
            tag = release.get("tag_name", "")
            if tag == state.get(key):
                continue
            state[key] = tag
            msg = f"[pioneer] RELEASE — {label}\nNueva versión: {tag}\n{release.get('html_url','')}"
            if tag == target or (target in tag):
                msg = f"[pioneer] 🎯 {label} {tag} — OBJETIVO ALCANZADO\n{release.get('html_url','')}\nListo para correr harness v3.8.0 contra Giskard."
            tg_send(msg)
            store_memory(f"Release detected: {label} {tag}")
            log(f"New release: {label} {tag}")
        except Exception as e:
            log(f"release check error {repo}: {e}")


# ── Stats update ───────────────────────────────────────────────────────────

STATS_FILE = Path("/tmp/giskard-status/stats.json")

def get_memory_count() -> int:
    try:
        import chromadb
        client_db = chromadb.PersistentClient(path="/home/dell7568/mcp-memory/memory_db")
        return sum(client_db.get_collection(c.name).count() for c in client_db.list_collections())
    except Exception:
        return 0

def get_marks_count() -> tuple:
    try:
        r = httpx.get(f"{BASE}:8015/leaderboard", timeout=5)
        lb = r.json().get("leaderboard", [])
        total = sum(e.get("total", 0) for e in lb)
        agents = len(lb)
        marks = []
        for e in lb:
            for m in e.get("marks", []):
                marks.append(m)
        return total, agents, marks
    except Exception:
        return 0, 0, []

def _trigger_oasis_trail() -> dict | None:
    """Llama POST /agent/trail en oasis con firma Ed25519 de pioneer.
    Genera un trail real con bridge_tx_hash propio en Basescan.
    Retorna el response dict o None si falla.
    """
    import base64
    import json as _json
    import uuid
    import sys as _sys
    _sys.path.insert(0, "/home/dell7568/mcp-oasis")
    try:
        from agent_signing import sign_request, build_payload
        from nacl.signing import SigningKey as _SK
    except ImportError as e:
        log(f"oasis trail: missing dep {e}")
        return None

    sk_b64 = PIONEER_SIGNING_KEY
    if not sk_b64:
        log("oasis trail: PIONEER_SIGNING_KEY not set")
        return None

    ts = int(__import__("time").time())
    nonce = str(uuid.uuid4())
    try:
        signature = sign_request(sk_b64, AGENT_ID, ts, nonce)
    except Exception as e:
        log(f"oasis trail: sign error {e}")
        return None

    try:
        resp = httpx.post(
            f"{OASIS_URL}/agent/trail",
            json={
                "agent_id": AGENT_ID,
                "signature": signature,
                "timestamp": ts,
                "nonce": nonce,
                "state": f"pioneer daily cycle {__import__('datetime').datetime.utcnow().date().isoformat()}",
            },
            timeout=120,
        )
        data = resp.json()
        if resp.status_code == 200:
            log(f"oasis trail OK: tx={data.get('bridge_tx_hash','?')[:16]}... status={data.get('bridge_status')}")
        else:
            log(f"oasis trail error {resp.status_code}: {data}")
        return data
    except Exception as e:
        log(f"oasis trail: request error {e}")
        return None


def update_stats(services_ok: int):
    try:
        memories = get_memory_count()
        marks_total, agents, _ = get_marks_count()

        # Load existing stats to preserve marks list
        existing = {}
        if STATS_FILE.exists():
            with open(STATS_FILE) as f:
                existing = json.load(f)

        existing.update({
            "memories": memories,
            "marks_total": marks_total,
            "agents": agents,
            "services_ok": services_ok,
            "updated": datetime.now().strftime("%Y-%m-%d"),
        })

        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATS_FILE, "w") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        log(f"stats.json updated: memories={memories}, marks={marks_total}, agents={agents}")
    except Exception as e:
        log(f"update_stats error: {e}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    log("pioneer-agent-001 cycle start")
    state = load_state()

    # Detección de movimiento de mercado
    delta_pct = check_market_movement(state)
    if delta_pct >= 2.0:
        log(f"MOVIMIENTO DETECTADO: {delta_pct:.2f}% — triggering arb")
        tg_send(f"[pioneer] ETH movió {delta_pct:.2f}% — escaneando oportunidades")
        trigger_arb(delta_pct)

    # Wallet balance — alerta si baja del umbral
    check_wallet_balance(state)

    # Health check silencioso — alertar solo si algo cayó
    services = check_services()
    down = [s for s in services if s["status"] != "OK"]
    if down:
        msg = "[pioneer] ALERTA — servicios caídos: " + ", ".join(
            f"{s['name']} (:{s['port']})" for s in down)
        tg_send(msg)
        log(msg)

    # Releases
    check_releases(state)

    # GitHub mentions sin respuesta → pizarra.txt
    check_github_mentions(state)

    # GitHub
    github_alerts = check_github(state)

    # Moltbook — replies a nuestros posts
    moltbook_alerts = check_moltbook(state)

    # Moltbook — rastrillaje de comunidad (una vez por día)
    scan_moltbook_community(state)

    # Stacker News
    stacker_alerts = check_stacker(state)

    # Procesar
    all_alerts = github_alerts + moltbook_alerts + stacker_alerts
    if all_alerts:
        process_alerts(all_alerts, state)

    # Guardar memoria de cada ciclo (actividad real del agente)
    services_ok = len([s for s in services if s["status"] == "OK"])
    wallet_eth = state.get("wallet_eth", -1)
    wallet_str = f"{wallet_eth:.4f}" if wallet_eth >= 0 else "N/A"
    cycle_summary = (
        f"pioneer-agent-001 cycle. services: {services_ok}/{len(services)}. "
        f"alerts: {len(all_alerts)}. "
        f"github_watched: {len(GITHUB_WATCH)}. "
        f"wallet: {wallet_str} ETH. "
        f"ts: {datetime.now().isoformat()}"
    )
    store_memory(cycle_summary)
    log(f"Memory stored: {cycle_summary[:80]}...")

    # Actualizar stats.json local para el dashboard
    update_stats(services_ok)

    # Reporte diario + trail on-chain propio (una vez por día)
    if should_send_daily(state):
        send_daily_report(state)
        try:
            _trigger_oasis_trail()
        except Exception as e:
            log(f"oasis trail exception: {e}")

    # Reporte semanal — lunes 09:xx Bs As (C2 + weekly digest)
    if should_send_weekly(state):
        log("weekly hook fired — running competitive intel scan + digest")
        try:
            report = competitive_intel_scan(state)
            send_competitive_report(report)
        except Exception as e:
            log(f"competitive_intel_scan error: {e}")
        try:
            weekly_digest(state)
        except Exception as e:
            log(f"weekly_digest error: {e}")

    save_state(state)
    log("pioneer-agent-001 cycle done")


if __name__ == "__main__":
    main()
