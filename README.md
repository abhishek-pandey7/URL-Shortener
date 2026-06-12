# URL Shortener

A production-patterned URL shortening service built with Python (Flask), PostgreSQL, and Redis. The project is a ground-up implementation of several core distributed systems design patterns: counter-based ID generation, Base62 encoding, Cache-Aside lazy loading, SSRF-resistant URL validation, and Redis-backed rate limiting.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Quick Start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Running the Service](#running-the-service)
- [Project Structure](#project-structure)
- [System Design](#system-design)
  - [ID Generation: Counter + Base62 Encoding](#id-generation-counter--base62-encoding)
  - [Storage Layer: PostgreSQL](#storage-layer-postgresql)
  - [Caching Layer: Redis and Cache-Aside](#caching-layer-redis-and-cache-aside)
  - [Rate Limiting](#rate-limiting)
  - [URL Validation and SSRF Prevention](#url-validation-and-ssrf-prevention)
  - [Request Flows](#request-flows)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [CLI Client](#cli-client)
- [Design Trade-offs and Limitations](#design-trade-offs-and-limitations)

---

## Architecture Overview

```
+------------------+      POST /shorten       +------------------+
|   Client         | -----------------------> |   Flask App      |
|   (CLI / HTTP)   |                          |   (app.py)       |
|                  | <--- short_url --------- |                  |
|                  |                          +--------+---------+
|                  |      GET /<short_id>              |
|                  | -----------------------> +--------v---------+
|                  | <--- 302 Redirect -----  |  Redis (Cache)   |
+------------------+        (cache hit)       |  TTL: 24 hours   |
                                              +--------+---------+
                                                       |
                                              (cache miss only)
                                                       |
                                              +--------v---------+
                                              |  PostgreSQL DB   |
                                              |  (source of      |
                                              |   truth)         |
                                              +------------------+
```

The service is split into two storage layers with clearly separated responsibilities:

| Layer       | Technology | Role                                                       |
|-------------|------------|------------------------------------------------------------|
| Primary DB  | PostgreSQL | Source of truth. Persists all URL mappings durably.        |
| Cache       | Redis      | Serves hot (frequently accessed) URLs at low latency.      |
| Rate Limit  | Redis (db 1) | Per-IP fixed-window rate limiting on the write endpoint. |

---

## Quick Start

### Prerequisites

- Python 3.10 or higher
- PostgreSQL running and accessible
- Redis running on `localhost:6379`

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd url-shortener

# Install Python dependencies
pip install -r requirements.txt

# Copy the environment template and fill in your credentials
cp .env.example .env
```

Edit `.env` with your PostgreSQL connection string:

```
DATABASE_URL=postgresql://postgres:your_password@localhost:5432/url-shortener
```

The database table is created automatically on first run via `URLDatabase._init_db()`. No migration step is required.

### Running the Service

Start the Flask server:

```bash
python app.py
```

The server starts on `http://127.0.0.1:5000` in debug mode by default.

In a separate terminal, use the interactive CLI client to shorten URLs:

```bash
python client.py
```

Or make raw HTTP requests directly:

```bash
# Shorten a URL
curl -X POST http://127.0.0.1:5000/shorten \
  -H "Content-Type: application/json" \
  -d '{"long_url": "https://example.com/some/very/long/path"}'

# Follow a short URL
curl -L http://127.0.0.1:5000/dnh
```

---

## Project Structure

```
url-shortener/
├── app.py            # Flask application: route handlers, Redis client, rate limiter, URL validator
├── base62.py         # Stateless Base62 encode / decode functions
├── database.py       # PostgreSQL wrapper class (URLDatabase)
├── client.py         # Interactive CLI for testing the /shorten endpoint
├── requirements.txt  # Python dependencies
├── .env.example      # Environment variable template
└── .env              # Local credentials (not committed)
```

### Module responsibilities

`app.py` is the entry point and wires everything together. It owns the Flask application instance, the Redis client (db 0 for URL cache, db 1 for rate limiting), the `Limiter` instance, the `is_valid_url` validation function, and the two route handlers (`/shorten` and `/<short_id>`).

`database.py` encapsulates all PostgreSQL interaction behind a `URLDatabase` class. It creates the table schema on instantiation, provides `insert(long_url)` which returns the new row's integer ID, and `get_by_id(db_id)` for lookups. Each method opens and closes its own connection using `psycopg2`'s context manager protocol.

`base62.py` is a pure, stateless module with no dependencies on the rest of the application. It holds the 62-character alphabet (`a-z`, `A-Z`, `0-9`) and implements `encode(int) -> str` and `decode(str) -> int` via standard positional-value arithmetic. It raises `ValueError` on negative input to `encode`, and `alphabet.index` implicitly raises `ValueError` on unknown characters in `decode`, which `app.py` catches to return a 400.

`client.py` is a thin REPL loop that POSTs to `http://127.0.0.1:5000/shorten` and prints the resulting short URL. It handles 429 rate limit responses explicitly and catches `ConnectionError` for when the server is not running.

---

## System Design

### ID Generation: Counter + Base62 Encoding

Every short URL is derived from a unique integer ID, not from a hash of the long URL.

PostgreSQL's `SERIAL` type on the `id` column acts as a global monotonic counter. Each insert returns a new integer (1, 2, 3, ...). That integer is then Base62-encoded to produce the short code.

**Why not hashing?**

Hash-based approaches (MD5, SHA, or random UUIDs) introduce collision risk and require a separate mapping table to store the hash-to-ID correspondence. With a counter, the short code *is* the row address — `decode(short_id)` gives you the exact `id` to `SELECT` on. No separate lookup table is needed, and collisions are structurally impossible.

**Why Base62?**

Base62 uses only characters from `[a-z A-Z 0-9]`, all of which are URL-safe without percent-encoding. This is a deliberate choice over alternatives:

| Encoding | Characters | URL-safe | Notes                                        |
|----------|------------|----------|----------------------------------------------|
| Base64   | 64         | No       | `+` and `/` must be percent-encoded in URLs  |
| Base62   | 62         | Yes      | Clean, compact, no special characters        |
| Hex      | 16         | Yes      | Requires 2x more characters for same range   |
| UUID v4  | 36 chars   | Yes      | Far too long for a short URL                 |

**Capacity:** A 6-character Base62 string encodes 62^6 = approximately 56.8 billion unique values, which is comparable to what production systems like bit.ly operate at. With 7 characters the space grows to roughly 3.5 trillion.

**Implementation** (`base62.py`):

```python
alphabet = string.ascii_letters + string.digits  # 62 characters
BASE = 62

def encode(num: int) -> str:
    # Repeated modulo: build digits in reverse, then join
    result = []
    while num > 0:
        result.append(alphabet[num % BASE])
        num //= BASE
    return "".join(reversed(result))

def decode(short_url: str) -> int:
    # Positional value: left-to-right accumulation
    num = 0
    for char in short_url:
        num = num * BASE + alphabet.index(char)
    return num
```

The edge case where `num == 0` is handled explicitly — `encode(0)` returns `alphabet[0]` (`'a'`) rather than an empty string.

---

### Storage Layer: PostgreSQL

The `urls` table is the single source of truth for all URL mappings:

```sql
CREATE TABLE IF NOT EXISTS urls (
    id         SERIAL PRIMARY KEY,
    long_url   TEXT NOT NULL,
    clicks     INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Key decisions:

`SERIAL PRIMARY KEY` provides the auto-incrementing integer counter that drives the entire ID generation strategy. The counter is monotonically increasing and managed entirely by PostgreSQL, which makes it safe under concurrent inserts without application-level coordination.

`clicks` and `created_at` columns are included for future analytics. In the current implementation, click counting is handled exclusively in Redis (via `INCR`) to avoid high-frequency row-level writes to PostgreSQL on every redirect. If durable click statistics are needed, a periodic flush job can copy Redis counters back to the `clicks` column.

`long_url` is stored as `TEXT` rather than `VARCHAR(n)` because URL length is variable and PostgreSQL's `TEXT` type has no practical length overhead versus `VARCHAR` on modern Postgres.

The `URLDatabase` class opens a fresh connection per request using `psycopg2.connect()` within a `with` block. This is simple and correct for a single-instance service but does not pool connections. Under concurrent load, this causes connection setup overhead on every request. The production path forward is replacing per-request connections with a connection pool (e.g., `psycopg2.pool.ThreadedConnectionPool` or the `psycopg3` async driver with a pool).

---

### Caching Layer: Redis and Cache-Aside

Redis sits in front of PostgreSQL on the redirect hot path and uses the Cache-Aside (Lazy Loading) pattern.

**Cache-Aside vs Write-Through**

The alternative to Cache-Aside is Write-Through caching, where every call to `POST /shorten` immediately writes the new mapping into Redis as well as PostgreSQL. Write-Through guarantees that the cache is always warm for any URL that has ever been created.

Cache-Aside was chosen instead because the access pattern for a URL shortener is heavily skewed: a large fraction of links are created but rarely or never clicked. Populating Redis on write would waste memory holding cold entries indefinitely. Cache-Aside defers the cache entry to the first actual redirect, ensuring only accessed URLs consume cache memory.

**Write path** (`POST /shorten`):

```
1. INSERT INTO urls (long_url) -> returns integer ID
2. Base62 encode ID -> short_id
3. Return short_url to caller
   (Redis is NOT written)
```

**Read path** (`GET /<short_id>`):

```
1. Redis GET short_id
   |- HIT:  Redis INCR clicks:{short_id}
   |        Return 302 Redirect (fast path, no DB)
   |
   `- MISS: Base62 decode short_id -> integer ID
            PostgreSQL SELECT WHERE id = integer_id
            Redis SET short_id long_url EX 86400
            Redis INCR clicks:{short_id}
            Return 302 Redirect
```

**TTL:** Every key written to Redis carries a 24-hour expiry (`EX 86400`). This provides automatic eviction for cold URLs without explicit invalidation logic. If a URL is accessed regularly, it will be re-cached on the next miss after expiry. If it is accessed rarely, it expires and the next access bears a single DB round-trip.

**Redis database separation:** The application uses two separate Redis logical databases. `db 0` stores URL cache entries (`short_id -> long_url` and `clicks:{short_id}`). `db 1` is used exclusively by Flask-Limiter for rate limiting state. This separation prevents rate limit counters from colliding with URL cache keys and allows each concern to be flushed or inspected independently.

**Redis failure handling:** All Redis operations in the redirect route are wrapped in `try/except redis.RedisError`. If Redis is unavailable, the service degrades gracefully: redirects still work via the PostgreSQL fallback. The cache write is also guarded, so a Redis outage on the write path does not cause a 500 — the redirect completes and the entry is simply not cached for next time.

---

### Rate Limiting

`POST /shorten` is rate-limited to 10 requests per minute per IP address using Flask-Limiter with Redis as the storage backend.

```python
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri='redis://localhost:6379/1?protocol=2',
    strategy='fixed-window'
)

@app.route('/shorten', methods=['POST'])
@limiter.limit('10 per minute')
def shorten():
    ...
```

The fixed-window strategy resets the counter at the start of each 60-second window. Requests exceeding the limit receive a `429 Too Many Requests` response automatically from Flask-Limiter. The rate limiting state is stored in Redis db 1, separate from the URL cache in db 0.

The `GET /<short_id>` redirect endpoint is intentionally not rate-limited, as redirect traffic is expected to be high-volume and latency-sensitive. Abuse prevention on the read path would require a different mechanism (e.g., CDN-level throttling).

---

### URL Validation and SSRF Prevention

All submitted URLs are validated by `is_valid_url()` before insertion:

```python
def is_valid_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    if parsed.hostname:
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_loopback or ip.is_private:
                return False
        except ValueError:
            pass  # hostname is a domain name, not an IP literal; allowed
    return True
```

**SSRF prevention:** Server-Side Request Forgery (SSRF) is an attack where a user submits a URL pointing to an internal network resource (e.g., `http://127.0.0.1/admin`, `http://169.254.169.254/` for cloud metadata services). If the application later fetches or processes that URL, it can expose internal services.

In this service the URLs are not fetched server-side — they are only stored and served as redirect targets. The SSRF risk here is not in the server itself making a request, but in the service being used as an open redirector to internal addresses. Blocking loopback and private IP ranges at validation time prevents the shortener from being used to proxy access to internal infrastructure.

The `ValueError` catch around `ipaddress.ip_address()` is intentional: hostnames like `example.com` are not valid IP literals and raise `ValueError`, which the code correctly treats as a domain name (allowed). Only bare IP address literals that resolve to private ranges are blocked.

---

### Request Flows

**Shorten a URL**

```
Client: POST /shorten {"long_url": "https://example.com/path"}
  -> Rate limiter checks: under 10/min for this IP?
  -> is_valid_url() validates scheme and IP range
  -> URLDatabase.insert("https://example.com/path") -> id = 42
  -> base62.encode(42) -> "G"
  -> Response: 201 {"short_url": "http://localhost:5000/G"}
```

**Redirect (cache hit)**

```
Client: GET /G
  -> Redis GET "G" -> "https://example.com/path"   (hit)
  -> Redis INCR "clicks:G"
  -> Response: 302 Location: https://example.com/path
```

**Redirect (cache miss)**

```
Client: GET /G
  -> Redis GET "G" -> nil                           (miss)
  -> base62.decode("G") -> 42
  -> PostgreSQL SELECT long_url FROM urls WHERE id = 42
  -> Redis SET "G" "https://example.com/path" EX 86400
  -> Redis INCR "clicks:G"
  -> Response: 302 Location: https://example.com/path
```

**Redirect (invalid short code)**

```
Client: GET /!!!
  -> Redis GET "!!!" -> nil
  -> base62.decode("!!!") -> ValueError (characters not in alphabet)
  -> Response: 400 {"error": "Invalid URL Format"}
```

---

## API Reference

### POST /shorten

Creates a shortened URL from a long URL.

**Rate limit:** 10 requests per minute per IP.

**Request body:**

```json
{
  "long_url": "https://example.com/some/very/long/path"
}
```

**Success response — 201 Created:**

```json
{
  "short_url": "http://localhost:5000/dnh"
}
```

**Error responses:**

| Status | Condition | Body |
|--------|-----------|------|
| 400    | `long_url` field is missing from the request body | `{"error": "missing url"}` |
| 400    | URL scheme is not `http`/`https`, or IP resolves to loopback/private range | `{"error": "Invalid or Unrestricted URL"}` |
| 429    | Rate limit exceeded (10 requests/minute per IP) | Flask-Limiter default response |

---

### GET /\<short_id\>

Redirects to the original URL associated with the given short code.

**Success response — 302 Found:**

`Location` header is set to the original long URL. The browser or HTTP client follows the redirect automatically.

**Error responses:**

| Status | Condition | Body |
|--------|-----------|------|
| 400    | `short_id` contains characters outside the Base62 alphabet | `{"error": "Invalid URL Format"}` |
| 404    | `short_id` is valid Base62 but no matching row exists in the database | `{"error": "URL not found"}` |

---

## Configuration

All configuration is read from environment variables via `python-dotenv`. Copy `.env.example` to `.env` and fill in the values before starting the server.

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `DATABASE_URL` | Yes | Full PostgreSQL connection string in libpq URI format | `postgresql://postgres:password@localhost:5432/url-shortener` |

Redis connection parameters (host, port, database numbers) are currently hardcoded in `app.py`. To make them configurable, extract them to additional environment variables following the same `os.getenv()` pattern used for `DATABASE_URL`.

---

## CLI Client

`client.py` provides a simple REPL for interacting with the running service without needing curl or an API client.

```
$ python client.py
URL Shortener CLI
Make sure Flask server is running in another terminal
Enter long URL or type 'q' to quit: https://docs.python.org/3/library/ipaddress.html


Successfully Shortened!
Shortened URL: http://127.0.0.1:5000/B

Enter long URL or type 'q' to quit: q
Exiting...
```

The client handles the following cases explicitly:
- Empty input is silently skipped.
- HTTP 429 responses print a human-readable rate limit message instead of attempting to parse the JSON body (which Flask-Limiter may not return as JSON).
- `requests.exceptions.ConnectionError` is caught and reported when the Flask server is not running.

---

## Design Trade-offs and Limitations

| Area | Current Implementation | Production Path |
|------|------------------------|-----------------|
| ID generation | PostgreSQL `SERIAL` (single-node counter) | Distributed ID service (e.g., Twitter Snowflake, Sonyflake) to eliminate single-node bottleneck |
| Connection management | Per-request `psycopg2.connect()` | Connection pool (`ThreadedConnectionPool` or `psycopg3` async pool) to reduce connection overhead under concurrency |
| Click tracking | Redis `INCR` only; not persisted to PostgreSQL | Periodic background job to flush Redis click counters into the `clicks` column for durable analytics |
| Redis availability | Graceful degradation on `RedisError` | Redis Sentinel or Redis Cluster for high availability; managed service (e.g., ElastiCache) in cloud deployments |
| Rate limiting | Fixed-window, per-IP, on `/shorten` only | Sliding-window rate limiting; authenticated quotas per API key; CDN-level throttling for redirect traffic |
| URL validation | Scheme check + IP range block | Optional HEAD request to verify the target URL is reachable; allowlist/blocklist for known malicious domains |
| HTTPS termination | HTTP only (Flask dev server) | Reverse proxy (Nginx, Caddy) handling TLS termination in front of the Flask process |
| Short code collisions | Structurally impossible (counter-based) | Only becomes relevant if switching to hash-based or random ID generation |
| Analytics | Click counters in Redis only | Time-series store or append-only log for per-click events (timestamp, referrer, user-agent, geography) |
| Custom aliases | Not supported | Allow users to specify a preferred short code, falling back to counter-based if taken |