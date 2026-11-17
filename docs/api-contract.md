# Frontend HTTP and WebSocket contract

This refines the approved `.scratch/mvp/spec.md` for backend integration. All JSON keys use snake_case. UUIDs are strings, timestamps ISO8601 with timezone, currency TWD integers. Business routes require Authorization: Bearer token. Error: `{code,message,request_id,details?}`. Collections: `{items:[],next_cursor:null|string}`.

| Route | Request / response used by UI |
| --- | --- |
| POST /api/v1/auth/login | `{email,password}` → `{token,user:{id,email,role}}` |
| POST /api/v1/auth/register | `{email,password}` → user; UI then performs login |
| GET /api/v1/auth/me | user `{id,email,role}` |
| POST /api/v1/auth/logout | any successful 2xx |
| GET /api/v1/events | params category,venue,status,sort,cursor,snapshot_id; → collection plus snapshot_id |
| POST /api/v1/events | `{title,description,category,venue,sales_start,sales_end,zones:[{code,name,price_twd}]}` → event |
| GET /api/v1/events/{id} | event `{id,owner_id,title,description,category,venue,status,version,sales_start,sales_end,zones:[{id,code,name,price_twd}]}` |
| PATCH /api/v1/events/{id} | editable fields; If-Match: version → updated event |
| POST /api/v1/events/{id}/publish | If-Match: version → updated event |
| DELETE /api/v1/events/{id} | successful 2xx |
| GET /api/v1/events/{id}/seats | optional zone_id → `{version,items:[{id,zone_id,row_label,seat_number,status,held_until?}]}` |
| GET /api/v1/events/{id}/version | `{version}` |
| POST /api/v1/events/{id}/bookings | `{seat_ids:[id]}` OR `{zone_id,quantity}`; Idempotency-Key → order |
| GET /api/v1/orders | order collection |
| GET /api/v1/orders/{id} | order `{id,event_id,status,ticket_count,subtotal_twd,fee_twd,total_twd,expires_at,created_at,payments?:[{id,status}],items?:[{seat_id,zone_code_snapshot,row_label_snapshot,seat_number_snapshot,unit_price_twd}]}` |
| POST /api/v1/orders/{id}/cancel | → order |
| POST /api/v1/orders/{id}/payments | `{outcome:success|failure,delay_seconds:0}`; Idempotency-Key → payment `{id,status}` |
| POST /api/v1/events/{id}/seats/import | multipart `file`, `mode` all_or_nothing or skip_invalid → job |
| GET /api/v1/imports/{id} | job `{id,status,total_rows,processed_rows,inserted_rows,error_rows,failure_code?}` |
| GET /api/v1/imports/{id}/errors | collection `{items:[{row_number,source_line?,code,reason}]}` |
| POST /api/v1/imports/{id}/retry | → job |
| GET /api/v1/events/{id}/report | XLSX binary |
| GET /api/v1/admin/metrics | `{items:[{bucket_at,rps,p50_ms,p95_ms,p99_ms,error_rate,connections}],data_complete?:boolean}`; error_rate ratio 0–1 |
| GET /api/v1/admin/alerts | alert collection `{id,rule_id,status,triggered_at,details}` |
| GET /api/v1/admin/users | user collection `{id,email,role,is_active}` |
| PATCH /api/v1/admin/users/{id} | `{role,is_active}` → user |
| GET /api/v1/admin/logs/{request_id} | log collection `{service,level,code,observed_at,context}` |

WS `/ws/events/{event_id}`: client first frame `{type:"auth",token}`; server acknowledges `{type:"auth_ok"}`. Client then loads HTTP seat snapshot while buffering changes. Change frame `{type:"seat_changed",version,changes:[seat patch]}`. Heartbeat `{type:"heartbeat",version}` (at least every 5s). Client accepts only the next version, ignores duplicates, reloads snapshot on gaps, pauses selection on disconnect and retries connection. Optional client `{type:"heartbeat"}` requests version check.

GET collection pagination retains snapshot_id and next_cursor. 410 SNAPSHOT_EXPIRED triggers explicit refresh. Monetary calculation remains server authoritative. All mutation retries reuse the pending action's Idempotency-Key until acknowledged.
