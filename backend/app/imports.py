"""Durable, fenced seat imports. No worker relies on the submitter staying active."""
from __future__ import annotations

import csv
from datetime import timedelta
import fcntl
import hashlib
import io
import logging
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, Callable
from uuid import UUID, uuid4

import ijson
from ijson.common import JSONError
from sqlalchemy import Integer, Uuid, and_, column, delete, func, insert, literal, or_, select, tuple_, update, values, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import get_settings
from app.errors import AppError
from app.events import get_event
from app.models import Event, EventStream, ImportError as RowError, ImportJob, ImportRow, OutboxEvent, Seat, SeatEvent, Zone

SessionFactory = Callable[[], Session]
LEASE_SECONDS = 60
FIELDS = ('zone_code', 'row_label', 'seat_number')
_LOG = logging.getLogger(__name__)


class LostLease(Exception):
    pass


class InvalidSource(Exception):
    def __init__(self, code, reason, row_number=None, source_line=None):
        self.code, self.reason = code, reason
        self.row_number, self.source_line = row_number, source_line
        super().__init__(reason)


def job_json(job: ImportJob) -> dict:
    return {
        'id': str(job.id), 'event_id': str(job.event_id), 'status': job.status,
        'phase': job.phase, 'mode': job.mode.lower(), 'format': job.format.lower(),
        'total_rows': job.total_rows, 'processed_rows': job.processed_rows,
        'inserted_rows': job.inserted_rows, 'error_rows': job.error_rows,
        'failure_code': job.failure_code,
    }


def _save_source(source: BinaryIO) -> tuple[Path, str]:
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='ticket-upload-', suffix='.upload', dir=settings.upload_dir)
    temporary = Path(name)
    final = settings.upload_dir / f'{uuid4().hex}.bin'
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, 'wb') as output:
            while chunk := source.read(65536):
                size += len(chunk)
                if size > settings.import_max_bytes:
                    raise AppError(413, 'FILE_TOO_LARGE', 'Seat file exceeds the configured byte limit')
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final)
        directory = os.open(settings.upload_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return final, digest.hexdigest()
    except BaseException:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise


def accept_import(session: Session, principal: Principal, event_id: UUID, source: BinaryIO, filename: str, mode: str, request_id: str) -> dict:
    mode = mode.upper()
    if mode not in {'ALL_OR_NOTHING', 'SKIP_INVALID'}:
        raise AppError(422, 'INVALID_IMPORT_MODE', 'Choose all_or_nothing or skip_invalid')
    extension = Path(filename).suffix.lower()
    if extension not in {'.csv', '.json'}:
        raise AppError(422, 'INVALID_IMPORT_FORMAT', 'Provide a CSV or JSON file')
    # Avoid holding the activity metadata lock while receiving/copying the file.
    get_event(session, event_id, principal, manage=True)
    session.rollback()
    path, digest = _save_source(source)
    owned = True
    guard = path.open('rb')
    try:
        # GC takes an exclusive nonblocking flock before checking references.
        # Keep the durable file protected through an ambiguous DB commit too.
        fcntl.flock(guard.fileno(), fcntl.LOCK_SH)
        if not path.exists():
            raise OSError('Upload expired before it could be accepted')
        event = get_event(session, event_id, principal, manage=True, lock=True)
        existing = session.scalar(select(ImportJob).where(ImportJob.event_id == event_id, ImportJob.file_hash == digest))
        if existing is not None:
            result = job_json(existing)
            session.rollback()
            return result
        if event.status != 'DRAFT':
            raise AppError(409, 'ACTIVITY_FROZEN', 'Seats may only be imported before publication')
        accepted_at = session.scalar(select(func.clock_timestamp()))
        job = ImportJob(
            id=uuid4(), event_id=event_id, submitted_by=principal.id,
            authorization_snapshot={'user_id': str(principal.id), 'role': principal.role,
                'auth_version': principal.auth_version, 'session_id': str(principal.session_id),
                'event_owner_id': str(event.owner_id), 'accepted_at': accepted_at.isoformat()},
            file_hash=digest, file_path=str(path), original_name=filename,
            format=extension[1:].upper(), mode=mode, request_id=request_id,
        )
        session.add(job)
        session.flush()
        result = job_json(job)
        owned = False
        # Preserve the durable file even if the commit acknowledgement is lost;
        # an orphan is recoverable, a committed job whose file vanished is not.
        session.commit()
        return result
    finally:
        guard.close()
        if owned:
            path.unlink(missing_ok=True)


def get_import(session: Session, principal: Principal, job_id: UUID) -> ImportJob:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise AppError(404, 'IMPORT_NOT_FOUND', 'Import job not found')
    get_event(session, job.event_id, principal, manage=True)
    return job


def retry_import(session: Session, principal: Principal, job_id: UUID) -> dict:
    event_id = session.scalar(select(ImportJob.event_id).where(ImportJob.id == job_id))
    if event_id is None:
        raise AppError(404, 'IMPORT_NOT_FOUND', 'Import job not found')
    event = get_event(session, event_id, principal, manage=True, lock=True)
    job = session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
    if event.status != 'DRAFT':
        raise AppError(409, 'ACTIVITY_FROZEN', 'Published inventory cannot be changed')
    if job.status != 'FAILED_SYSTEM':
        raise AppError(409, 'IMPORT_NOT_RETRYABLE', 'Only a transient system failure can retry the original job')
    job.status, job.failure_code, job.finished_at = 'QUEUED', None, None
    job.generation += 1
    job.lease_until, job.lease_owner = None, None
    session.flush()
    result = job_json(job)
    session.commit()
    return result


def claim_import_job(session: Session, worker_id: str) -> tuple[UUID, int] | None:
    now = session.scalar(select(func.clock_timestamp()))
    job = session.scalar(select(ImportJob).where(or_(
        ImportJob.status == 'QUEUED',
        and_(ImportJob.status == 'RUNNING', or_(ImportJob.lease_until <= now, ImportJob.lease_until.is_(None))),
    )).order_by(ImportJob.created_at, ImportJob.id).with_for_update(skip_locked=True).limit(1))
    if job is None:
        session.rollback()
        return None
    job.status = 'RUNNING'
    if job.phase == 'QUEUED':
        job.phase = 'PARSING'
    job.generation += 1
    job.lease_owner, job.lease_until = worker_id, now + timedelta(seconds=LEASE_SECONDS)
    result = (job.id, job.generation)
    session.commit()
    return result


def _locked_job(session, job_id, generation):
    event_id = session.scalar(select(ImportJob.event_id).where(ImportJob.id == job_id))
    if event_id is None:
        raise LostLease()
    event = session.scalar(select(Event).where(Event.id == event_id).with_for_update(key_share=True))
    job = session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update().execution_options(populate_existing=True))
    now = session.scalar(select(func.clock_timestamp()))
    if job.status != 'RUNNING' or job.generation != generation or job.lease_until is None or job.lease_until <= now:
        raise LostLease()
    job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    return event, job


def _draft(event):
    if event is None or event.deleted_at is not None or event.status != 'DRAFT':
        raise InvalidSource('ACTIVITY_FROZEN', 'The activity is no longer an editable draft')


def _records(path, kind):
    if kind == 'CSV':
        with open(path, 'r', encoding='utf-8-sig', newline='') as source:
            reader = csv.reader(source, strict=True)
            header = next(reader, None)
            if header is None or len(header) != 3 or set(header) != set(FIELDS):
                raise InvalidSource('INVALID_CSV_HEADER', 'CSV header must contain zone_code,row_label,seat_number exactly', source_line=1)
            number = 0
            while True:
                start = reader.line_num + 1
                record = next(reader, None)
                if record is None:
                    break
                if not record:
                    continue
                number += 1
                yield number, start, dict(zip(header, record)) if len(record) == 3 else record
    else:
        with open(path, 'rb') as source:
            offset = 3 if source.read(3) == b'\xef\xbb\xbf' else 0
            source.seek(offset)
            token = source.read(1)
            while token and token.isspace():
                token = source.read(1)
            if token != b'[':
                raise InvalidSource('INVALID_JSON_ARRAY', 'JSON seat input must be a top-level array')
            source.seek(offset)
            for number, record in enumerate(ijson.items(source, 'item'), 1):
                yield number, None, record


def _coordinates(record):
    if not isinstance(record, dict) or set(record) != set(FIELDS):
        raise ValueError('Each row must contain exactly zone_code,row_label,seat_number')
    result = {}
    for field, maximum in zip(FIELDS, (50, 100, 100)):
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f'{field} must be text or an integer')
        value = str(value).strip()
        if not value or len(value) > maximum or '\x00' in value:
            raise ValueError(f'{field} must contain 1–{maximum} characters without NUL')
        result[field] = value
    return result


def _stage(factory, job_id, generation, metadata):
    settings = get_settings()
    digest, size = hashlib.sha256(), 0
    with open(metadata['file_path'], 'rb') as source:
        while chunk := source.read(65536):
            size += len(chunk)
            if size > settings.import_max_bytes:
                raise InvalidSource('FILE_TOO_LARGE', 'Source exceeds the configured byte limit')
            digest.update(chunk)
    if digest.hexdigest() != metadata['file_hash']:
        raise OSError('Persisted import source checksum does not match')
    with factory() as session:
        event, job = _locked_job(session, job_id, generation)
        _draft(event)
        if job.inserted_rows:
            raise OSError('Cannot reset staging after committed inventory')
        session.execute(delete(RowError).where(RowError.job_id == job_id))
        session.execute(delete(ImportRow).where(ImportRow.job_id == job_id))
        job.total_rows = job.processed_rows = job.error_rows = job.checkpoint = 0
        job.phase = 'PARSING'
        session.commit()
    rows, errors, total = [], [], 0

    def flush():
        if not rows:
            return
        with factory() as session:
            event, job = _locked_job(session, job_id, generation)
            _draft(event)
            session.execute(insert(ImportRow), rows)
            if errors:
                session.execute(insert(RowError), errors)
            job.processed_rows = job.total_rows = total
            job.error_rows += len(errors)
            session.commit()
        rows.clear()
        errors.clear()

    try:
        for number, line, record in _records(metadata['file_path'], metadata['format']):
            if number > settings.import_max_rows:
                raise InvalidSource('ROW_LIMIT_EXCEEDED', 'Source exceeds the configured row limit', number, line)
            total = number
            row = {'job_id': job_id, 'row_number': number, 'source_line': line, 'validation_state': 'PENDING'}
            try:
                row.update(_coordinates(record))
            except ValueError as error:
                row['validation_state'] = 'INVALID'
                errors.append({'id': uuid4(), 'job_id': job_id, 'row_number': number,
                    'source_line': line, 'code': 'INVALID_ROW', 'reason': str(error)})
            rows.append(row)
            if len(rows) >= settings.import_batch_size:
                flush()
        flush()
    except (csv.Error, UnicodeError, JSONError) as error:
        raise InvalidSource('MALFORMED_SOURCE', f'Cannot parse source: {str(error)[:300]}') from error
    with factory() as session:
        event, job = _locked_job(session, job_id, generation)
        _draft(event)
        job.total_rows = job.processed_rows = total
        job.phase = 'VALIDATING'
        session.commit()


def _mark_invalid(session, job_id, predicate, code, reason):
    matching = (ImportRow.job_id == job_id, ImportRow.validation_state.in_(['PENDING', 'VALID']), predicate)
    source = select(func.gen_random_uuid(), ImportRow.job_id, ImportRow.row_number,
        ImportRow.source_line, literal(code, type_=RowError.__table__.c.code.type),
        literal(reason, type_=RowError.__table__.c.reason.type)).where(*matching)
    session.execute(RowError.__table__.insert().from_select(['id', 'job_id', 'row_number', 'source_line', 'code', 'reason'], source))
    session.execute(update(ImportRow).where(*matching).values(validation_state='INVALID').execution_options(synchronize_session=False))


def _refresh_planner_statistics(session):
    """Re-analyze the tables validation joins before planning its set-based checks.

    Autovacuum may have analyzed import_rows while it held only an earlier job's
    rows; the planner then estimates ~1 row for this job and validates with a
    nested loop that is quadratic in the file size (a 100K-row import stalled for
    minutes). ANALYZE samples a bounded number of pages, so this stays cheap.
    """
    session.execute(text('ANALYZE import_rows'))
    session.execute(text('ANALYZE seats'))


def _validate(session, job, *, file_duplicates=True):
    if file_duplicates:
        duplicates = select(ImportRow.zone_code, ImportRow.row_label, ImportRow.seat_number).where(
            ImportRow.job_id == job.id, ImportRow.zone_code.is_not(None),
            ImportRow.row_label.is_not(None), ImportRow.seat_number.is_not(None),
        ).group_by(ImportRow.zone_code, ImportRow.row_label, ImportRow.seat_number).having(func.count() > 1)
        _mark_invalid(session, job.id, tuple_(ImportRow.zone_code, ImportRow.row_label, ImportRow.seat_number).in_(duplicates), 'DUPLICATE_IN_FILE', 'All occurrences of a duplicated seat in this file are invalid')
    known_zone = select(Zone.id).where(Zone.event_id == job.event_id, Zone.code == ImportRow.zone_code).correlate(ImportRow).exists()
    _mark_invalid(session, job.id, ~known_zone, 'UNKNOWN_ZONE', 'zone_code does not match a configured activity zone')
    existing = select(Seat.id).join(Zone, Zone.id == Seat.zone_id).where(
        Seat.event_id == job.event_id, Zone.code == ImportRow.zone_code,
        Seat.row_label == ImportRow.row_label, Seat.seat_number == ImportRow.seat_number,
    ).correlate(ImportRow).exists()
    _mark_invalid(session, job.id, existing, 'SEAT_EXISTS', 'Seat coordinates already exist in this activity')
    session.execute(update(ImportRow).where(ImportRow.job_id == job.id, ImportRow.validation_state == 'PENDING').values(validation_state='VALID').execution_options(synchronize_session=False))
    job.error_rows = session.scalar(select(func.count()).select_from(ImportRow).where(ImportRow.job_id == job.id, ImportRow.validation_state == 'INVALID'))


def _apply_batch(session, job):
    rows = session.execute(select(
        ImportRow.row_number, ImportRow.row_label, ImportRow.seat_number, Zone.id.label('zone_id'),
    ).join(Zone, and_(Zone.event_id == job.event_id, Zone.code == ImportRow.zone_code)).where(
        ImportRow.job_id == job.id, ImportRow.validation_state == 'VALID',
        ImportRow.row_number > job.checkpoint,
    ).order_by(ImportRow.row_number).limit(get_settings().import_batch_size)).mappings().all()
    if not rows:
        return 0
    records, applied = [], []
    for row in rows:
        seat_id = uuid4()
        records.append({'id': seat_id, 'event_id': job.event_id, 'zone_id': row['zone_id'],
            'row_label': row['row_label'], 'seat_number': row['seat_number'], 'status': 'AVAILABLE', 'version': 0})
        applied.append((row['row_number'], seat_id, row['zone_id']))
    session.execute(Seat.__table__.insert(), records)
    assignments = values(column('row_number', Integer), column('seat_id', Uuid), column('zone_id', Uuid), name='applied').data(applied)
    session.execute(update(ImportRow).where(ImportRow.job_id == job.id, ImportRow.row_number == assignments.c.row_number).values(
        validation_state='APPLIED', applied_seat_id=assignments.c.seat_id,
        resolved_zone_id=assignments.c.zone_id,
    ).execution_options(synchronize_session=False))
    job.checkpoint = rows[-1]['row_number']
    job.inserted_rows += len(rows)
    return len(rows)


def _emit_inventory(session, job):
    session.execute(pg_insert(EventStream).values(event_id=job.event_id).on_conflict_do_nothing(index_elements=['event_id']))
    stream = session.scalar(select(EventStream).where(EventStream.event_id == job.event_id).with_for_update())
    stream.current_version += 1
    payload = {'event_id': str(job.event_id), 'version': stream.current_version, 'changes': [], 'resync_required': True}
    session.add(SeatEvent(event_id=job.event_id, version=stream.current_version, changes=payload, request_id=job.request_id))
    session.add(OutboxEvent(id=uuid4(), kind='seat_changed', aggregate_id=job.event_id,
        aggregate_version=stream.current_version, payload=payload, request_id=job.request_id))


def _complete(session, job, status='SUCCEEDED', code=None):
    job.status, job.failure_code = status, code
    # FAILED_SYSTEM keeps APPLYING and its checkpoint for explicit retry.
    if status != 'FAILED_SYSTEM':
        job.phase = 'FINISHED'
    job.finished_at = session.scalar(select(func.clock_timestamp()))
    job.lease_owner, job.lease_until = None, None
    if status == 'SUCCEEDED':
        job.checkpoint = job.total_rows


def _apply(factory, job_id, generation, mode):
    with factory() as session:
        event, job = _locked_job(session, job_id, generation)
        _draft(event)
        # Revalidate at the final protected boundary, not only during parsing.
        _refresh_planner_statistics(session)
        _validate(session, job)
        if mode == 'ALL_OR_NOTHING':
            if job.error_rows:
                _complete(session, job, 'FAILED_VALIDATION', 'ROW_ERRORS')
            else:
                inserted = 0
                while count := _apply_batch(session, job):
                    inserted += count
                    session.flush()
                if inserted:
                    _emit_inventory(session, job)
                _complete(session, job)
            session.commit()
            return
        job.phase = 'APPLYING'
        session.commit()
    refreshed = False
    while True:
        with factory() as session:
            event, job = _locked_job(session, job_id, generation)
            _draft(event)
            if not refreshed:
                _refresh_planner_statistics(session)
                refreshed = True
            # Earlier committed rows are APPLIED, so recovery never mistakes
            # the same job's own checkpointed seats for a new duplicate upload.
            _validate(session, job, file_duplicates=False)
            count = _apply_batch(session, job)
            if count:
                _emit_inventory(session, job)
            else:
                _complete(session, job)
            session.commit()
            if not count:
                return


def _fail(factory, job_id, generation, error, validation):
    with factory() as session:
        _, job = _locked_job(session, job_id, generation)
        if validation:
            session.add(RowError(id=uuid4(), job_id=job_id, row_number=error.row_number,
                source_line=error.source_line, code=error.code, reason=error.reason))
            job.error_rows += 1
            if error.row_number is not None:
                job.total_rows = max(job.total_rows, error.row_number)
                job.processed_rows = max(job.processed_rows, error.row_number)
        _complete(session, job, 'FAILED_VALIDATION' if validation else 'FAILED_SYSTEM',
            error.code if validation else 'IMPORT_SYSTEM_ERROR')
        session.commit()


def process_import_job(session_factory: SessionFactory, job_id: UUID, generation: int) -> None:
    """Process a previously claimed generation; safe to call from any worker."""
    try:
        with session_factory() as session:
            _, job = _locked_job(session, job_id, generation)
            metadata = {'file_path': job.file_path, 'file_hash': job.file_hash, 'format': job.format,
                'mode': job.mode, 'phase': job.phase}
            session.commit()
        if metadata['phase'] != 'APPLYING':
            _stage(session_factory, job_id, generation, metadata)
        _apply(session_factory, job_id, generation, metadata['mode'])
    except LostLease:
        return
    except InvalidSource as error:
        try:
            _fail(session_factory, job_id, generation, error, True)
        except LostLease:
            pass
    except Exception as error:
        _LOG.error('Import failed', extra={'job_id': str(job_id), 'error_type': type(error).__name__})
        try:
            _fail(session_factory, job_id, generation, error, False)
        except LostLease:
            pass


def process_import_jobs(session_factory: SessionFactory, worker_id='imports', limit=1) -> int:
    processed = 0
    for _ in range(limit):
        with session_factory() as session:
            claim = claim_import_job(session, worker_id)
        if claim is None:
            break
        process_import_job(session_factory, *claim)
        processed += 1
    return processed


def cleanup_orphan_uploads(session: Session, grace_seconds=3600, limit=1000) -> int:
    """Remove aged, unreferenced owned uploads; call periodically, not per batch.

    The grace window protects files being fsynced/accepted and ambiguous commits.
    Never delete arbitrary files or directories from the upload volume.
    """
    import re
    import time
    root = get_settings().upload_dir
    if not root.exists():
        return 0
    cutoff, removed, checked = time.time()-grace_seconds, 0, 0
    with os.scandir(root) as entries:
        for entry in entries:
            if checked >= limit:
                break
            if not entry.is_file(follow_symlinks=False):
                continue
            if not (re.fullmatch(r'[0-9a-f]{32}\.bin', entry.name) or re.fullmatch(r'ticket-upload-[A-Za-z0-9_-]+\.upload', entry.name)):
                continue
            checked += 1
            path = str(root / entry.name)
            try:
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                with open(path, 'rb') as guard:
                    try:
                        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    if session.scalar(select(ImportJob.id).where(ImportJob.file_path == path).limit(1)) is None:
                        Path(path).unlink(missing_ok=True)
                        removed += 1
            except FileNotFoundError:
                continue
    return removed
