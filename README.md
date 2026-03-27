# pioneer-agent-001

The first autonomous agent built on the Giskard ecosystem.

pioneer-agent-001 is not a demo. It runs every 30 minutes as a systemd service, monitors the Giskard stack, reports to Telegram, and stores everything it learns in Giskard Memory.

## What it does

- Health checks all Giskard services every 30 minutes
- Monitors GitHub (PRs, issues, new contacts)
- Monitors Moltbook (replies, mentions)
- Classifies incoming signals: spam / relevant / urgent
- Drafts responses and sends them to Telegram for human approval
- Stores decisions and observations in Giskard Memory
- Generates a daily report at 9:00 AM

## Identity

- **agent_id:** `pioneer-agent-001`
- **Model:** claude-haiku-4-5-20251001
- **GENESIS mark:** `b5f3a03c-1f6b-43a3-b4a0-664a18040548` — Arbitrum One
- **Verify:** `GET http://localhost:8015/verify/pioneer-agent-001`

The GENESIS mark is permanent proof that this agent existed and acted — recorded on-chain on 2026-03-25.

## Built on Giskard

pioneer-agent-001 uses the full Giskard stack:

| Service | Purpose |
|---|---|
| [giskard-memory](https://github.com/giskard09/giskard-memory) | Stores decisions and observations between sessions |
| [giskard-marks](https://github.com/giskard09/giskard-marks) | On-chain identity proof (GENESIS mark) |
| [giskard-search](https://github.com/giskard09/giskard-search) | Discovery and search |
| [giskard-origin](https://github.com/giskard09/mcp-origin) | Orientation for new agents |

This is what Giskard was built for: an agent that wakes up, acts, saves what it learned, and returns knowing where it left off.

## Decision logging

pioneer stores not just what happened, but why decisions were made:

```python
store_decision(
    problem="Privacy of external agent memories in Giskard Memory",
    options=["Off-chain encryption", "On-chain encrypted blobs", "No encryption"],
    chosen="Off-chain AES-256-GCM + X25519",
    reason="Same privacy as on-chain, zero cost, millisecond latency",
    discarded={
        "On-chain storage": "Cost $0.01-0.05/memory, 2-10s latency, no privacy improvement",
        "No encryption": "Giskard can read everything — unacceptable for external agents"
    }
)
```

## Part of the Giskard ecosystem

```
github.com/giskard09/giskard-memory    ← memory layer
github.com/giskard09/giskard-marks     ← identity layer
github.com/giskard09/argentum-core     ← reputation layer
github.com/giskard09/pioneer-agent     ← first agent using all three
```

The agent does not just call tools. It remembers, proves it existed, and earns trust over time.

## License

Apache 2.0
