"""
DUAL-CHANNEL PROCESS EXECUTION LOGGING SERVICE (core/services/logging_service.py)

Hybrid Two-Tier Audit Logging & Milestone Subsystem for Glass Putty Manufacturing ERP.
Maintains standard terminal/console stdout logging while persisting immutable operational audit records
to the ProcessExecutionLog database ledger.
"""

import sys
import logging
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


def _prepare_log_payload(
    process_type: str,
    message: str,
    level: str = 'INFO',
    details: Optional[Dict[str, Any]] = None,
    production_order=None,
    work_order=None,
    logged_by=None
) -> Dict[str, Any]:
    """
    Standardizes log payload and enforces string truncation guard.
    Overflow is safely placed into the details JSONField.
    """
    level_normalized = (level or 'INFO').upper().strip()
    if level_normalized not in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
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

    return {
        'process_type': process_type,
        'level': level_normalized,
        'message': truncated_message,
        'details': details_dict,
        'production_order': production_order,
        'work_order': work_order,
        'logged_by': logged_by,
        'raw_message': raw_message,
    }


def log_execution_event(
    process_type: str,
    message: str,
    level: str = 'INFO',
    details: Optional[Dict[str, Any]] = None,
    production_order=None,
    work_order=None,
    logged_by=None
):
    """
    Dual-Channel Execution Logger:
    1. Tier 1: Writes formatted log to Python logger / sys.stdout.
    2. Tier 2: Persists immutable ProcessExecutionLog audit record to the database.

    Fail-safe: Database persistence failure never interrupts the core transaction.
    """
    payload = _prepare_log_payload(
        process_type=process_type,
        message=message,
        level=level,
        details=details,
        production_order=production_order,
        work_order=work_order,
        logged_by=logged_by
    )

    # -------------------------------------------------------------------------
    # TIER 1: CONSOLE / STDOUT LOGGING
    # -------------------------------------------------------------------------
    log_line = f"[{payload['process_type']}] {payload['raw_message']}"
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
    try:
        from core.models import ProcessExecutionLog
        log_entry = ProcessExecutionLog.objects.create(
            process_type=payload['process_type'],
            level=payload['level'],
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

    for event in event_list:
        payload = _prepare_log_payload(
            process_type=event.get('process_type', 'RECONCILIATION'),
            message=event.get('message', ''),
            level=event.get('level', 'INFO'),
            details=event.get('details'),
            production_order=event.get('production_order'),
            work_order=event.get('work_order'),
            logged_by=event.get('logged_by')
        )
        prepared_entries.append(payload)

        # Tier 1: Terminal stdout
        log_line = f"[{payload['process_type']}] {payload['raw_message']}"
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
                process_type=payload['process_type'],
                level=payload['level'],
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
