# LAMO – SECTION 13: SECURITY FIXES (SEC-01 & SEC-02) COMPREHENSIVE REFINED IMPLEMENTATION PLAN
**Document:** Final Technical Investigation, Exact Execution Pipelines, Concurrency Lock Protocol & Refined Architecture  
**Date:** November 06, 2026  
**Author:** Senior Django Backend Developer & Application Security Engineer  
**Approved Tasks:** SEC-01 (Customer Account Deletion Data Preservation) & SEC-02 (UPI QR Fallback Correction)  
**Status:** REFINED PLAN AWAITING IMPLEMENTATION APPROVAL (Strict Read-Only Mode Maintained)  
**Language:** Simple Manglish with Technical English References  

---

## 1. SEC-01: RECONCILING ACTIVE-BOOKING RESTRICTIONS & RECURRING BOOKINGS

### 1.1 Strict Definition of Booking Statuses During Account Deletion
To prevent any ambiguity or dilution of consumer and worker protections, the system enforces three distinct status categories during account deletion:

| Category | Statuses Included | Rule on Customer Deletion Request | Justification |
|---|---|---|---|
| **Category A: Hard Deletion Blockers** | `IN_PROGRESS`, `COMPLETION_REQUESTED`, `DISPUTED` | **DELETION PERMANENTLY BLOCKED** | A worker is currently at the customer's property, completing labor, or in dispute resolution. Customer cannot delete account until resolved. |
| **Category B: Standard Single Bookings** | `PENDING`, `ACCEPTED` (Non-recurring) | **If `ACCEPTED`: BLOCKED.**<br>**If `PENDING`: CANCELLED.** | A standard individual accepted booking represents an agreed upcoming appointment with an assigned worker. Deleting account while an individual confirmed appointment is pending blocks deletion. Unassigned `PENDING` bookings are safely cancelled. |
| **Category C: Recurring Booking Occurrences** | `PENDING`, `ACCEPTED` (Tied to a `BookingSeries`) | **Future Occurrences (`date >= today`): CANCELLED.**<br>**Past Occurrences (`date < today` / `COMPLETED`): 100% PRESERVED.** | In recurring series (e.g. weekly cleaning for 3 months), future occurrences may have been auto-accepted by the worker in advance. Deactivating the series safely cancels upcoming occurrences with full refund checks and worker notifications, while never touching historical work. |

### 1.2 Reconciliation Logic:
1. **Pre-Check in `delete_account_view` & `admin_delete_user`:**
   Check for blocking bookings:
   ```python
   # 1. Any in-progress, completion requested, or disputed booking blocks deletion unconditionally
   has_critical_jobs = Booking.objects.filter(
       customer=locked_user,
       status__in=['IN_PROGRESS', 'COMPLETION_REQUESTED', 'DISPUTED']
   ).exists()
   if has_critical_jobs:
       messages.error(request, 'Cannot delete account while you have in-progress, awaiting completion, or disputed bookings.')
       return redirect('edit_profile')

   # 2. Any standalone non-recurring booking that has already been ACCEPTED blocks deletion
   has_accepted_standalone = Booking.objects.filter(
       customer=locked_user,
       series__isnull=True,
       status='ACCEPTED'
   ).exists()
   if has_accepted_standalone:
       messages.error(request, 'Cannot delete account with confirmed upcoming bookings. Please cancel your scheduled bookings first.')
       return redirect('edit_profile')
   ```
2. **Cancellation of Open Requests:**
   - Any standalone `PENDING` bookings are cancelled.
   - Any recurring series (`BookingSeries`) are marked `is_active = False`.
   - All future child bookings belonging to customer recurring series with `status in ['PENDING', 'ACCEPTED', 'EMERGENCY_PENDING']` and `date >= today` are safely cancelled via `_cancel_single_booking_internal()`.
   - Completed bookings (`status='COMPLETED'`) are never touched.

---

## 2. SEC-01: WORKER ACCEPTANCE CONCURRENCY & LOCK PROTOCOL

### 2.1 Worker-Side Schedule Change Endpoints Inspected
We inspected all worker status-changing endpoints:
- `worker_accept_booking()` (Line 4386): Locks `WorkerProfile.select_for_update()` and `Booking.select_for_update()`.
- `worker_accept_emergency()` (Line 4587): Locks `WorkerProfile.select_for_update()` and `Booking.select_for_update()`.
- `worker_propose_reschedule()` (Line 13028): Read-only status verification (does not change booking status).
- `customer_accept_reschedule()` (Line 13099): Locks `Booking.select_for_update()` and `WorkerProfile.select_for_update()`.

### 2.2 Concurrency Race Analysis:
What happens if Customer C requests account deletion while Worker W simultaneously attempts to accept Customer C's `PENDING` booking?
- In `worker_accept_booking()`, line 4411 locks `Booking.objects.select_for_update()`.
- If `_anonymize_and_soft_delete_customer()` also locks `Booking.objects.select_for_update().filter(customer=locked_user, status='PENDING')`:
  - Worker's acceptance thread and Customer's deletion thread serialize on the **`Booking` row lock**!
  - If Customer's deletion transaction acquires the lock first:
    The booking transitions to `status = 'CANCELLED'`. When Worker's thread acquires the lock, line 4415 executes:
    `if booking.status != 'PENDING': messages.warning(request, 'This booking has already been processed.'); return redirect(...)`. Worker acceptance is cleanly rejected!
  - If Worker's acceptance transaction acquires the lock first:
    The booking transitions to `status = 'ACCEPTED'`. When Customer's deletion transaction evaluates `has_accepted_standalone`, it detects the newly committed `ACCEPTED` status, aborts deletion, and returns a friendly error!

### 2.3 The Smallest Safe Correction:
In `_anonymize_and_soft_delete_customer()`:
1. `with transaction.atomic():`
2. Lock user row: `locked_user = CustomUser.objects.select_for_update().get(id=user.id, role='USER')`.
3. Lock open bookings: `open_bookings = list(Booking.objects.select_for_update().filter(customer=locked_user, status__in=['PENDING', 'ACCEPTED', 'EMERGENCY_PENDING', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'DISPUTED']))`.
4. Perform state evaluation under the lock:
   - If any booking is `IN_PROGRESS`, `COMPLETION_REQUESTED`, `DISPUTED`, or standalone `ACCEPTED`: Raise error / redirect.
   - For all remaining eligible `PENDING` or recurring child `ACCEPTED` bookings: cancel them.
5. In `worker_accept_booking()`:
   Add defensive check: `if not booking.customer.is_active: messages.error(request, 'Customer account is no longer active.'); return redirect(...)`.

---

## 3. SEC-01: MULTI-DEVICE SESSION INVALIDATION

### 3.1 Django Session & Authentication Architecture
- `settings.py` includes `django.contrib.auth.middleware.AuthenticationMiddleware` and `django.contrib.sessions.middleware.SessionMiddleware`.
- In Django, session data is stored in the database (`django_session` table) keyed by session cookie.
- When `user.is_active = False` is saved:
  - Django's `AuthenticationMiddleware` on every incoming HTTP request calls `request.user = auth.get_user(request)`.
  - In Django's `ModelBackend`, `get_user(user_id)` fetches the user from the database and checks `user.is_active`.
  - **If `user.is_active is False`, Django automatically treats `request.user` as an `AnonymousUser` across ALL existing sessions and devices!**
- Calling `user.set_unusable_password()` also scrambles the password hash. Django's session hash check (`AbstractBaseUser.get_session_auth_hash()`) immediately fails for any session holding the old hash.
- To provide complete multi-device destruction, we additionally query and flush all active session keys tied to this user:
  ```python
  from django.contrib.sessions.models import Session
  for s in Session.objects.all():
      try:
          data = s.get_decoded()
          if str(data.get('_auth_user_id')) == str(user_id):
              s.delete()
      except Exception:
          pass
  ```
- **Outcome:** The deleted customer is instantly logged out across all browsers, mobile devices, and active tabs.

---

## 4. SEC-01: CANCELLATION HELPER SAFETY & SIDE EFFECTS VERIFICATION

### 4.1 Verification of `_cancel_single_booking_internal()`
We audit the reusable cancellation helper against every potential edge case:
1. **Duplicate Cancellation Prevention:**
   Guard placed at start of helper:
   `if booking.status == 'CANCELLED': return`.
2. **Duplicate Refund Prevention:**
   In `_handle_booking_cancellation_refund(booking)`:
   - Evaluates `if not is_paid: return`.
   - Checks `payment.status`. If already `'REFUNDED'`, it does not duplicate the refund.
3. **Bookings Without Assigned Workers (Unassigned Emergency / Pending):**
   - Wrapped with `if booking.worker and booking.worker.user:`. If unassigned, worker notifications and emails are safely skipped.
4. **Schedule De-allocation:**
   - In LAMO, worker calendar conflict checking is driven by `ACTIVE_BOOKING_STATUSES = ('PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED')` in `has_worker_schedule_conflict()` (Line 675).
   - Setting `status = 'CANCELLED'` automatically removes the booking from active statuses, immediately opening the worker's slot in real-time. No manual schedule table cleanup required.
5. **Transaction Rollback Safety:**
   - Booking updates, status transitions, and refund records are within `transaction.atomic()`. If an error occurs, all DB changes roll back.
   - Notifications and emails use `_create_idempotent_notification()` and `async_send=True`, preventing duplicate alerts.

---

## 5. SEC-01: PROFILE IMAGE POST-COMMIT CLEANUP

### 5.1 Storage-Agnostic Post-Commit Hook:
```python
picture_to_delete = None
if hasattr(locked_user, 'user_profile') and locked_user.user_profile:
    prof = locked_user.user_profile
    if prof.profile_picture:
        picture_to_delete = prof.profile_picture
        prof.profile_picture = None
    prof.phone = ''
    prof.address = ''
    prof.latitude = None
    prof.longitude = None
    prof.save()

if picture_to_delete:
    def _safe_delete_avatar(img=picture_to_delete, uid=locked_user.id):
        try:
            img.delete(save=False)
            logger.info("Avatar asset successfully deleted for anonymized user %s", uid)
        except Exception as ex:
            logger.error("Failed to delete avatar file for user %s: %s", uid, str(ex))

    transaction.on_commit(_safe_delete_avatar)
```
- **Guarantees:** If the transaction rolls back, `_safe_delete_avatar` is never called. If it commits, the file asset is removed cleanly.

---

## 6. SEC-02: UPI QR FALLBACK CORRECTION DETAILS (RE-AFFIRMED)

- In `lamo/lamoapp/views.py` (`get_booking_upi_payment_data`):
  - Completely remove the `{phone}@upi` fallback (Lines 10513–10516).
  - Explicitly require `worker.upi_id`:
    ```python
    worker_upi = (getattr(worker, 'upi_id', '') or '').strip()
    if not worker_upi:
        return None
    ```
  - If `worker.upi_id` is empty, returns `None`.
  - In `lamo/templates/user/my_bookings.html`, the existing `{% else %}` block displays:
    `Ask worker for their GPay / PhonePe QR code in person.`
  - Legitimate UPI IDs, Cash payments, and commission calculations remain 100% untouched.

---

## 7. COMPLETE REGRESSION TEST PLAN

A dedicated test suite `test_section13_security_fixes.py` will verify:
1. `test_customer_soft_delete_preserves_completed_bookings`: Completed bookings and payments survive customer account deletion.
2. `test_customer_soft_delete_anonymizes_core_identity`: User PII scrubbed; username/email randomized.
3. `test_customer_deletion_blocked_on_critical_jobs`: Deletion blocked if `IN_PROGRESS`, `COMPLETION_REQUESTED`, `DISPUTED`, or standalone `ACCEPTED`.
4. `test_recurring_future_accepted_bookings_cancelled`: Future accepted occurrences of a recurring series are cancelled with worker notification and refund.
5. `test_worker_cannot_accept_booking_of_deleted_customer`: Worker acceptance rejects if customer was deactivated in a race condition.
6. `test_session_invalidation_across_devices`: Deactivated user's session cookies are rejected by `AuthenticationMiddleware`.
7. `test_upi_qr_generated_only_for_configured_upi`: Worker with `upi_id` renders QR; worker with phone only returns `None`.

---

## 8. FINAL APPROVAL REQUEST & STOP NOTICE

The plan now completely reconciles:
- ✅ Active-booking restrictions vs. future recurring cancellation.
- ✅ Concurrency locking between worker acceptance and account deletion.
- ✅ Multi-device session invalidation.
- ✅ Verified single-booking cancellation helper with zero side-effect duplication.
- ✅ Post-commit avatar asset cleanup.
- ✅ Removal of unverified `{phone}@upi` fallback.

**Strict Protocol Status:** No source code or migrations have been modified. I have stopped here and will wait for your explicit approval to implement.
