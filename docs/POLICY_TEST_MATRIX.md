# LEASH — Policy Engine Test Matrix (Phase 2C)

**Document:** `docs/POLICY_TEST_MATRIX.md`
**Revision:** v2.1 (Security Review V2 Approved)
**Date:** 2026-11-04

This document defines the comprehensive test matrix for the deterministic Policy Engine. All tests use a fixed baseline context:
- `context.now = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)`
- Test suite is implemented via `pytest` parameterized tests.

---

## 1. Test Suite Summary & Case Counts

| Rule / Category | Test ID Prefix | Test Focus | Number of Cases |
|---|:---:|---|:---:|
| **P01** | `P01-` | Mandate Signature Verification & Secret Key Integrity | 7 |
| **P02** | `P02-` | Mandate Expiration & Lifecycle State Machine | 7 |
| **P03** | `P03-` | Mandate Total Budget Ceiling (Hard Limit) | 7 |
| **P04** | `P04-` | Per-Item Price Limit & Unset Limit Handling | 7 |
| **P05** | `P05-` | Merchant Allowlist, IDNA, Domain Spoofing, & Schemes | 10 |
| **P06** | `P06-` | Category Allowlist & Empty-List Semantics | 7 |
| **P07** | `P07-` | Quantity Limits (Per-Item vs. Total Items) | 7 |
| **P08** | `P08-` | Currency Consistency & ISO-4217 Validation | 7 |
| **P09** | `P09-` | Live Price Oracle Validation, Staleness, & Variance | 8 |
| **P10** | `P10-` | Daily Spend Reservation & Status Accounting | 7 |
| **P11** | `P11-` | Idempotency, Duplicate Protection, & Replay | 8 |
| **P12** | `P12-` | Content Hash Integrity, SSRF Defense, & Prompt Injection | 10 |
| **INT** | `INT-` | System Precedence, Concurrency, & Approval Binding | 10 |
| **TOTAL** | | **Comprehensive Test Cases** | **102** |

---

## 2. Unit & Rule Test Specifications

### P01 — Mandate Signature Validity (7 cases)
| Test ID | Description | Mandate State | Expected Rule Status | Expected Overall Decision |
|---|---|---|:---:|:---:|
| `P01-01` | Valid HMAC signature, `signed_at` = now - 1h | Signature valid, algorithm HMAC-SHA256 | `PASS` | Depends on other rules |
| `P01-02` | Mandate signature is `None` (unsigned draft) | `signature = None` | `BLOCK` | `BLOCK` |
| `P01-03` | Mandate signature is empty string `""` | `signature = ""` | `BLOCK` | `BLOCK` |
| `P01-04` | Forged / altered signature string | Signature modified by 1 byte | `BLOCK` | `BLOCK` |
| `P01-05` | Timestamp in future (`signed_at` = now + 10s) | Signature valid for future timestamp | `BLOCK` | `BLOCK` |
| `P01-06` | Missing `signed_at` timestamp | `signed_at = None` | `BLOCK` | `BLOCK` |
| `P01-07` | Key rotation (signature generated with old key) | Verified against current `MANDATE_SIGNING_KEY` | `BLOCK` | `BLOCK` |

---

### P02 — Mandate Expiration & Lifecycle Status (7 cases)
| Test ID | Description | Mandate Status & Expiration | Expected Rule Status | Expected Overall Decision |
|---|---|---|:---:|:---:|
| `P02-01` | Mandate is ACTIVE and unexpired (`expires_at` = now + 24h) | Status ACTIVE, unexpired | `PASS` | Depends on other rules |
| `P02-02` | Mandate status is REVOKED | Status REVOKED, unexpired | `BLOCK` | `BLOCK` |
| `P02-03` | Mandate status is DRAFT | Status DRAFT, unexpired | `BLOCK` | `BLOCK` |
| `P02-04` | Mandate status is SIGNED (not yet activated) | Status SIGNED, unexpired | `BLOCK` | `BLOCK` |
| `P02-05` | Expiration exact boundary (`expires_at == now`) | Status ACTIVE, `expires_at = now` | `BLOCK` | `BLOCK` |
| `P02-06` | Mandate expired in past (`expires_at` = now - 1s) | Status ACTIVE, expired | `BLOCK` | `BLOCK` |
| `P02-07` | Missing `expires_at` timestamp | Status ACTIVE, `expires_at = None` | `BLOCK` | `BLOCK` |

---

### P03 — Mandate Budget Ceiling (7 cases)
*Mandate signed budget: \$40.00. Rule P03 is a hard block; user approval cannot expand budget.*
| Test ID | Description | Proposal Total | Cumulative Spent | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|:---:|:---:|
| `P03-01` | Proposal well within budget | \$16.00 | \$0.00 | `False` | `PASS` | `ALLOW` |
| `P03-02` | Proposal exactly at budget ceiling | \$40.00 | \$0.00 | `False` | `PASS` | `ALLOW` |
| `P03-03` | Proposal exceeds budget ceiling | \$45.00 | \$0.00 | `False` | `BLOCK` | `BLOCK` |
| `P03-04` | Proposal exceeds budget (approval enabled) | \$45.00 | \$0.00 | `True` | `BLOCK` | `BLOCK` (No override) |
| `P03-05` | Cumulative spend exceeds budget ceiling | \$15.00 | \$30.00 | `False` | `BLOCK` | `BLOCK` |
| `P03-06` | Proposal total is negative or zero (\$0.00) | \$0.00 | \$0.00 | `False` | `BLOCK` | `BLOCK` |
| `P03-07` | User approval attempt on budget overage | \$45.00 | \$0.00 | `True` | `BLOCK` | `BLOCK` |

---

### P04 — Per-Item Price Limit (7 cases)
*Configured mandate `max_price_per_item`: \$25.00.*
| Test ID | Description | Configured Limit | Unit Price | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|:---:|:---:|
| `P04-01` | Unit price within limit | \$25.00 | \$12.50 | `False` | `PASS` | `ALLOW` |
| `P04-02` | Unit price exactly at limit | \$25.00 | \$25.00 | `False` | `PASS` | `ALLOW` |
| `P04-03` | Unit price exceeds limit (no approval) | \$25.00 | \$30.00 | `False` | `BLOCK` | `BLOCK` |
| `P04-04` | Unit price exceeds limit (approval enabled) | \$25.00 | \$30.00 | `True` | `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `P04-05` | No item price limit configured | `None` | \$100.00 | `False` | `NOT_APPLICABLE` | `ALLOW` |
| `P04-06` | Unit price is negative | \$25.00 | -\$5.00 | `False` | `BLOCK` | `BLOCK` |
| `P04-07` | Limit is set to zero (corrupted mandate) | \$0.00 | \$10.00 | `False` | `BLOCK` | `BLOCK` |

---

### P05 — Merchant Allowlist Validation (10 cases)
*Mandate allowlist: `["amazon.com", "xn--bcher-kva.de"]` (`bücher.de` in punycode).*
| Test ID | Description | Proposal Merchant | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|---|:---:|:---:|:---:|
| `P05-01` | Exact match in allowlist | `amazon.com` | `False` | `PASS` | `ALLOW` |
| `P05-02` | Subdomain mismatch | `store.amazon.com` | `False` | `BLOCK` | `BLOCK` |
| `P05-03` | Unlisted merchant (no approval) | `evil.com` | `False` | `BLOCK` | `BLOCK` |
| `P05-04` | Unlisted merchant (approval enabled) | `target.com` | `True` | `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `P05-05` | Merchant includes URI scheme | `https://amazon.com` | `False` | `BLOCK` | `BLOCK` |
| `P05-06` | Merchant includes path | `amazon.com/books` | `False` | `BLOCK` | `BLOCK` |
| `P05-07` | Merchant includes embedded port | `amazon.com:443` | `False` | `BLOCK` | `BLOCK` |
| `P05-08` | Merchant includes user credentials | `admin@amazon.com` | `False` | `BLOCK` | `BLOCK` |
| `P05-09` | Internationalized Domain Name (IDNA) | `bücher.de` | `False` | `PASS` | `ALLOW` |
| `P05-10` | Empty mandate allowlist (`[]`) | `amazon.com` | `False` | `BLOCK` | `BLOCK` |

---

### P06 — Product Category Allowlist Validation (7 cases)
*Mandate categories: `["books", "office-supplies"]`.*
| Test ID | Description | Proposal Category | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|---|:---:|:---:|:---:|
| `P06-01` | Allowed category exact match | `books` | `False` | `PASS` | `ALLOW` |
| `P06-02` | Case-insensitive normalisation | `BOOKS` | `False` | `PASS` | `ALLOW` |
| `P06-03` | Unlisted category (no approval) | `electronics` | `False` | `BLOCK` | `BLOCK` |
| `P06-04` | Unlisted category (approval enabled) | `electronics` | `True` | `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `P06-05` | Empty mandate categories (`[]`) | `books` | `False` | `BLOCK` | `BLOCK` |
| `P06-06` | Missing proposal category | `""` or `None` | `False` | `BLOCK` | `BLOCK` |
| `P06-07` | Whitespace padded category | `  books  ` | `False` | `PASS` | `ALLOW` |

---

### P07 — Quantity Restrictions (7 cases)
*Mandate: `max_quantity_per_item = 2`, `max_item_count = 5`.*
| Test ID | Description | Proposal Quantity | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|:---:|
| `P07-01` | Quantity within limits | 1 | `False` | `PASS` | `ALLOW` |
| `P07-02` | Quantity exactly at per-item limit | 2 | `False` | `PASS` | `ALLOW` |
| `P07-03` | Quantity exceeds per-item limit (no approval) | 3 | `False` | `BLOCK` | `BLOCK` |
| `P07-04` | Quantity exceeds per-item limit (approval enabled) | 3 | `True` | `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `P07-05` | Quantity exceeds max total item count | 6 | `False` | `BLOCK` | `BLOCK` |
| `P07-06` | Quantity is zero or negative | 0 | `False` | `BLOCK` | `BLOCK` |
| `P07-07` | Quantity exceeds upper boundary (`MAX_QUANTITY`) | 10001 | `False` | `BLOCK` | `BLOCK` |

---

### P08 — Currency Consistency (7 cases)
*Mandate currency: `USD`.*
| Test ID | Description | Proposal Currency | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|
| `P08-01` | Matching currency `USD` | `USD` | `PASS` | Depends on other rules |
| `P08-02` | Currency mismatch `EUR` | `EUR` | `BLOCK` | `BLOCK` |
| `P08-03` | Currency mismatch with approval enabled | `EUR` | `BLOCK` | `BLOCK` (No override) |
| `P08-04` | Lowercase currency code | `usd` | `BLOCK` | `BLOCK` |
| `P08-05` | Unsupported currency | `BTC` | `BLOCK` | `BLOCK` |
| `P08-06` | Missing proposal currency | `None` | `BLOCK` | `BLOCK` |
| `P08-07` | Matching zero-decimal currency | Both `JPY` | `PASS` | Depends on other rules |

---

### P09 — Live Price Validation & Tolerance (8 cases)
*Proposal unit price: \$12.50. Tolerance: 5.00%.*
| Test ID | Description | Oracle Status | Oracle Price | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|:---:|:---:|
| `P09-01` | Variance within 5% tolerance | Available, fresh | \$12.45 (0.4%) | `False` | `PASS` | `ALLOW` |
| `P09-02` | Variance exactly at 5% boundary | Available, fresh | \$11.88 (4.96%) | `False` | `PASS` | `ALLOW` |
| `P09-03` | Variance exceeds 5% (no approval) | Available, fresh | \$10.00 (20%) | `False` | `BLOCK` | `BLOCK` |
| `P09-04` | Variance exceeds 5% (approval enabled) | Available, fresh | \$10.00 (20%) | `True` | `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `P09-05` | Price oracle unavailable / unreachable | Unavailable | `None` | `False` | `NOT_APPLICABLE` | `ALLOW` |
| `P09-06` | Price oracle reading is stale (> 5m old) | Stale | \$12.50 | `False` | `NOT_APPLICABLE` | `ALLOW` |
| `P09-07` | Product SKU not indexed in oracle | Available, fresh | `None` | `False` | `NOT_APPLICABLE` | `ALLOW` |
| `P09-08` | Oracle returns corrupted price (\$0.00) | Available, fresh | \$0.00 | `False` | `BLOCK` | `BLOCK` |

---

### P10 — Daily Spend Reservation & Limit (7 cases)
*Daily limit: \$50.00.*
| Test ID | Description | Today Reserved | Proposal Total | `require_user_approval` | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|:---:|:---:|
| `P10-01` | Projected spend within daily limit | \$20.00 | \$15.00 | `False` | `PASS` | `ALLOW` |
| `P10-02` | Projected spend exactly at daily limit | \$35.00 | \$15.00 | `False` | `PASS` | `ALLOW` |
| `P10-03` | Projected spend exceeds limit (no approval) | \$40.00 | \$15.00 | `False` | `BLOCK` | `BLOCK` |
| `P10-04` | Projected spend exceeds limit (approval enabled) | \$40.00 | \$15.00 | `True` | `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `P10-05` | No daily limit configured | `None` | \$100.00 | `False` | `NOT_APPLICABLE` | `ALLOW` |
| `P10-06` | Pending SUBMITTED proposals count against limit | \$40.00 (pending) | \$15.00 | `False` | `BLOCK` | `BLOCK` |
| `P10-07` | REJECTED proposals released from reservation | \$0.00 (cleared) | \$15.00 | `False` | `PASS` | `ALLOW` |

---

### P11 — Duplicate Purchase & Replay Protection (8 cases)
| Test ID | Description | Existing Match in DB | Idempotency Key Match | Expected Rule Status | Expected Overall Decision |
|---|---|:---:|:---:|:---:|:---:|
| `P11-01` | Completely fresh unique proposal | None | None | `PASS` | Depends on other rules |
| `P11-02` | Fresh proposal with fresh idempotency key | None | Fresh key | `PASS` | Depends on other rules |
| `P11-03` | Identical retry with same `proposal_id` | Match same ID | N/A | `PASS` (Replay) | Returns original proposal |
| `P11-04` | Duplicate terms submitted under new `proposal_id` | Match different ID | N/A | `BLOCK` | `BLOCK` |
| `P11-05` | Idempotent replay: key and fingerprint match | Match same | Match same | `PASS` (Replay) | Returns original proposal |
| `P11-06` | Idempotency conflict: key reused with changed terms | No match | Match key | `BLOCK` | `BLOCK` (409 Conflict) |
| `P11-07` | Proposal missing fingerprint field | `None` | None | `BLOCK` | `BLOCK` |
| `P11-08` | Race: simultaneous insert hits unique index | Match race | N/A | `PASS` (Caught) | Returns existing proposal |

---

### P12 — Product Integrity, SSRF & Injection Defense (10 cases)
| Test ID | Description | Payload Anomaly / Check | Expected Rule Status | Expected Overall Decision |
|---|---|---|:---:|:---:|
| `P12-01` | Valid clean proposal, hash verifies | Clean data, valid HTTPS URL | `PASS` | Depends on other rules |
| `P12-02` | Tampered proposal content hash mismatch | DB content hash altered | `BLOCK` (P12-A) | `BLOCK` |
| `P12-03` | Disallowed control character in product name | Contains `\x00` | `BLOCK` (P12-B) | `BLOCK` |
| `P12-04` | Non-HTTP/HTTPS URI scheme in source | `ftp://files.example.com` | `BLOCK` (P12-B) | `BLOCK` |
| `P12-05` | Product source includes user credentials | `https://user:pass@amazon.com` | `BLOCK` (P12-B) | `BLOCK` |
| `P12-06` | SSRF attempt targeting `localhost` | `http://localhost:8000/secret` | `BLOCK` (P12-B) | `BLOCK` |
| `P12-07` | SSRF attempt targeting cloud metadata service | `http://169.254.169.254/meta` | `BLOCK` (P12-B) | `BLOCK` |
| `P12-08` | Prompt injection marker in product name | `Ignore previous instructions` | `BLOCK` (P12-C) | `BLOCK` |
| `P12-09` | SQL injection marker in product name | `Hardcover -- DROP TABLE` | `BLOCK` (P12-C) | `BLOCK` |
| `P12-10` | HTML script injection in product name | `<script>alert(1)</script>` | `BLOCK` (P12-C) | `BLOCK` |

---

## 3. End-to-End System Precedence & Approval Tests (10 cases)

| Test ID | Description | Scenario Details | Expected Decision |
|---|---|---|:---:|
| `INT-01` | Clean evaluation with `require_user_approval == False` | All rules PASS, mandate permits autonomous execution | `ALLOW` |
| `INT-02` | Clean evaluation with `require_user_approval == True` | All rules PASS, mandate requires user confirmation | `REQUIRE_APPROVAL` |
| `INT-03` | Hard BLOCK overrides soft REQUIRE_APPROVAL | P04 is `REQUIRE_APPROVAL` but P08 currency is `BLOCK` | `BLOCK` |
| `INT-04` | Multiple soft exceptions all require approval | P04, P05, and P06 all produce `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |
| `INT-05` | Single-use approval token verification | Valid token re-evaluated against exact proposal | `ALLOW` |
| `INT-06` | Approval token rejected after terms modified | Proposal unit price changed after user sign-off | `BLOCK` |
| `INT-07` | Expired approval token | Approval token evaluated after 15m TTL expiration | `BLOCK` |
| `INT-08` | Checkout revalidation price drift | Oracle price drifts > 5% between approval and capture | `BLOCK` |
| `INT-09` | Merchant exception does not mutate mandate allowlist | Unlisted merchant approved for Proposal A; Proposal B for same merchant still requires approval | `REQUIRE_APPROVAL` |
| `INT-10` | Concurrent spend race condition reservation | Two simultaneous \$30 proposals against \$50 budget; one commits, second is blocked | 1st `ALLOW`, 2nd `BLOCK` |

---

*End of POLICY_TEST_MATRIX.md v2.1*
