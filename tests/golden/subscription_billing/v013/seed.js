'use strict';

const { MongoClient, ObjectId } = require('mongodb');
const { createHash } = require('node:crypto');

const MONGODB_URI = process.env.MONGODB_URI;
const DB_NAME = process.env.DB_NAME || 'poc_01J8Q8MFYJ6NVJ8B2Q5V2D9Q3A';
const SEED_MAX_DOCS = process.env.SEED_MAX_DOCS;
const SEED_COLLECTION_CAPS = process.env.SEED_COLLECTION_CAPS;

const collectionNames = ['accounts', 'plans', 'subscriptions', 'invoices', 'payments'];
const requestedCounts = {
  accounts: 60,
  plans: 6,
  subscriptions: 120,
  invoices: 240,
  payments: 180
};

function parseNonNegativeInteger(value, label, fallback) {
  if (value === undefined || value === '') return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`${label} must be a non-negative safe integer`);
  }
  return parsed;
}

function resolveCounts() {
  const globalCap = parseNonNegativeInteger(SEED_MAX_DOCS, 'SEED_MAX_DOCS', Number.MAX_SAFE_INTEGER);
  let caps = {};
  if (SEED_COLLECTION_CAPS !== undefined && SEED_COLLECTION_CAPS !== '') {
    try {
      caps = JSON.parse(SEED_COLLECTION_CAPS);
    } catch (_error) {
      throw new Error('SEED_COLLECTION_CAPS must be valid JSON');
    }
    if (caps === null || Array.isArray(caps) || typeof caps !== 'object') {
      throw new Error('SEED_COLLECTION_CAPS must be a JSON object');
    }
  }

  const counts = {};
  for (const name of collectionNames) {
    const collectionCap = Object.prototype.hasOwnProperty.call(caps, name)
      ? parseNonNegativeInteger(caps[name], `SEED_COLLECTION_CAPS.${name}`)
      : Number.MAX_SAFE_INTEGER;
    counts[name] = Math.min(requestedCounts[name], globalCap, collectionCap);
  }

  if (counts.accounts === 0 || counts.plans === 0) counts.subscriptions = 0;
  if (counts.accounts === 0 || counts.subscriptions === 0) counts.invoices = 0;
  if (counts.accounts === 0 || counts.invoices === 0) counts.payments = 0;
  return counts;
}

function deterministicId(namespace, index) {
  const bytes = createHash('sha256')
    .update(`20260923:${namespace}:${index}`)
    .digest()
    .subarray(0, 12);
  return new ObjectId(bytes);
}

function day(offset) {
  return new Date(Date.UTC(2026, 8, 23 + offset, 12, 0, 0));
}

function buildDocuments(counts) {
  const accountStatuses = ['active', 'trial', 'delinquent'];
  const accounts = Array.from({ length: counts.accounts }, (_, i) => ({
    _id: deterministicId('accounts', i),
    account_number: `ACCT-${String(i + 1).padStart(6, '0')}`,
    name: `Subscription Account ${String(i + 1).padStart(3, '0')}`,
    email: `billing${String(i + 1).padStart(3, '0')}@example.test`,
    status: accountStatuses[i % accountStatuses.length],
    created_at: day(-180 + i * 2)
  }));

  const planNames = ['Starter', 'Basic', 'Growth', 'Professional', 'Business', 'Enterprise'];
  const plans = Array.from({ length: counts.plans }, (_, i) => ({
    _id: deterministicId('plans', i),
    plan_code: `PLAN-${String(i + 1).padStart(2, '0')}`,
    name: planNames[i],
    monthly_price: [9, 19, 39, 79, 149, 299][i],
    active: true
  }));

  const subscriptions = Array.from({ length: counts.subscriptions }, (_, i) => {
    const account = accounts[i % accounts.length];
    const status = i % 5 === 4 ? 'canceled' : 'active';
    return {
      _id: deterministicId('subscriptions', i),
      account_id: account._id,
      plan_id: plans[(i * 5 + Math.floor(i / Math.max(accounts.length, 1))) % plans.length]._id,
      status,
      started_at: day(-150 + (i % 100)),
      current_period_end: day(7 + (i % 45))
    };
  });

  const invoiceStatuses = ['paid', 'open', 'overdue'];
  const invoices = Array.from({ length: counts.invoices }, (_, i) => {
    const subscription = subscriptions[i % subscriptions.length];
    const account = accounts.find((item) => item._id.equals(subscription.account_id));
    const plan = plans.find((item) => item._id.equals(subscription.plan_id));
    const status = invoiceStatuses[i % invoiceStatuses.length];
    const issuedAt = day(-90 + (i % 100));
    return {
      _id: deterministicId('invoices', i),
      invoice_number: `INV-${String(i + 1).padStart(7, '0')}`,
      account_id: account._id,
      subscription_id: subscription._id,
      status,
      amount_due: plan.monthly_price,
      due_at: new Date(issuedAt.getTime() + 14 * 86400000),
      issued_at: issuedAt
    };
  });

  const payments = Array.from({ length: counts.payments }, (_, i) => {
    const invoice = invoices[(i * 3) % invoices.length];
    const status = i % 6 === 5 ? 'failed' : 'paid';
    return {
      _id: deterministicId('payments', i),
      payment_reference: `PAY-${String(i + 1).padStart(7, '0')}`,
      account_id: invoice.account_id,
      invoice_id: invoice._id,
      status,
      amount: invoice.amount_due,
      paid_at: day(-70 + (i % 90))
    };
  });

  return { accounts, plans, subscriptions, invoices, payments };
}

const indexes = {
  accounts: [
    { key: { account_number: 1 }, unique: true },
    { key: { email: 1 }, unique: true },
    { key: { created_at: -1, status: 1 } },
    { key: { status: 1, created_at: -1 } }
  ],
  plans: [
    { key: { plan_code: 1 }, unique: true },
    { key: { active: 1, monthly_price: 1 } }
  ],
  subscriptions: [
    { key: { account_id: 1, status: 1 } },
    { key: { current_period_end: 1, plan_id: 1 } },
    { key: { account_id: 1, status: 1, current_period_end: 1 } }
  ],
  invoices: [
    { key: { invoice_number: 1 }, unique: true },
    { key: { account_id: 1, due_at: 1, status: 1 } },
    { key: { issued_at: -1, subscription_id: 1 } },
    { key: { account_id: 1, status: 1, due_at: 1 } }
  ],
  payments: [
    { key: { payment_reference: 1 }, unique: true },
    { key: { invoice_id: 1, status: 1 } },
    { key: { account_id: 1, paid_at: -1 } },
    { key: { status: 1 } }
  ]
};

async function main() {
  if (!MONGODB_URI) throw new Error('MONGODB_URI is required');
  const counts = resolveCounts();
  const documents = buildDocuments(counts);
  const client = new MongoClient(MONGODB_URI);

  try {
    await client.connect();
    const db = client.db(DB_NAME);

    for (const name of [...collectionNames].reverse()) {
      try {
        await db.collection(name).drop();
      } catch (error) {
        if (error && error.codeName !== 'NamespaceNotFound' && error.code !== 26) throw error;
      }
    }

    for (const name of collectionNames) {
      await db.createCollection(name);
      await db.collection(name).createIndexes(indexes[name]);
      if (documents[name].length > 0) {
        await db.collection(name).insertMany(documents[name], { ordered: true });
      }
    }

    const seedSummary = {};
    for (const name of collectionNames) {
      seedSummary[name] = await db.collection(name).countDocuments({});
    }
    console.log(JSON.stringify({ seed_summary: seedSummary }));
  } finally {
    await client.close();
  }
}

main().catch((error) => {
  const message = error && typeof error.message === 'string' ? error.message : 'unknown error';
  console.error(`Seed failed: ${message}`);
  process.exitCode = 1;
});
