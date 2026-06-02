# URL Shortener

A lightweight URL shortening service built with Python (Flask), SQLite, and Redis. The project is a hands-on implementation of several core distributed-systems design patterns - counter-based ID generation, Base62 encoding, and a Cache-Aside caching strategy.

---

## Table of Contents

- [Quick Start](#quick-start)
- [System Design](#system-design)
  - [Architecture Overview](#architecture-overview)
  - [ID Generation: Counter + Base62 Encoding](#id-generation-counter--base62-encoding)
  - [Storage Layer: SQLite](#storage-layer-sqlite)
  - [Caching Layer: Redis (Cache-Aside)](#caching-layer-redis-cache-aside)
  - [Request Flows](#request-flows)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Design Trade-offs & Limitations](#design-trade-offs--limitations)

---

## Quick Start

**Prerequisites:** Python 3.10+, Redis running on `localhost:6379`

```bash
# Install dependencies
pip install -r requirements.txt

# Start the Flask server
python app.py

# In a separate terminal, use the CLI client
python client.py
```

---

## System Design

### Architecture Overview

```
┌────────────┐      POST /shorten       ┌─────────────────┐
│   Client   │ ───────────────────────► │   Flask App     │
│ (CLI/HTTP) │                          │   (app.py)      │
│            │ ◄─── short_url ────────  │                 │
│            │                          └────────┬────────┘
│            │      GET /<short_id>              │
│            │ ───────────────────────►          │
│            │ ◄─── 302 Redirect ─────  ┌────────▼────────┐      ┌─────────────────┐
└────────────┘                          │  Redis Cache    │      │  SQLite DB      │
                                        │  (hot URLs)     │      │  (source of     │
                                        │  TTL: 24hrs     │      │   truth)        │
                                        └─────────────────┘      └─────────────────┘
```

The service has two layers of storage with clearly separated responsibilities:

| Layer | Technology | Role |
|---|---|---|
| Primary DB | SQLite (`urls.db`) | Source of truth - persists all URL mappings |
| Cache | Redis | Serves hot (frequently accessed) URLs at low latency |

---

### ID Generation: Counter + Base62 Encoding

**The Problem:** Short URLs need a short, unique, URL-safe identifier for every long URL stored.

**The Approach:** SQLite's `AUTOINCREMENT` on the `id` column acts as a global monotonic counter. Each new URL gets an integer ID (1, 2, 3, ...). That integer is then encoded into Base62.

**Why Base62?**

Base62 uses `[a-z A-Z 0-9]` - 62 characters - all of which are URL-safe with no encoding overhead. This is a deliberate choice over alternatives:

| Scheme | Characters | URL-safe? | Notes |
|---|---|---|---|
| Base64 | 64 | ✗ | `+` and `/` need URL-encoding |
| Base62 | 62 | ✓ | Clean, compact, no special chars |
| MD5/SHA hash | hex | ✓ | Collision risk; not sequential |
| UUID | alphanumeric | ✓ | 36 chars - too long |

**Capacity:** A 6-character Base62 string can represent 62⁶ ≈ **56 billion** unique URLs, which is comparable to production systems like bit.ly.

**Implementation** (`base62.py`):
```python
# encode: integer → short string
# e.g., 12345 → "dnh"
def encode(num: int) -> str: ...

# decode: short string → integer (for DB lookup)
def decode(short_url: str) -> int: ...
```

The encode/decode pair is the only bridge between the URL-facing identifier and the database row - no separate mapping table is needed.

---

### Storage Layer: SQLite

The `urls` table is the single source of truth:

```sql
CREATE TABLE IF NOT EXISTS urls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    long_url    TEXT NOT NULL,
    clicks      INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

Key decisions:
- `AUTOINCREMENT` on `id` drives the ID generation strategy described above. The integer ID is the only thing that needs to be stored - the short code is derived from it on the fly.
- `clicks` and `created_at` are included for future analytics, though click tracking is currently handled in Redis (see below).
- Each request opens and closes its own connection (`with sqlite3.connect(...)`), keeping the code simple at the cost of connection-pool efficiency.

---

### Caching Layer: Redis (Cache-Aside)

Redis serves as a read-through cache for the redirect hot path, using the **Cache-Aside (Lazy Loading)** pattern.

**Why Cache-Aside and not Write-Through?**

With write-through caching, every new URL created would also be written into Redis immediately. This is wasteful: a link that is created but never clicked wastes Redis RAM. Cache-Aside defers the cache population to the first actual access.

```
Write path (POST /shorten):
  1. Insert into SQLite → get integer ID
  2. Encode to Base62 → short_id
  3. Return short_url
  ✗  Do NOT write to Redis yet

Read path (GET /<short_id>):
  1. Check Redis for short_id
     ├─ HIT  → redirect immediately (fast path)
     └─ MISS → decode short_id → query SQLite → populate Redis → redirect
```

**TTL (Time-To-Live):** Every key written to Redis has a 24-hour expiry (`ex=86400`). This provides automatic cache invalidation without needing explicit eviction logic. Frequently accessed URLs will be re-cached on the next cache miss; cold URLs expire naturally.

**Click Tracking:** Redis also tracks redirect counts using atomic increments:
```python
redis_client.incr(f"clicks:{short_id}")  # incremented on every redirect, cache hit or miss
```

This offloads high-frequency write operations from SQLite to Redis, avoiding row-level locking on the `clicks` column for every redirect.

---

### Request Flows

**Shorten a URL (`POST /shorten`)**

```
Client → POST /shorten { "long_url": "https://..." }
       → Insert into SQLite
       → Get autoincrement ID (e.g., 42)
       → Base62 encode: 42 → "G"
       → Return { "short_url": "http://localhost:5000/G" }
```

**Redirect (`GET /<short_id>`)**

```
Client → GET /G

  [Cache Hit]
  → Redis GET "G" → "https://..."
  → Redis INCR "clicks:G"
  → 302 Redirect

  [Cache Miss]
  → Redis GET "G" → nil
  → Base62 decode: "G" → 42
  → SQLite SELECT WHERE id = 42 → "https://..."
  → Redis SET "G" "https://..." EX 86400
  → Redis INCR "clicks:G"
  → 302 Redirect
```

---

## API Reference

### `POST /shorten`

Shortens a long URL.

**Request body:**
```json
{ "long_url": "https://example.com/some/very/long/path" }
```

**Response (`201 Created`):**
```json
{ "short_url": "http://localhost:5000/dnh" }
```

**Error (`400 Bad Request`):**
```json
{ "error": "missing url" }
```

---

### `GET /<short_id>`

Redirects to the original URL.

**Response:** `302 Found` with `Location` header set to the original URL.

**Errors:**
- `400` - short_id contains characters outside the Base62 alphabet
- `404` - short_id not found in the database

---

## Project Structure

```
url-shortener/
├── app.py          # Flask routes: /shorten and /<short_id>
├── base62.py       # Stateless encode/decode functions
├── database.py     # SQLite wrapper (URLDatabase class)
├── client.py       # Interactive CLI for testing the API
├── requirements.txt
└── urls.db         # SQLite database file (auto-created on first run)
```

---

## Design Trade-offs & Limitations

| Area | Current Design | Production Consideration |
|---|---|---|
| **Database** | SQLite (single-file, single-writer) | Replace with PostgreSQL/MySQL for concurrent writes |
| **ID generation** | DB `AUTOINCREMENT` (single node) | Distributed systems need a dedicated ID service (e.g., Twitter Snowflake) to avoid counter contention |
| **Cache** | Redis on localhost | Redis Cluster or a managed cache (ElastiCache) for HA |
| **Click tracking** | Redis counter only; not persisted to SQLite | Periodic flush from Redis → DB for durable analytics |
| **URL validation** | None beyond null check | Add URL format validation and optional reachability check |
| **Collisions** | N/A - counter is monotonic, no collision possible | Only relevant if switching to hash-based ID generation |
| **Security** | No rate limiting or auth | Add rate limiting on `/shorten` to prevent abuse |