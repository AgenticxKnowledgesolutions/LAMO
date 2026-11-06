# LAMO – SECTION 12: DATABASE ARCHITECTURE & DATA INTEGRITY AUDIT REPORT
**Audit Date:** November 06, 2026  
**Auditor:** Senior Django Database Architect & MySQL Specialist (Antigravity Senior Audit Team)  
**System:** LAMO – Labour and Service Management System  
**Mode:** STRICT READ-ONLY AUDIT (Zero code/database modifications made)  
**Language:** Simple Manglish with Technical English References  

---

## 1. EXECUTIVE SUMMARY

LAMO system-inte database layer (`models.py`, `settings.py`, and 42 database migrations) comprehensive technical and architectural audit complete aaki. 

Database architecture-il core relational structure (MySQL InnoDB engine targeting) highly mature aanu. Decimal fields for currency, comprehensive foreign keys, critical one-to-one models, cascading rules, and 21+ concurrency points-il `select_for_update()` row-level locks use cheythittund. Furthermore, platform fee deduplication, booking arrival verification, and emergency booking locks state machine level-il tightly guarded aanu.

Ennalum, production scale-il data integrity-kkum concurrency-kkum impact cheyyunna **2 Important P2 Issues** and **several Architectural Observables** identify cheythittund:
1. **WalletTransaction UTR Duplication Risk (P2):** `WalletTransaction.reference_id` field-il database-level `unique=True` constraint illa. Concurrency duplicate checks view-level Python validation-il maathram aanu depend cheyyunnath.
2. **Booking Slot Exclusion Constraint Absent (P2):** `Booking` model-il `(worker, date, time/slot)` composite unique constraint illa. System fully depends on `select_for_update()` and `has_worker_schedule_conflict()` application-level checks.
3. **Database Engine & Environment Setup:** Settings file-il MySQL production with Aiven SSL certificates properly structured aanu, with fallback protection preventing accidental SQLite usage in production.

---

## 2. DATABASE CONFIGURATION & SETTINGS AUDIT (`lamo/settings.py`)

### 2.1 Configuration Review
* **Database Driver / Engine:** Default production engine `django.db.backends.mysql` with PyMySQL fallback (`pymysql.install_as_MySQLdb()`).
* **Connection Pooling / Lifetime:** `CONN_MAX_AGE` is configurable via `DJANGO_DB_CONN_MAX_AGE` with default `60` seconds (`CONN_MAX_AGE=60`). This prevents excessive TCP/SSL handshake overhead on cloud MySQL databases (like Aiven/AWS RDS).
* **SSL / TLS Configuration:**
  - Auto-detects Aiven MySQL URLs (`aivencloud.com`).
  - Sets `ssl_mode='VERIFY_CA'` or `VERIFY_IDENTITY` and passes `cert/ca.pem` certificate bundle.
  - Fail-safe check: If CA file does not exist, warns or configures appropriate fallback.
* **Silent SQLite Fallback Safety:**
  - `lamo/settings.py`-il `DATABASE_URL` empty aayalum engine default aayi `django.db.backends.mysql` (host `127.0.0.1:3306`) use cheyyum. SQLite use cheyyanamenkil `DJANGO_DB_ENGINE=sqlite` explicitly specify cheyyanam. Production-il environment variable miss aayal silent SQLite create aakilla, connection failure throw cheyyum (Good practice).
* **Test Isolation:**
  - `'test' in sys.argv` aayirikkumbol SQLite in-memory database override cheyyunnu (`:memory:`), ensuring rapid test execution without touching MySQL schema.

| Parameter | Configuration Found | Audit Status | Remarks |
|---|---|---|---|
| Engine | `django.db.backends.mysql` | ✅ SECURE | PyMySQL compatibility shim present |
| Connection Pooling | `CONN_MAX_AGE = 60` | ✅ OPTIMAL | Prevents connection thrashing |
| Cloud SSL | Aiven `VERIFY_CA` + `ca.pem` | ✅ SECURE | Encrypted in-flight communication |
| Production Fallback | Requires explicit `sqlite` env | ✅ SAFE | No accidental SQLite creation in prod |
| Charset / Collation | MySQL Default UTF-8 | ⚠️ OBSERVABLE | Need utf8mb4 explicitly configured |

---

## 3. DATA MODELS & RELATIONAL ARCHITECTURE AUDIT

LAMO has 19 active database models across user management, booking workflows, wallets, notifications, communications, and disputes:

### 3.1 Model Relationship & Cascade Matrix

| Model | Key Relationships | `on_delete` Strategy | Audit Finding |
|---|---|---|---|
| `CustomUser` | Base Auth Model | N/A | Central identity model |
| `UserProfile` | `OneToOneField(CustomUser)` | `CASCADE` | User delete aayal profile automatically cleans up |
| `WorkerProfile` | `OneToOneField(CustomUser)`, `ForeignKey(Service)` | `CASCADE` (User), `SET_NULL` (Service) | Worker delete protected through bookings; Service removal does not drop worker records |
| `Booking` | `customer (User)`, `worker (WorkerProfile)`, `service (Service)`, `series (BookingSeries)` | `customer`: `CASCADE`<br>`worker`: **`PROTECT`**<br>`service`: `SET_NULL`<br>`series`: `SET_NULL` | **Excellent Data Protection:** `models.PROTECT` on `worker` prevents accidental or malicious deletion of worker accounts having active/past financial or booking records! |
| `Payment` | `OneToOneField(Booking)` | `CASCADE` | 1:1 constraint strictly prevents multiple primary payment records per booking |
| `WorkerWallet` | `OneToOneField(WorkerProfile)` | `CASCADE` | 1:1 wallet per worker profile |
| `WalletTransaction` | `ForeignKey(WorkerWallet)`, `ForeignKey(Booking)` | `wallet`: `CASCADE`<br>`booking`: `SET_NULL` | Booking delete aayalum transaction history retains for auditing |
| `Review` | `OneToOneField(Booking)` | `CASCADE` | Strict 1:1 guarantees customer cannot submit duplicate reviews for the same booking |
| `Complaint` | `ForeignKey(Booking)`, `ForeignKey(User)` | `CASCADE`, `unique_together=('booking', 'customer')` | Guarantees one complaint per user per booking |
| `WorkerLeave` | `ForeignKey(WorkerProfile)` | `CASCADE`, `unique_together=['worker', 'leave_date']` | Prevents duplicate leave markings for the same day |
| `PushSubscription` | `ForeignKey(CustomUser)` | `CASCADE`, `endpoint=unique=True` | Endpoint unique constraint prevents duplicate web push endpoints |

---

## 4. CONSTRAINTS, INDEXES & QUERY PERFORMANCE AUDIT

### 4.1 Indexing Status
* **`Booking` Model:**
  - `booking_worker_date_status_idx` on `['worker', 'date', 'status']` — Highly optimized! Worker availability, conflict checks, and dashboard queries evaluate `(worker, date, status)` without table scans.
  - `booking_cust_created_idx` on `['customer', '-created_at']` — Optimized for Customer Dashboard recent booking lists.
  - `booking_stat_dist_idx` on `['status', 'district']` — Optimized for worker emergency/general broadcast discovery.
* **`Notification` Model:**
  - `notif_recip_read_idx` on `['recipient', 'is_read']` — Optimized for unread badge count queries.
  - `notif_recip_created_idx` on `['recipient', '-created_at']` — Optimized for user notification inbox pagination.
* **`WorkerProfile` Model:**
  - `worker_svc_ver_stat_dist_idx` on `['service', 'is_verified', 'work_status', 'district']` — Compound index covers customer worker searches.

### 4.2 N+1 Query Optimization Audit
* `views.py`-il **76 occurrences** of `select_related()` and `prefetch_related()` use cheythittund.
* Critical paths (e.g. `customer_dashboard`, `worker_dashboard`, `admin_dashboard`, `booking_detail`) properly chain `select_related('customer', 'worker__user', 'service')`, eliminating repetitive database roundtrips.

---

## 5. FINANCIAL & TRANSACTIONAL INTEGRITY AUDIT

### 5.1 Currency & Money Representation
* All financial values (`pricing_amount`, `total_amount`, `balance`, `amount`, `platform_fee`, `material_amount`, `worker_share`) use `models.DecimalField(max_digits=10, decimal_places=2)`.
* **Zero Float Precision Leakage:** Floating point arithmetic calculations database level-il store cheyyunnilla.

### 5.2 Concurrency & Race Condition Protections (`select_for_update`)
* Views file-il **21 separate atomic transaction blocks** use `select_for_update()` row-level locks:
  1. Wallet recharge approvals (`admin_approve_recharge`) lock both `WalletTransaction` and `WorkerWallet`.
  2. Booking status transitions (`accept_booking`, `start_service`, `complete_booking`, `cancel_booking`) lock `Booking` rows.
  3. Platform fee settlement `_settle_booking_completion_financials()` locks `WorkerWallet` inside `transaction.atomic()`.
  4. Platform fee deduplication protected via boolean flag `booking.fee_settled` (checked and marked under lock).

---

## 6. CONFIRMED ISSUES & DEFICIENCIES (AUDIT FINDINGS)

### 🔴 Finding 1 (P2): Missing Database Unique Constraint on `WalletTransaction.reference_id` (UTR)
* **Location:** `lamo/lamoapp/models.py` (Line 1968, `WalletTransaction` model)
* **Details:**
  ```python
  reference_id = models.CharField(
      max_length=100,
      blank=True,
      default='',
      help_text="Bank transaction reference / UTR number for manual recharges"
  )
  ```
* **Risk:**
  `reference_id` (UTR number) view level-il `WalletTransaction.objects.filter(reference_id=reference_id).exists()` vechu check cheyyunnund. Ennalum, database level-il unique constraint illa (`blank=True` aayathukondu ordinary `unique=True` empty strings-il conflict aavaam, ennal Django `UniqueConstraint(fields=['reference_id'], condition=~Q(reference_id=''))` missing aanu).
  Concurrent HTTP requests submission-il race condition vannal duplicate UTR transaction records create aavan theoretical window und.
* **Recommended Solution:** Add a partial unique constraint:
  ```python
  class Meta:
      constraints = [
          models.UniqueConstraint(
              fields=['reference_id'],
              condition=~models.Q(reference_id=''),
              name='unique_non_empty_wallet_tx_reference_id'
          )
      ]
  ```

---

### 🟡 Finding 2 (P2): Missing Database-Level Exclusion/Unique Constraint for Worker Double-Booking
* **Location:** `lamo/lamoapp/models.py` (`Booking` model)
* **Details:**
  Booking conflict prevention (`has_worker_schedule_conflict()`) application logic-il `select_for_update()` vechanu guard cheythirikkunnath. Database level-il active bookings-inu (`status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']`) `(worker, date, time)` unique constraint illa.
* **Risk:**
  Yathoru direct database insert or third-party background batch job execute cheythal worker-nu same time slot-il double booking create aavam.
* **Assessment:**
  Application level row-locking is currently protecting the standard booking flow, but database-level defensive constraint is absent.

---

### ℹ️ Finding 3 (Observable): MySQL `utf8mb4` Explicit Collation Specification
* **Location:** `lamo/lamo/settings.py` (`DATABASES` configuration)
* **Details:**
  Cloud MySQL (Aiven) connect cheyyumbozhum local MySQL connect cheyyumbozhum explicit `OPTIONS: {'charset': 'utf8mb4'}` settings-il define cheythittilla.
* **Risk:**
  Customer or worker Malayalam, Hindi, or emojis review/notes/messages input cheyyumbol MySQL default charset `latin1` or standard 3-byte `utf8` aaneil database `Incorrect string value` exception raise cheyyam.
* **Recommended Solution:** Add `OPTIONS`:
  ```python
  'OPTIONS': {
      'charset': 'utf8mb4',
      'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
  }
  ```

---

## 7. MIGRATION SYSTEM INTEGRITY AUDIT

* **Migration Sequence:** Migrations are sequentially numbered from `0001_initial.py` to `0042_booking_proposed_date_booking_proposed_time_and_more.py`.
* **Dependency Tree:**
  - All migration files strictly reference their immediate previous migration in the `dependencies` list.
  - Zero broken dependencies or migration merge conflicts detected.
* **Production Status:** UNVERIFIED (Production database credentials/live environment read-only access is not connected in this sandbox; migrations table on live cloud database could not be polled directly, in adherence to strict read-only rules).

---

## 8. SUMMARY SCORECARD

| Domain | Score | Assessment |
|---|---|---|
| Schema Normalization & Types | 9.5 / 10 | Excellent decimal usage, clear 1:1, FK structures |
| Foreign Key & Delete Safety | 9.5 / 10 | `PROTECT` on Worker in Bookings guarantees audit trails |
| Concurrency & Locks | 9.0 / 10 | 21+ `select_for_update()` locks protect critical paths |
| Indexing Strategy | 9.0 / 10 | Compound indexes present on Booking, Notification, WorkerProfile |
| Constraints & Deduplication | 8.0 / 10 | Good model constraints; needs UTR partial unique constraint |
| Overall DB Architecture | **9.0 / 10** | **PRODUCTION READY (with minor hardening recommended)** |

---

## 9. CONCLUSION & AUDIT SIGN-OFF

The LAMO Database Architecture and Data Integrity audit confirms that the relational structure and financial transaction locks are solid, secure, and well-designed for high-concurrency operations under MySQL.

**Strict Audit Protocol Notice:**
As per user instruction, **no files have been modified**, no migrations generated, and no database commands executed. Please review this audit report and approve any necessary fixes before implementation.

