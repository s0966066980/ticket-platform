"""Import behavior observed through HTTP and the worker's public entry point."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clear_settings_after_test():
    yield
    from app.config import get_settings
    get_settings.cache_clear()


def setup_activity(client, tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    get_settings.cache_clear()
    email = 'imports@example.test'
    assert client.post('/api/v1/auth/register', json={'email': email, 'password': 'Import-test-123!'}).status_code == 201
    login = client.post('/api/v1/auth/login', json={'email': email, 'password': 'Import-test-123!'}).json()
    headers = {'Authorization': 'Bearer ' + login['token']}
    now = datetime.now(timezone.utc)
    response = client.post('/api/v1/events', headers=headers, json={
        'title': '匯入測試', 'sales_start': (now-timedelta(hours=1)).isoformat(),
        'sales_end': (now+timedelta(days=1)).isoformat(),
        'zones': [{'code': 'A', 'name': 'A 區', 'price_twd': 100}],
    })
    assert response.status_code == 201
    return response.json()['id'], headers


def run_worker():
    from app.database import get_engine
    from app.imports import process_import_jobs
    return process_import_jobs(sessionmaker(get_engine(), expire_on_commit=False), limit=10)


def test_same_content_reuses_job_and_original_mode_and_worker_adds_seats(client, tmp_path, monkeypatch):
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    data = b'zone_code,row_label,seat_number\nA,1,1\nA,1,2\n'
    first = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}, data={'mode': 'all_or_nothing'})
    assert first.status_code == 202
    second = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('same.csv', data)}, data={'mode': 'skip_invalid'})
    assert second.status_code == 202
    assert second.json()['id'] == first.json()['id']
    assert second.json()['mode'] == 'all_or_nothing'
    assert run_worker() == 1
    job = client.get('/api/v1/imports/' + first.json()['id'], headers=headers).json()
    assert job['status'] == 'SUCCEEDED'
    assert job['inserted_rows'] == 2
    seats = client.get(f'/api/v1/events/{activity}/seats', headers=headers).json()['items']
    assert {(seat['row_label'], seat['seat_number']) for seat in seats} == {('1', '1'), ('1', '2')}


@pytest.mark.parametrize('mode,status,inserted', [('all_or_nothing', 'FAILED_VALIDATION', 0), ('skip_invalid', 'SUCCEEDED', 1)])
def test_duplicate_occurrences_and_unknown_zones_report_original_csv_lines(client, tmp_path, monkeypatch, mode, status, inserted):
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    data = b'zone_code,row_label,seat_number\nA,1,1\nA,1,1\nA,1,2\nZ,9,1\nA,,3\n'
    job_id = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}, data={'mode': mode}).json()['id']
    assert run_worker() == 1
    job = client.get(f'/api/v1/imports/{job_id}', headers=headers).json()
    assert (job['status'], job['inserted_rows'], job['error_rows']) == (status, inserted, 4)
    errors = client.get(f'/api/v1/imports/{job_id}/errors', headers=headers).json()['items']
    assert {error['source_line'] for error in errors} == {2, 3, 5, 6}
    assert {error['code'] for error in errors} == {'DUPLICATE_IN_FILE', 'UNKNOWN_ZONE', 'INVALID_ROW'}
    assert len(client.get(f'/api/v1/events/{activity}/seats', headers=headers).json()['items']) == inserted


def test_streaming_json_accepts_numeric_seat_numbers_and_rejects_non_array_file(client, tmp_path, monkeypatch):
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    valid = b'[{"zone_code":"A","row_label":"1","seat_number":1}]'
    first = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.json', valid)}).json()['id']
    run_worker()
    assert client.get(f'/api/v1/imports/{first}', headers=headers).json()['inserted_rows'] == 1
    invalid = b'{"item":{"zone_code":"A","row_label":"1","seat_number":2}}'
    second = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.json', invalid)}).json()['id']
    run_worker()
    job = client.get(f'/api/v1/imports/{second}', headers=headers).json()
    assert (job['status'], job['failure_code']) == ('FAILED_VALIDATION', 'INVALID_JSON_ARRAY')
    assert len(client.get(f'/api/v1/events/{activity}/seats', headers=headers).json()['items']) == 1


def test_row_and_byte_limits_do_not_commit_partial_inventory(client, tmp_path, monkeypatch):
    from app.config import get_settings
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    data = b'zone_code,row_label,seat_number\nA,1,1\nA,1,2\nA,1,3\n'
    monkeypatch.setenv('IMPORT_MAX_BYTES', '10')
    get_settings.cache_clear()
    assert client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}).status_code == 413
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv('IMPORT_MAX_BYTES', '20000000')
    monkeypatch.setenv('IMPORT_MAX_ROWS', '2')
    get_settings.cache_clear()
    job_id = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}, data={'mode': 'skip_invalid'}).json()['id']
    run_worker()
    assert client.get(f'/api/v1/imports/{job_id}', headers=headers).json()['failure_code'] == 'ROW_LIMIT_EXCEEDED'
    assert client.get(f'/api/v1/events/{activity}/seats', headers=headers).json()['items'] == []


def test_expired_generation_cannot_commit_after_another_worker_claims(client, db_session, tmp_path, monkeypatch):
    from sqlalchemy import update
    from app.database import get_engine
    from app.imports import claim_import_job, process_import_job
    from app.models import ImportJob
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    data = b'zone_code,row_label,seat_number\nA,1,1\n'
    job_id = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}).json()['id']
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    with factory() as session:
        old_claim = claim_import_job(session, 'old-worker')
    db_session.execute(update(ImportJob).values(lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
    db_session.commit()
    with factory() as session:
        new_claim = claim_import_job(session, 'new-worker')
    process_import_job(factory, *old_claim)
    assert client.get(f'/api/v1/imports/{job_id}', headers=headers).json()['inserted_rows'] == 0
    process_import_job(factory, *new_claim)
    assert client.get(f'/api/v1/imports/{job_id}', headers=headers).json()['inserted_rows'] == 1


def test_skip_invalid_retry_resumes_committed_checkpoint_without_duplicating_seats(client, tmp_path, monkeypatch):
    from sqlalchemy import event as sa_event
    from app.config import get_settings
    from app.database import get_engine
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    monkeypatch.setenv('IMPORT_BATCH_SIZE', '1')
    get_settings.cache_clear()
    data = b'zone_code,row_label,seat_number\nA,1,1\nA,1,2\n'
    job_id = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}, data={'mode': 'skip_invalid'}).json()['id']
    inserts = 0

    def interrupt_second_batch(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if statement.startswith('INSERT INTO seats '):
            inserts += 1
            if inserts == 2:
                raise RuntimeError('simulated worker interruption')

    engine = get_engine()
    sa_event.listen(engine, 'before_cursor_execute', interrupt_second_batch)
    try:
        run_worker()
    finally:
        sa_event.remove(engine, 'before_cursor_execute', interrupt_second_batch)
    failed = client.get(f'/api/v1/imports/{job_id}', headers=headers).json()
    assert (failed['status'], failed['phase'], failed['inserted_rows']) == ('FAILED_SYSTEM', 'APPLYING', 1)
    assert client.post(f'/api/v1/imports/{job_id}/retry', headers=headers).status_code == 202
    run_worker()
    done = client.get(f'/api/v1/imports/{job_id}', headers=headers).json()
    assert (done['status'], done['inserted_rows'], done['error_rows']) == ('SUCCEEDED', 2, 0)
    assert len(client.get(f'/api/v1/events/{activity}/seats', headers=headers).json()['items']) == 2


def test_publication_waits_for_import_and_published_activity_rejects_new_inventory(client, tmp_path, monkeypatch):
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    data = b'zone_code,row_label,seat_number\nA,1,1\n'
    job_id = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}).json()['id']
    publishing = {**headers, 'If-Match': '1'}
    assert client.post(f'/api/v1/events/{activity}/publish', headers=publishing).status_code == 409
    run_worker()
    assert client.post(f'/api/v1/events/{activity}/publish', headers=publishing).status_code == 200
    repeated = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)})
    assert repeated.status_code == 202
    assert repeated.json()['id'] == job_id
    changed = b'zone_code,row_label,seat_number\nA,1,2\n'
    assert client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('new.csv', changed)}).status_code == 409


def test_accepted_job_finishes_after_submitter_disabled_and_current_permissions_protect_results(client, db_session, tmp_path, monkeypatch):
    from uuid import UUID
    from sqlalchemy import update
    from app.models import User
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    owner = client.get('/api/v1/auth/me', headers=headers).json()['id']
    data = b'zone_code,row_label,seat_number\nA,1,1\n'
    job_id = client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)}).json()['id']
    registered = client.post('/api/v1/auth/register', json={'email': 'admin-import@example.test', 'password': 'Import-test-123!'})
    admin_id = UUID(registered.json()['id'])
    db_session.execute(update(User).where(User.id == admin_id).values(role='ADMIN'))
    db_session.commit()
    token = client.post('/api/v1/auth/login', json={'email': 'admin-import@example.test', 'password': 'Import-test-123!'}).json()['token']
    admin = {'Authorization': 'Bearer ' + token}
    assert client.patch('/api/v1/admin/users/' + owner, headers=admin, json={'is_active': False}).status_code == 200
    assert run_worker() == 1
    assert client.get(f'/api/v1/imports/{job_id}', headers=headers).status_code == 401
    result = client.get(f'/api/v1/imports/{job_id}', headers=admin).json()
    assert (result['status'], result['inserted_rows']) == ('SUCCEEDED', 1)


def test_concurrent_same_file_uploads_share_one_job_and_one_durable_source(client, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    data = b'zone_code,row_label,seat_number\nA,1,1\n'
    def upload():
        return client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('seats.csv', data)})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(), range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json()['id'] == responses[1].json()['id']
    assert len(list(tmp_path.glob('*.bin'))) == 1
    assert run_worker() == 1


def test_validation_stays_fast_when_planner_statistics_describe_only_an_earlier_job(client, db_session, tmp_path, monkeypatch):
    """Regression: a 100K JSON import hung in VALIDATING for minutes during a load
    run. Autovacuum had analyzed import_rows while it held only the previous job's
    rows, so the planner estimated ~1 row for the new job_id and validated
    duplicates with a nested loop (rows x rows). Validation must refresh statistics."""
    import time
    from sqlalchemy import text
    activity, headers = setup_activity(client, tmp_path, monkeypatch)
    rows = 15000
    first = 'zone_code,row_label,seat_number\n' + ''.join(f'A,{i // 100},{i % 100}\n' for i in range(rows))
    assert client.post(f'/api/v1/events/{activity}/seats/import', headers=headers, files={'file': ('one.csv', first.encode())}, data={'mode': 'all_or_nothing'}).status_code == 202
    assert run_worker() == 1
    db_session.execute(text('ANALYZE import_rows'))  # what autovacuum did between the two jobs
    db_session.commit()
    now = datetime.now(timezone.utc)
    second_activity = client.post('/api/v1/events', headers=headers, json={
        'title': 'second', 'sales_start': (now-timedelta(hours=1)).isoformat(), 'sales_end': (now+timedelta(days=1)).isoformat(),
        'zones': [{'code': 'A', 'name': 'A', 'price_twd': 100}]}).json()['id']
    second = 'zone_code,row_label,seat_number\n' + ''.join(f'A,{i // 100},{i % 100}\n' for i in range(rows))
    job = client.post(f'/api/v1/events/{second_activity}/seats/import', headers=headers, files={'file': ('two.csv', second.encode())}, data={'mode': 'all_or_nothing'}).json()
    started = time.monotonic()
    assert run_worker() == 1
    elapsed = time.monotonic() - started
    assert client.get('/api/v1/imports/' + job['id'], headers=headers).json()['inserted_rows'] == rows
    assert elapsed < 15, f'second import took {elapsed:.1f}s: validation fell back to a nested loop'
