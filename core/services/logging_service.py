"""
DUAL-CHANNEL PROCESS EXECUTION LOGGING SERVICE (core/services/logging_service.py)

Hybrid Two-Tier Audit Logging & Milestone Subsystem for Glass Putty Manufacturing ERP.
Maintains standard terminal/console stdout logging while persisting immutable operational audit records
to the ProcessExecutionLog database ledger.
"""

import sys
import logging
import contextvars
from typing import Optional, Dict, Any, List
from django.utils import timezone

logger = logging.getLogger('core.execution')
if not logger.handlers:
    # Ensure stdout handler is attached if not configured in settings
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

MAX_MESSAGE_LENGTH = 1000

# Thread-safe ContextVar to track triggering user across web requests & Celery tasks
_current_user_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar('current_authenticated_user', default=None)


def set_current_authenticated_user(user) -> contextvars.Token:
    """Sets the current authenticated user for operational audit logging and returns the context token."""
    return _current_user_var.set(user)


def reset_current_authenticated_user(token: contextvars.Token) -> None:
    """Resets the context variable to its previous state using the token to prevent thread leakage."""
    if token is not None:
        try:
            _current_user_var.reset(token)
        except Exception as e:
            logger.debug(f"[LOGGING CONTEXT RESET] Token reset ignored: {e}")


def get_current_authenticated_user():
    """Retrieves the current authenticated user from ContextVar if set."""
    return _current_user_var.get()


def _prepare_log_payload(
    process_type: str,
    message: str,
    level: str = 'INFO',
    details: Optional[Dict[str, Any]] = None,
    production_order=None,
    work_order=None,
    logged_by=None,
    event_title: Optional[str] = None
) -> Dict[str, Any]:
    """
    Standardizes log payload and enforces string truncation guard.
    Overflow is safely placed into the details JSONField.
    """
    level_normalized = (level or 'INFO').upper().strip()
    if level_normalized not in ['DEBUG', 'INFO', 'SUCCESS', 'WARNING', 'ERROR']:
        level_normalized = 'INFO'

    details_dict = dict(details) if details else {}

    raw_message = str(message or '')
    if len(raw_message) > MAX_MESSAGE_LENGTH:
        truncated_message = raw_message[:MAX_MESSAGE_LENGTH]
        details_dict['_full_message'] = raw_message
        details_dict['_truncated_length'] = len(raw_message)
    else:
        truncated_message = raw_message

    # Auto-resolve related objects if only one is passed
    if production_order and not work_order and getattr(production_order, 'work_order_id', None):
        work_order = production_order.work_order
    elif work_order and not production_order and hasattr(work_order, 'production_runs'):
        po_candidate = work_order.production_runs.first()
        if po_candidate:
            production_order = po_candidate

    # Auto-resolve user attribution from thread ContextVar if not explicitly passed
    resolved_user = logged_by or get_current_authenticated_user()

    # Resolve event_title from argument, details, or standard choice label
    clean_title = str(event_title or details_dict.get('event_title') or '').strip()
    if not clean_title:
        try:
            from core.models import ProcessExecutionLog
            clean_title = dict(ProcessExecutionLog.PROCESS_TYPE_CHOICES).get(
                process_type, process_type.replace('_', ' ').title()
            )
        except Exception:
            clean_title = process_type.replace('_', ' ').title()

    return {
        'process_type': process_type,
        'level': level_normalized,
        'event_title': clean_title,
        'message': truncated_message,
        'details': details_dict,
        'production_order': production_order,
        'work_order': work_order,
        'logged_by': resolved_user,
        'raw_message': raw_message,
    }


def log_execution_event(
    process_type: str,
    message: str,
    level: str = 'INFO',
    details: Optional[Dict[str, Any]] = None,
    production_order=None,
    work_order=None,
    logged_by=None,
    persist_to_db: bool = True,
    event_title: Optional[str] = None
):
    """
    Dual-Channel Execution Logger:
    1. Tier 1: Writes formatted log to Python logger / sys.stdout.
    2. Tier 2: Persists immutable ProcessExecutionLog audit record to the database (suppressed if persist_to_db=False or level=='DEBUG').

    Fail-safe: Database persistence failure never interrupts the core transaction.
    """
    payload = _prepare_log_payload(
        process_type=process_type,
        message=message,
        level=level,
        details=details,
        production_order=production_order,
        work_order=work_order,
        logged_by=logged_by,
        event_title=event_title
    )

    # -------------------------------------------------------------------------
    # TIER 1: CONSOLE / STDOUT LOGGING
    # -------------------------------------------------------------------------
    title_prefix = f"[{payload['event_title']}] " if payload.get('event_title') else ""
    log_line = f"[{payload['process_type']}] {title_prefix}{payload['raw_message']}"
    if payload['level'] == 'ERROR':
        logger.error(log_line)
    elif payload['level'] == 'WARNING':
        logger.warning(log_line)
    elif payload['level'] == 'DEBUG':
        logger.debug(log_line)
    else:
        logger.info(log_line)

    # -------------------------------------------------------------------------
    # TIER 2: DATABASE PERSISTENCE (ProcessExecutionLog)
    # -------------------------------------------------------------------------
    # Demote pure DEBUG developer diagnostic signals from database noise
    if not persist_to_db or payload['level'] == 'DEBUG':
        return None

    try:
        from core.models import ProcessExecutionLog
        log_entry = ProcessExecutionLog.objects.create(
            process_type=payload['process_type'],
            level=payload['level'],
            event_title=payload['event_title'],
            message=payload['message'],
            details=payload['details'],
            production_order=payload['production_order'],
            work_order=payload['work_order'],
            logged_by=payload['logged_by']
        )
        return log_entry
    except Exception as db_err:
        logger.warning(f"[LOGGING SUBSYSTEM DB WRITE FAILURE] {db_err}")
        return None


def bulk_log_execution_events(event_list: List[Dict[str, Any]]) -> List[Any]:
    """
    High-volume Batch Logging Helper for large multi-component reconciliations or MRP sweeps.
    Writes each entry to stdout and executes a single ProcessExecutionLog.objects.bulk_create() call.
    """
    if not event_list:
        return []

    from core.models import ProcessExecutionLog

    prepared_entries = []
    log_instances = []

    last_obj = ProcessExecutionLog.objects.order_by('-log_id').values('log_id').first()
    base_id = (last_obj['log_id'] if last_obj else 0)

    for idx, event in enumerate(event_list, start=1):
        payload = _prepare_log_payload(
            process_type=event.get('process_type', 'RECONCILIATION'),
            message=event.get('message', ''),
            level=event.get('level', 'INFO'),
            details=event.get('details'),
            production_order=event.get('production_order'),
            work_order=event.get('work_order'),
            logged_by=event.get('logged_by'),
            event_title=event.get('event_title')
        )
        prepared_entries.append(payload)

        # Tier 1: Terminal stdout
        title_prefix = f"[{payload['event_title']}] " if payload.get('event_title') else ""
        log_line = f"[{payload['process_type']}] {title_prefix}{payload['raw_message']}"
        if payload['level'] == 'ERROR':
            logger.error(log_line)
        elif payload['level'] == 'WARNING':
            logger.warning(log_line)
        elif payload['level'] == 'DEBUG':
            logger.debug(log_line)
        else:
            logger.info(log_line)

        log_instances.append(
            ProcessExecutionLog(
                log_code=f"PEL-{(base_id + idx):05d}",
                process_type=payload['process_type'],
                level=payload['level'],
                event_title=payload['event_title'],
                message=payload['message'],
                details=payload['details'],
                production_order=payload['production_order'],
                work_order=payload['work_order'],
                logged_by=payload['logged_by']
            )
        )

    # Tier 2: DB Bulk Insert
    try:
        created_records = ProcessExecutionLog.objects.bulk_create(log_instances)
        return created_records
    except Exception as db_err:
        logger.warning(f"[LOGGING SUBSYSTEM BULK DB WRITE FAILURE] {db_err}")
        return []
