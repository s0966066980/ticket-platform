# 06: Atomic reservations, quota, cancellation and idempotency

Status: claimed
Triage: ready-for-agent
Blocked by: 05

## Spec

Read ../spec.md and referenced approved documents.

## Ownership and acceptance

backend/app/booking.py, app/idempotency.py, api/bookings.py, api/orders.py, tests/test_booking.py. Full transactional flow, quota mutex, expiry-aware seat reclaim, region DB allocation, DB clock, prices, outbox, concurrent PG16 tests and idempotency leases.

Use tdd red-green vertical slices at approved public seams. You are not alone in the codebase: preserve others' changes. Implement in your own worktree/branch from integration/mvp; merge its latest tip before reporting. Do not mark this ticket resolved yourself; report commit, checks and remaining limitations to merger.

## Answer

Implemented atomic named-seat and database SKIP LOCKED region reservations, expiry-aware reclaim, account/event quota mutex with authoritative active-plus-paid calculation, integer 5% fee, terminal cancellation and duplicate-safe expiry. All reservation time checks occur after quota, seat and final event-stream locks; fresh event SELECT verifies deletion/sales state. Old order release compares current_order_id and only touches already locked item seats.

Durable idempotency uses independent short claim transactions, generation-fenced main transactions, atomic response/business commit, completed replay, in-progress Retry-After and different-body conflicts. Expected business rejections are saved; database failures release only unfinished claims and never overwrite an uncertain successful commit.

Shared interfaces: app.booking.lock_quota(session,user_id,event_id), lock_order_seats(session,order), load_seat_changes(session,seat_ids), emit_seat_changes(session,event_id,locked_stream,changes,request_id), order_json, get_order, expire_order(session,order_id,request_id='expiry-worker'), expire_due_orders(limit=100,event_id=None). Event payload is {type:seat_changed,event_id,version,changes:[Core seat projections]}, persisted identically in SeatEvent and Outbox.

app.idempotency exports Claim, OperationResult(status,body,headers), prepare_claim, claim_key, lock_claim, complete_key, reject_key and release_key. prepare_claim must be called before writes/locks. Authorized shared fixes: AppError now accepts keyword-only headers and preserves Retry-After; current_principal releases its read-only transaction after copying the immutable Principal, preventing saturated pools from retaining one connection while requesting another for claims/snapshots.

Compilation and whitespace checks pass. HTTP/PG tests are supplied for all main paths and races, including single-connection pool, but this branch's actual pytest execution was blocked by PyPI DNS. Integration baseline CI has run separately; new ticket06 tests need its next CI run before claiming runtime validation. No schema changes.
