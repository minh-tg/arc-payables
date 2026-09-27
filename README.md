# Arc Canteen Hackathon Workspace

Reproducible development workspace for building on **Arc** (Circle's L1 EVM blockchain where native gas is USDC) using **Canteen** tooling, Foundry smart contracts, Node.js, and Python agent integrations.

This repo is configured for zero system pollution: all hackathon tools (including `arc-canteen`) run isolated inside the local project environment (`.venv`), so removing this directory leaves no lingering binaries on your machine.

---

## 🚀 Quickstart

### Option A: Using Nix (Recommended for Nix users)

If you have Nix with flakes enabled:

```bash
# If you use direnv (automatically enters the shell):
direnv allow

# Or enter the devShell manually:
nix develop
```

This brings in `uv`, `python3`, `nodejs_22`, `pnpm`, `foundry` (`forge`/`cast`), `solc`, `jq`, and `curl` without installing anything into your global system profile.

Then sync the local tools:
```bash
uv sync
```

---

### Option B: Without Nix (Standard / Cross-platform)

Prerequisites:
- [uv](https://docs.astral.sh/uv/) (fast Python package manager)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`curl -L https://foundry.paradigm.xyz | bash`)
- [Node.js](https://nodejs.org/) (v20+)

Set up the project environment:
```bash
# Create local virtual environment and install arc-canteen inside .venv
uv sync

# (Optional) activate the project virtual environment
source .venv/bin/activate
```

---

## 🛠️ Working with `arc-canteen` (Zero Host Pollution)

Instead of running `uv tool install arc-canteen` (which would pollute `~/.local/bin`), run `arc-canteen` directly through the project's local virtualenv:

| Task | Command |
|---|---|
| **Login with GitHub** | `uv run arc-canteen login` |
| **View funded testnet wallet** | `uv run arc-canteen wallet` |
| **Get personal RPC endpoint** | `uv run arc-canteen rpc-url` |
| **Check testnet block number** | `uv run arc-canteen rpc eth_blockNumber` |
| **Export agent context / docs** | `uv run arc-canteen context` |
| **Sync Arc + Circle documentation** | `uv run arc-canteen context sync` |
| **Project status dashboard** | `uv run arc-canteen status` |

*(If you activated `.venv/bin/activate` or use `direnv`, you can call `arc-canteen` directly without `uv run`.)*

Alternatively, you can run any command ephemerally via uv cache without keeping even a virtual environment:
```bash
uvx arc-canteen <command>
```

---

## ⛓️ Arc Testnet Configuration

Arc is Circle's L1 EVM network where native gas is paid in USDC.

- **Chain ID**: `5042002` (`0x4cef52`)
- **Native Gas Token**: `USDC`
- **Block Explorer**: [testnet.arcscan.app](https://testnet.arcscan.app)
- **Public RPC**: `https://testnet-rpc.arc.network` (or use your Canteen tokenized RPC via `uv run arc-canteen rpc-url`)

### Environment Setup

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Populate `RPC_URL` and `PRIVATE_KEY` using your Canteen wallet:
```bash
# Get your RPC URL
uv run arc-canteen rpc-url

# Get your private key & testnet address
uv run arc-canteen wallet
```

---

## 🔨 Smart Contract Workflow (Foundry)

### Compile & Test
```bash
forge build
forge test
```

### Deploy to Arc Testnet
```bash
# Deploy Counter contract
forge create src/Counter.sol:Counter \
  --rpc-url $(uv run arc-canteen rpc-url) \
  --private-key <YOUR_PRIVATE_KEY>
```

---

## 🤖 Agent Context Integration

Feed Arc + Circle developer context into your coding agent:

```bash
# Display AGENTS.md + doc file paths
uv run arc-canteen context

# Clone / update local reference docs into ~/.arc-canteen/context/
uv run arc-canteen context sync
```
