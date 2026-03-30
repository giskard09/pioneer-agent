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
]

OWNER_WALLET   = "0xDcc84E9798E8eB1b1b48A31B8f35e5AA7b83DBF4"
WALLET_LOW_ETH = 0.015  # alerta si baja de este umbral

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── State ──────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "github_seen": {},     # repo+number+comment_id -> True
        "moltbook_seen": {},   # post_id+comment_id -> True
        "stacker_seen": {},    # item_id+comment_id -> True
        "last_daily": None,
        "pending_drafts": {},  # draft_id -> {content, context, channel}
    }

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

Reply with exactly one word: SPAM, RELEVANT, or URGENT."""

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

INSIGHT_SYSTEM = """You are pioneer-agent-001, scanning the Moltbook agent community for useful content.

We are building Giskard: MCP servers for agents (search, memory, identity, payments via Lightning/Arbitrum).
We want to learn from the broader agent community.

Flag as INSIGHT if the post contains ANY of:
- A real project/tool/protocol that agents are using for payments, memory, identity, or coordination
- A critique or problem with current agent infrastructure (even if not about Giskard)
- An architectural pattern or approach worth knowing
- A concrete experiment with results (even partial)
- Something that could compete with or complement our stack

Flag as NOISE if: pure marketing fluff, no technical substance, generic AI hype, or spam.

Reply with exactly:
NOISE
or
INSIGHT: <one-line concrete takeaway for our stack>"""

INSIGHT_KEYWORDS = [
    "mcp", "memory", "lightning", "payment", "sats", "reputation", "karma",
    "attestation", "identity", "agent-to-agent", "zk", "proof", "protocol",
    "sdk", "token", "stake", "sybil", "coordination", "registry", "mark",
    "embedding", "episodic", "semantic", "inference cost", "paywall",
    "autonomy", "economic", "earn", "spend", "nara", "l402", "x402",
    "trust", "credential", "did", "verifiable", "wallet", "arbitrum",
]

def _keyword_insight(title: str, content: str):
    """Pre-filtro por keywords. Retorna (matched, keywords_found)."""
    text = (title + " " + content).lower()
    found = [kw for kw in INSIGHT_KEYWORDS if kw in text]
    return len(found) >= 2, found


def classify_insight(title: str, content: str) -> str:
    """
    Clasifica un post de la comunidad. Retorna 'NOISE' o 'INSIGHT: ...'
    Usa keyword pre-filter para reducir llamadas a Haiku.
    Si Haiku falla, confía en el keyword filter.
    """
    matched, keywords = _keyword_insight(title, content)
    if not matched:
        return "NOISE"

    # Keyword match — intentar con Haiku para un resumen preciso
    try:
        text = f"Title: {title}\n\nContent: {content[:1200]}"
        r = client.messages.create(
            model=MODEL,
            max_tokens=60,
            system=INSIGHT_SYSTEM,
            messages=[{"role": "user", "content": text}]
        )
        return r.content[0].text.strip()
    except Exception as e:
        log(f"classify_insight error (Haiku): {e} — usando keyword fallback")
        # Haiku no disponible: si hay keywords relevantes, es insight
        return f"INSIGHT: keywords detectados: {', '.join(keywords[:4])}"


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
    Rastrilla submolts de Moltbook buscando insights para el stack Giskard.
    Corre una vez por día. Manda digest a Telegram y guarda en memoria.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("community_scan_last") == today:
        return  # ya corrió hoy

    headers = {"Authorization": f"Bearer {MOLTBOOK_KEY}"}
    if "community_seen" not in state:
        state["community_seen"] = {}

    insights = []

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
                pid   = post.get("id", "")
                if pid in state["community_seen"]:
                    continue
                state["community_seen"][pid] = today

                # Ignorar propios y spam
                author_name = post.get("author", {}).get("name", "")
                if author_name in ("giskardmcp",) or post.get("is_spam"):
                    continue

                title   = post.get("title", "")
                content = post.get("content", "")
                result  = classify_insight(title, content)
                log(f"community [{submolt}] @{author_name}: {result[:60]}")

                if result.startswith("INSIGHT:"):
                    takeaway = result[8:].strip()
                    insights.append({
                        "submolt":  submolt,
                        "author":   author_name,
                        "title":    title[:80],
                        "takeaway": takeaway,
                        "post_id":  pid,
                        "url":      f"https://www.moltbook.com/p/{pid}",
                        "upvotes":  post.get("upvotes", 0),
                    })
                    store_memory(
                        f"community_insight [{submolt}] @{author_name}: {takeaway} — post: {pid}"
                    )

        except Exception as e:
            log(f"community scan error [{submolt}]: {e}")

    # Limpiar seen antiguo (mantener solo últimos 500)
    if len(state["community_seen"]) > 500:
        keys = list(state["community_seen"].keys())
        state["community_seen"] = {k: state["community_seen"][k] for k in keys[-500:]}

    state["community_scan_last"] = today

    # Digest a Telegram
    if insights:
        lines = [f"[pioneer] COMMUNITY SCAN — {len(insights)} insight(s) encontrados\n"]
        for i, ins in enumerate(insights[:10], 1):
            lines.append(
                f"{i}. @{ins['author']} en /{ins['submolt']} ({ins['upvotes']} upvotes)\n"
                f"   \"{ins['title']}\"\n"
                f"   => {ins['takeaway']}\n"
                f"   {ins['url']}"
            )
        tg_send("\n".join(lines))
        log(f"community scan done: {len(insights)} insights sent to Telegram")
    else:
        log("community scan done: 0 insights — all noise today")


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
            urgency = "URGENTE" if classification == "URGENT" else "NUEVO"
            msg = (
                f"[pioneer] {lab_tag}{urgency} — GitHub {label}\n"
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
            urgency = "URGENTE" if classification == "URGENT" else "NUEVO"
            # Contactos estratégicos → tag LAB
            lab_contacts = {"oceantiger", "feri-sanyi", "feri-sanyi-agent", "msaleme",
                            "petchevere", "fransdev", "francesdevelopment"}
            lab_tag = "[LAB] " if author.lower() in lab_contacts else ""
            msg = (
                f"[pioneer] {lab_tag}{urgency} — Moltbook\n"
                f"Post: {alert['post_title']}\n"
                f"@{author}: {body}"
            )
            tg_send(msg)
            if lab_tag:
                store_memory(f"LAB contact activity — @{author} en Moltbook: {body[:200]}")

        elif src == "stacker":
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


def trigger_liquidator(delta_pct: float):
    """Notifica al liquidator cuando hay caída brusca de precio."""
    try:
        r = requests.post(
            "http://localhost:8021/trigger_liq",
            json={"delta_pct": delta_pct, "source": "pioneer"},
            timeout=5
        )
        if r.status_code == 200:
            log(f"liq trigger enviado | delta: {delta_pct:.2f}%")
    except Exception as e:
        log(f"liq trigger error: {e}")


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
        log(f"MOVIMIENTO DETECTADO: {delta_pct:.2f}% — triggering arb + liquidator")
        tg_send(f"[pioneer] ETH movió {delta_pct:.2f}% — escaneando oportunidades")
        trigger_arb(delta_pct)
        # En caídas, las liquidaciones son más probables
        if delta_pct < 0 or True:  # siempre disparar liquidator en movimiento >= 2%
            trigger_liquidator(delta_pct)

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

    # Reporte diario
    if should_send_daily(state):
        send_daily_report(state)

    save_state(state)
    log("pioneer-agent-001 cycle done")


if __name__ == "__main__":
    main()
