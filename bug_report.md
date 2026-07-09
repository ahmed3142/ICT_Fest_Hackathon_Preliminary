# CoWork — Bug Report

Every fix below was verified empirically against the running API (functional tests
via FastAPI `TestClient`, concurrency tests against a real `uvicorn` server hit by
parallel threads). No behavior was guessed. The API contract (paths, status codes,
error codes, JSON field names, JWT claims) is preserved exactly.

Difficulty tiers: **Easy** (3), **Medium** (5), **Hard/concurrency** (10).

---

## HARD — concurrency & liveness

### H1. Notification deadlock (service hang) — Rule 16
- **File:** `app/services/notifications.py` (`notify_created` / `notify_cancelled`)
- **Bug:** `notify_created` acquired `_email_lock` then `_audit_lock`; `notify_cancelled` acquired them in the opposite order (`_audit_lock` then `_email_lock`). A concurrent create + cancel produced a circular wait.
- **Symptom:** firing create+cancel together hung requests and wedged the whole notification subsystem (15/16 requests timed out; the service stopped responding to bookings).
- **Fix:** acquire the two locks in the same global order (email → audit) in both functions. Work order inside is unchanged.

### H2. Duplicate reference codes — Rule 7
- **File:** `app/services/reference.py` (`next_reference_code`)
- **Bug:** `current = counter; sleep; counter = current + 1` is a non-atomic read-modify-write; concurrent callers read the same value.
- **Symptom:** 8 concurrent bookings all got `CW-001000`.
- **Fix:** wrap the counter increment in a `threading.Lock` so each caller atomically claims a value.

### H3. Rate limit bypass under concurrency — Rule 5
- **File:** `app/services/ratelimit.py` (`record_and_check`)
- **Bug:** the bucket trim/append/write is a non-atomic read-modify-write with a sleep; concurrent callers overwrite each other's bucket, so the count never grows.
- **Symptom:** 25 concurrent `POST /bookings` all passed (0 got 429).
- **Fix:** guard the trim+append+count with a `threading.Lock`.

### H4. Stats drift under concurrency — Rule 14
- **File:** `app/services/stats.py` (`record_create` / `record_cancel`)
- **Bug:** non-atomic read-modify-write on the per-room stats dict.
- **Symptom:** 10 concurrent bookings → stats reported count = 1.
- **Fix:** guard both mutators (and the reader) with a `threading.Lock`.

### H5. Double-booking race — Rule 3
- **File:** `app/routers/bookings.py` (`create_booking` / `_has_conflict`)
- **Bug:** conflict check and insert were separate steps; concurrent requests all passed the check before any committed.
- **Symptom:** 6 concurrent identical-slot bookings all succeeded.
- **Fix:** serialize the conflict check + quota check + insert/commit under a module-level `_booking_lock`.

### H6. Quota race — Rule 4
- **File:** `app/routers/bookings.py` (`create_booking` / `_check_quota`)
- **Bug:** quota `COUNT` and insert were not serialized.
- **Symptom:** 6 concurrent in-window bookings by one member all succeeded (limit is 3).
- **Fix:** covered by the same `_booking_lock` critical section as H5.

### H7. Concurrent cancel → double refund — Rule 6
- **File:** `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** status check and status update were not atomic; each concurrent cancel wrote its own `RefundLog`.
- **Symptom:** 5 concurrent cancels of one booking → 5×`200` and 5 RefundLog entries (5× refund).
- **Fix:** serialize under `_booking_lock`, `db.refresh(booking)` inside the lock to observe a concurrent cancel, then check `ALREADY_CANCELLED`. Exactly one refund results.

### (Bonus) H8. Registration org-creation race — Rules 15 & 16
- **File:** `app/routers/auth.py` (`register`)
- **Bug:** two concurrent `register` calls for the same new `org_name` both created the org; the second hit the `organizations.name` unique constraint → unhandled `IntegrityError`.
- **Symptom:** 8 concurrent registrations → 3×`201`, **5×`500`**.
- **Fix:** catch `IntegrityError` on org creation, roll back, re-fetch the org and join as `member`; also catch it on user insert → `409 USERNAME_TAKEN`.

---

## MEDIUM — logic

### M1. Logout did not invalidate the access token — Rule 8
- **File:** `app/auth.py` (`get_token_payload`)
- **Bug:** logout stored the token's `jti`, but the check compared `payload.get("sub")` against that set — so it never matched.
- **Symptom:** token still worked (`200`) after `POST /auth/logout`.
- **Fix:** check `payload.get("jti") in _revoked_tokens`.

### M2. Refresh tokens were not single-use — Rule 8
- **File:** `app/routers/auth.py` (`refresh`), `app/auth.py`
- **Bug:** `/auth/refresh` issued new tokens but never invalidated the presented refresh token.
- **Symptom:** the same refresh token could be replayed repeatedly (`200`).
- **Fix:** track redeemed refresh `jti`s (`_used_refresh_tokens`); reject reuse with `401` and mark the presented token used on each rotation (RFC 9700 refresh-token rotation).

### M3. Duplicate username returned 201 — Rule 15
- **File:** `app/routers/auth.py` (`register`)
- **Bug:** on an existing username it returned the existing user with `201` instead of erroring.
- **Symptom:** re-registering a username returned `201` (no error code).
- **Fix:** `raise AppError(409, "USERNAME_TAKEN", ...)`.

### M4. UTC-offset input not converted — Rule 1
- **File:** `app/timeutils.py` (`parse_input_datetime`)
- **Bug:** `dt.replace(tzinfo=None)` dropped the offset without converting to UTC.
- **Symptom:** `12:00+05:00` was stored/returned as `12:00Z` (should be `07:00Z`).
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

### M5. Missing minimum-duration / `end ≤ start` validation — Rule 2
- **File:** `app/routers/bookings.py` (`create_booking`)
- **Bug:** only `duration > 8` was checked; a 0-hour (`end == start`) or negative duration slipped through.
- **Symptom:** `end_time == start_time` was accepted (`201`).
- **Fix:** `if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS: raise INVALID_BOOKING_WINDOW`.

### M6. Overlap check rejected back-to-back bookings — Rule 3
- **File:** `app/routers/bookings.py` (`_has_conflict`)
- **Bug:** used `<=` (`b.start <= end and start <= b.end`), so touching intervals counted as overlapping.
- **Symptom:** `[50–51]` then `[51–52]` → second wrongly `409`.
- **Fix:** strict `<` on both comparisons.

### M7. Member could read another member's booking — Rule 10
- **File:** `app/routers/bookings.py` (`get_booking`)
- **Bug:** only org scoping was applied; no per-member ownership check (unlike `cancel_booking`).
- **Symptom:** member 2 could `GET` member 1's booking (`200`, should be `404`).
- **Fix:** add `if user.role != "admin" and booking.user_id != user.id: raise 404 BOOKING_NOT_FOUND`.

### M8. CSV export leaked other orgs' data — Rule 9
- **File:** `app/services/export.py` (`generate_export` / `fetch_bookings_raw`)
- **Bug:** the `include_all` + `room_id` path called `fetch_bookings_raw`, which has no org filter.
- **Symptom:** org A could export org B's room bookings.
- **Fix:** route the `include_all` path through the org-scoped `_fetch_scoped(db, org_id, None, room_id)`.

### M9. `create_booking` left the usage-report cache stale — Rule 12
- **File:** `app/routers/bookings.py` (`create_booking`)
- **Bug:** invalidated the availability cache but not the report cache.
- **Symptom:** a new booking did not appear in a cached `usage-report` (count stayed 0).
- **Fix:** add `cache.invalidate_report(user.org_id)` after commit.

### M10. `cancel_booking` left the availability cache stale — Rule 13
- **File:** `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** invalidated the report cache but not the availability cache.
- **Symptom:** a cancelled booking still showed as a busy interval.
- **Fix:** add `cache.invalidate_availability(booking.room_id, booking.start_time.date().isoformat())`.

### M11. 48-hour refund tier misclassified — Rule 6
- **File:** `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** used floored `notice_hours > 48`, so notice ≥ 48h (e.g. 48.5h) fell into the 50% tier.
- **Symptom:** 48.5h notice → 50% (rule: 100%).
- **Fix:** compare the raw timedelta: `if notice >= timedelta(hours=48): 100`.

### M12 & M13. Refund rounding wrong and response ≠ ledger — Rule 6
- **Files:** `app/routers/bookings.py` (`cancel_booking`, line ~208) and `app/services/refunds.py` (`log_refund`)
- **Bug:** the cancel response used `round(price*pct/100.0)` (Python's `round()` is banker's rounding → `round(500.5)==500`), while `log_refund` used `int(...)` truncation. Both diverged from half-up, and from each other.
- **Symptom:** 50% of 1001 → 500 (rule: 501); and for 1003 the response was 502 while the RefundLog stored 501.
- **Fix:** compute an exact integer half-up amount `(price_cents * percent + 50) // 100` in `log_refund`, and set the cancel response's `refund_amount_cents = entry.amount_cents` so response and ledger are identical by construction.

---

## EASY — one-liners

### E1. Access token lifetime wrong — Rule 8
- **File:** `app/auth.py` (`create_access_token`)
- **Bug:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` → 54000s.
- **Symptom:** `exp − iat` = 54000 (rule: exactly 900).
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` (15 min = 900 s).

### E2. Past-start grace window — Rule 2
- **File:** `app/routers/bookings.py` (`create_booking`)
- **Bug:** `if start <= now - timedelta(seconds=300)` allowed starts up to 5 min in the past.
- **Symptom:** a booking 3 min in the past was accepted (`201`).
- **Fix:** `if start <= now` (strictly future, no grace).

### E3. Booking list sorted descending — Rule 11
- **File:** `app/routers/bookings.py` (`list_bookings`)
- **Bug:** `Booking.start_time.desc()`.
- **Fix:** `Booking.start_time.asc()` (ties by ascending `id`).

### E4. Pagination offset wrong — Rule 11
- **File:** `app/routers/bookings.py` (`list_bookings`)
- **Bug:** `.offset(page * limit)` skipped the first page.
- **Fix:** `.offset((page - 1) * limit)`.

### E5. Pagination limit hardcoded — Rule 11
- **File:** `app/routers/bookings.py` (`list_bookings`)
- **Bug:** `.limit(10)` ignored the `limit` parameter.
- **Fix:** `.limit(limit)`.

### E6. `GET /bookings/{id}` corrupted `start_time` — API contract
- **File:** `app/routers/bookings.py` (`get_booking`)
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrote `start_time` with `created_at`.
- **Symptom:** the detail endpoint returned the creation time in the `start_time` field.
- **Fix:** remove that line (the serializer already sets `start_time` correctly).

### E7. Sub-24h cancellation refunded 50% — Rule 6
- **File:** `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** the `else` branch set `refund_percent = 50`.
- **Symptom:** 10h notice → 50% (rule: 0%).
- **Fix:** `else: refund_percent = 0`.

---

## Verification summary

| Suite | Result |
|---|---|
| Functional probes (19 rule checks) | 19/19 correct |
| Concurrency probes (7 race/liveness checks) | 7/7 correct |
| Registration-race probe | fixed (8×201, 0×500) |
| Edge cases (duration bounds, cross-org 404, roles, auth, CSV header) | 15/15 pass |
| Shipped `tests/test_smoke.py` | pass |

All fixes are minimal and localized; no unrelated code was refactored, and the
API contract is unchanged.
