# Deterministic subscription billing seed

This bundle seeds the five MongoDB collections declared by spec `v001`: `accounts`, `plans`, `subscriptions`, `invoices`, and `payments`. It recreates the collections on every run, creates all declared indexes plus query-supporting indexes, and inserts deterministic dates, values, references, and ObjectIds.

## Requirements

- Node.js 20 or later
- A reachable MongoDB deployment

## Run

```sh
npm install
MONGODB_URI='<MongoDB URI>' npm run seed
```

`DB_NAME` defaults to `poc_01J8Q8MFYJ6NVJ8B2Q5V2D9Q3A` and can be overridden.

## Optional limits

`SEED_MAX_DOCS` places the same upper bound on each collection. `SEED_COLLECTION_CAPS` is a JSON object whose keys independently cap named collections. Effective counts are the minimum of the requested count, global cap, and that collection's cap. Dependent collections can be reduced to zero when a required parent collection is empty so no dangling references are created.

```sh
SEED_MAX_DOCS=100 \
SEED_COLLECTION_CAPS='{"accounts":25,"subscriptions":50,"invoices":75}' \
MONGODB_URI='<MongoDB URI>' \
DB_NAME='billing_demo' \
npm run seed
```

Requested uncapped counts are 60 accounts, 6 plans, 120 subscriptions, 240 invoices, and 180 payments. On success, the script prints exactly one JSON line containing `seed_summary` with direct per-collection counts. Failures are written to standard error and produce a non-zero exit status.
