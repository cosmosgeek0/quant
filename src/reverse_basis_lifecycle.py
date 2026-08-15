"""Pure reverse-basis lifecycle: consume canonical truth; emit, never execute."""
from __future__ import annotations
import hashlib, json, re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict
from margin_reconcile import LiabilityOpen, PartialUnwind, ReconciledFlat, UnknownFreeze, VerdictRow
(BLOCKED, PREFLIGHTED, BORROWING, LIABILITY_CONFIRMED, ENTRY_MARGIN_SELL_WORKING, HEDGE_WORKING, OPEN, EXITING, REPAYING, CLOSED, RECOVERY_REQUIRED, RECOVERING, RECOVERED, MANUAL_INTERVENTION) = STATE_NAMES = ('BLOCKED', 'PREFLIGHTED', 'BORROWING', 'LIABILITY_CONFIRMED', 'ENTRY_MARGIN_SELL_WORKING', 'HEDGE_WORKING', 'OPEN', 'EXITING', 'REPAYING', 'CLOSED', 'RECOVERY_REQUIRED', 'RECOVERING', 'RECOVERED', 'MANUAL_INTERVENTION')
STATES = STATE_NAMES; ZERO = Decimal('0')
FILL_STATUSES = frozenset(('FILLED', 'PARTIALLY_FILLED')); NO_FILL_STATUSES = frozenset(('NEW', 'PENDING_NEW', 'ACCEPTED', 'REJECTED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'EXPIRED_IN_MATCH'))
BORROW_STATUSES = frozenset(('BORROW', 'BORROWED', 'BORROW_CONFIRMED')); REPAY_STATUSES = frozenset(('REPAY', 'REPAID', 'REPAY_CONFIRMED'))
ROLES = frozenset(('ENTRY', 'HEDGE', 'COVER', 'CLOSE', 'RECOVERY')); ASSET_RE = re.compile('[A-Z0-9]{1,20}'); SYMBOL_RE = re.compile('[A-Z0-9]{5,30}'); FillFact = Tuple[str, str, str, str]
IntentRow = TypedDict('IntentRow', {'intent_id': str, 'kind': str, 'asset': str, 'quantity': str, 'execution_authority': str})
TransitionRow = TypedDict('TransitionRow', {'verdict': str, 'reason': str, 'from_state': str, 'to_state': str, 'event_kind': str, 'transition_count': str, 'evidence': List[str]})

def _decimal(value: Any, label: str, positive: bool=False) -> Decimal:
    if not isinstance(value, str) or not value:
        raise ValueError(label + ' must be an exact decimal string')
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(label + ' is malformed') from None
    if not parsed.is_finite() or parsed < ZERO or (positive and parsed <= ZERO):
        raise ValueError(label + ' is outside its allowed bounds')
    return parsed

@dataclass(frozen=True)
class LifecycleState:
    stage: str = BLOCKED; transition_count: int = 0; stop_present: bool = False
    ticket_present: bool = False; borrow_capability: bool = False
    frozen_from: Optional[str] = None; reconcile_digest: Optional[str] = None
    asset: Optional[str] = None; requested_principal: Optional[str] = None; liability_total: Optional[str] = None
    fills: Tuple[FillFact, ...] = (); repay_confirmed: bool = False; recovery_witness: bool = False
    pending_intent_kind: Optional[str] = None; pending_intent_id: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        row = asdict(self); row['fills'] = [list(fact) for fact in self.fills]; return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> 'LifecycleState':
        if not isinstance(row, Mapping) or set(row) != set(asdict(cls())):
            raise ValueError('persisted lifecycle row has the wrong shape')
        values = dict(row)
        raw_fills = values.get('fills')
        if not isinstance(raw_fills, (list, tuple)):
            raise ValueError('persisted fills are malformed')
        values['fills'] = tuple((tuple(fact) for fact in raw_fills))
        state = cls(**values)
        if state.stage not in STATE_NAMES or isinstance(state.transition_count, bool) or (not isinstance(state.transition_count, int)) or (state.transition_count < 0):
            raise ValueError('persisted lifecycle identity is malformed')
        for value in (state.stop_present, state.ticket_present, state.borrow_capability, state.repay_confirmed, state.recovery_witness):
            if not isinstance(value, bool):
                raise ValueError('persisted lifecycle boolean is malformed')
        if state.frozen_from is not None and state.frozen_from not in STATE_NAMES:
            raise ValueError('persisted frozen stage is malformed')
        for fact in state.fills:
            if len(fact) != 4 or fact[0] not in ROLES or (not isinstance(fact[1], str)) or (not fact[1]) or (fact[3] not in FILL_STATUSES):
                raise ValueError('persisted fill is malformed')
            _decimal(fact[2], 'persisted fill', True)
        for value in (state.requested_principal, state.liability_total):
            if value is not None:
                _decimal(value, 'persisted quantity', True)
        return state

def to_row(state: LifecycleState) -> Dict[str, Any]: return state.to_row()

def from_row(row: Mapping[str, Any]) -> LifecycleState: return LifecycleState.from_row(row)

def _move(state: LifecycleState, stage: Optional[str]=None, **changes: Any) -> LifecycleState:
    return replace(state, stage=stage or state.stage, transition_count=state.transition_count + 1, **changes)

def _row(old: LifecycleState, new: LifecycleState, kind: str, verdict: str, reason: str, evidence: Sequence[str]) -> TransitionRow:
    named = list(dict.fromkeys((str(value) for value in evidence if str(value))))
    return {'verdict': verdict, 'reason': reason, 'from_state': old.stage, 'to_state': new.stage, 'event_kind': kind, 'transition_count': str(new.transition_count), 'evidence': named or ['event.kind=' + kind]}

def _done(old: LifecycleState, new: LifecycleState, kind: str, reason: str, evidence: Sequence[str], intents: Sequence[IntentRow]=(), verdict: str='ADVANCED') -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    return (new, list(intents), _row(old, new, kind, verdict, reason, evidence))

def _intent(state: LifecycleState, kind: str, quantity: str) -> IntentRow:
    seed = '%s:%d' % (state.stage, state.transition_count); identity = 'rbl-' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]; return {'intent_id': identity, 'kind': kind, 'asset': state.asset or '', 'quantity': quantity, 'execution_authority': 'EMIT_ONLY'}

def _emit(state: LifecycleState, kind: str, quantity: str, stage: Optional[str]=None) -> Tuple[LifecycleState, List[IntentRow]]:
    intent = _intent(state, kind, quantity); new = _move(state, stage, pending_intent_kind=kind, pending_intent_id=intent['intent_id']); return (new, [intent])

def _freeze(state: LifecycleState, reason: str, evidence: Sequence[str], kind: str='RECONCILIATION') -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    origin = state.frozen_from if state.stage == MANUAL_INTERVENTION and state.frozen_from is not None else state.stage
    new = _move(state, MANUAL_INTERVENTION, frozen_from=origin, pending_intent_kind=None, pending_intent_id=None)
    return (new, [], _row(state, new, kind, 'FROZEN', reason, evidence))

def _digest(rows: Sequence[Mapping[str, Any]]) -> str:
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError('fresh reconciliation rows are required')
    material = []
    for verdict in rows:
        if not isinstance(verdict, Mapping) or not isinstance(verdict.get('evidence'), list):
            raise ValueError('reconciliation row is malformed')
        name = verdict.get('verdict')
        if name == 'LIABILITY_OPEN':
            borrowed = _decimal(verdict.get('borrowed'), 'borrowed')
            interest = _decimal(verdict.get('interest'), 'interest')
            total = _decimal(verdict.get('total_liability'), 'liability', True)
            if borrowed + interest != total or not isinstance(verdict.get('dust'), bool):
                raise ValueError('liability arithmetic is contradictory')
        elif name == 'PARTIAL_UNWIND':
            _decimal(verdict.get('remaining_qty'), 'remaining quantity', True)
        elif name == 'UNKNOWN_FREEZE':
            if not isinstance(verdict.get('missing_evidence'), list):
                raise ValueError('unknown evidence is malformed')
        elif name != 'RECONCILED_FLAT':
            raise ValueError('reconciliation verdict is unsupported')
        material.append(dict(verdict))
    encoded = json.dumps(material, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

def _reconcile(state: LifecycleState, rows: Sequence[VerdictRow], fresh: bool) -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    digest = _digest(rows)
    unknown = next((item for item in rows if item['verdict'] == 'UNKNOWN_FREEZE'), None)
    if unknown is not None:
        evidence = ['margin_reconcile.UNKNOWN_FREEZE'] + unknown['evidence'] + unknown['missing_evidence']
        return _freeze(state, unknown.get('reason', 'unknown truth'), evidence)
    if state.stage == MANUAL_INTERVENTION:
        if state.frozen_from is None or not fresh:
            return _done(state, state, 'MARGIN_RECONCILE', 'FROZEN_AWAITING_FRESH_RECONCILIATION', ['margin_reconcile.fresh=true required'], verdict='REFUSED')
        flat = any((item['verdict'] == 'RECONCILED_FLAT' for item in rows))
        recovery = state.recovery_witness or state.frozen_from in (RECOVERY_REQUIRED, RECOVERING)
        stage = RECOVERED if flat and recovery else CLOSED if flat else RECOVERY_REQUIRED
        new = _move(state, stage, frozen_from=None, reconcile_digest=digest)
        return _done(state, new, 'MARGIN_RECONCILE', 'FRESH_RECONCILIATION_RELEASED_FREEZE', ['margin_reconcile.fresh=true'])
    partial = next((item for item in rows if item['verdict'] == 'PARTIAL_UNWIND'), None)
    flat = next((item for item in rows if item['verdict'] == 'RECONCILED_FLAT'), None)
    liability = next((item for item in rows if item['verdict'] == 'LIABILITY_OPEN'), None)
    if partial is not None:
        new = _move(state, RECOVERY_REQUIRED, reconcile_digest=digest, pending_intent_kind=None, pending_intent_id=None)
        return _done(state, new, 'MARGIN_RECONCILE', 'PARTIAL_UNWIND_REQUIRES_RECOVERY', ['margin_reconcile.PARTIAL_UNWIND'] + partial['evidence'])
    closed = flat is not None and state.stage == REPAYING and state.repay_confirmed
    recovered = flat is not None and state.stage == RECOVERING and state.recovery_witness
    if closed or recovered:
        new = _move(state, CLOSED if closed else RECOVERED, reconcile_digest=digest, pending_intent_kind=None, pending_intent_id=None)
        return _done(state, new, 'MARGIN_RECONCILE', 'EXACT_TERMINAL_FLATNESS_CONFIRMED', ['margin_reconcile.RECONCILED_FLAT'] + flat['evidence'])
    if liability is not None and state.stage == BORROWING:
        evidence = ['margin_reconcile.LIABILITY_OPEN'] + liability['evidence']
        mismatch = liability.get('asset') != state.asset or _decimal(liability['borrowed'], 'borrowed') != _decimal(state.requested_principal, 'principal', True)
        if mismatch:
            new = _move(state, RECOVERY_REQUIRED, reconcile_digest=digest)
            return _done(state, new, 'MARGIN_RECONCILE', 'BORROW_LIABILITY_MISMATCH', evidence)
        base = replace(state, liability_total=liability['total_liability'], reconcile_digest=digest)
        kind = 'REPAY' if state.stop_present else 'MARGIN_SELL'
        quantity = liability['total_liability'] if state.stop_present else state.requested_principal
        (new, intents) = _emit(base, kind, quantity, LIABILITY_CONFIRMED)
        reason = 'STOP_SEALED_ENTRY_REPAY_ALLOWED' if state.stop_present else 'EXACT_LIABILITY_CONFIRMED'
        return _done(state, new, 'MARGIN_RECONCILE', reason, evidence, intents)
    new = _move(state, reconcile_digest=digest)
    return _done(state, new, 'MARGIN_RECONCILE', 'RECONCILIATION_RECORDED_WITHOUT_STAGE_ADVANCE', ['margin_reconcile.verdicts'], verdict='OBSERVED')

def _ledger(state: LifecycleState, event: Mapping[str, Any]) -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    status = str(event.get('status', '')).upper()
    evidence = ['ledger.asset', 'ledger.principal', 'ledger.interest', 'ledger.status=' + status]
    principal = _decimal(event.get('principal'), 'ledger principal')
    interest = _decimal(event.get('interest'), 'ledger interest')
    if status in BORROW_STATUSES:
        allowed = state.stage == PREFLIGHTED and state.borrow_capability and (not state.stop_present) and (event.get('asset') == state.asset) and (principal == _decimal(state.requested_principal, 'principal', True))
        if not allowed:
            return _freeze(state, 'confirmed borrow mismatched preflight', evidence, 'LEDGER')
        new = _move(state, BORROWING, pending_intent_kind=None, pending_intent_id=None)
        return _done(state, new, 'LEDGER', 'CONFIRMED_BORROW_RECORDED', evidence)
    if state.stage not in (REPAYING, LIABILITY_CONFIRMED, RECOVERING):
        return _done(state, state, 'LEDGER', 'REPAY_CONFIRMATION_OUT_OF_SEQUENCE', evidence, verdict='REFUSED')
    if state.liability_total is None or principal + interest != _decimal(state.liability_total, 'liability', True):
        new = _move(state, RECOVERY_REQUIRED)
        return _done(state, new, 'LEDGER', 'REPAY_AMOUNT_MISMATCH_REQUIRES_RECOVERY', evidence)
    new = _move(state, RECOVERING if state.stage == RECOVERING else REPAYING, repay_confirmed=True, pending_intent_kind=None, pending_intent_id=None)
    return _done(state, new, 'LEDGER', 'EXACT_REPAY_CONFIRMED', evidence)

def _parse_fill(event: Mapping[str, Any]) -> Tuple[str, str, str, str, List[str]]:
    status = str(event.get('status', '')).upper()
    if status in NO_FILL_STATUSES:
        raise LookupError('intent or no-fill evidence cannot advance lifecycle')
    if status not in FILL_STATUSES:
        raise ValueError('exchange fill status is unsupported')
    (raw_id, side) = (event.get('id'), str(event.get('side', '')).upper())
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or (not str(raw_id)):
        raise ValueError('exchange fill id is malformed')
    if side not in ('BUY', 'SELL'):
        raise ValueError('exchange fill side is malformed')
    if not isinstance(event.get('symbol'), str) or not SYMBOL_RE.fullmatch(event['symbol']):
        raise ValueError('exchange fill symbol is malformed')
    quantity = event.get('executedQty')
    _decimal(quantity, 'exchange executedQty', True)
    quote = 'cummulativeQuoteQty' if 'cummulativeQuoteQty' in event else 'cumulativeQuoteQty'
    _decimal(event.get(quote), 'exchange quote quantity')
    if 'fee_by_asset' in event:
        if not isinstance(event['fee_by_asset'], Mapping):
            raise ValueError('exchange fee map is malformed')
        for value in event['fee_by_asset'].values():
            _decimal(value, 'exchange fee')
    else:
        pair = next(((amount, asset) for amount, asset in (
            ('commission', 'commissionAsset'), ('fee', 'feeAsset'),
            ('feeQty', 'feeAsset')) if amount in event or asset in event), None)
        if pair is None:
            raise ValueError('exchange fee evidence is missing')
        _decimal(event.get(pair[0]), 'exchange fee')
        if not isinstance(event.get(pair[1]), str):
            raise ValueError('exchange commission asset is missing')
    evidence = ['exchange_fill.id', 'exchange_fill.status', 'exchange_fill.executedQty', 'exchange_fill.' + quote, 'exchange_fill.fee_fields']
    return (str(raw_id), side, quantity, status, evidence)

def _record(state: LifecycleState, role: str, order_id: str, quantity: str, status: str) -> Tuple[LifecycleState, bool]:
    facts = list(state.fills)
    for (index, fact) in enumerate(facts):
        if fact[:2] == (role, order_id):
            if _decimal(quantity, 'fill', True) < _decimal(fact[2], 'prior fill', True):
                raise ValueError('cumulative fill quantity regressed')
            replacement = (role, order_id, quantity, status)
            if replacement == fact:
                return (state, False)
            facts[index] = replacement
            return (replace(state, fills=tuple(facts)), True)
    facts.append((role, order_id, quantity, status))
    return (replace(state, fills=tuple(facts)), True)

def _total(state: LifecycleState, role: str) -> Optional[Decimal]: values = [_decimal(q, role + ' fill', True) for (r, _, q, _) in state.fills if r == role]; return sum(values, ZERO) if values else None

def _terminal(state: LifecycleState, role: str) -> bool: rows = [fact for fact in state.fills if fact[0] == role]; return bool(rows) and all((fact[3] == 'FILLED' for fact in rows))

def _fill(state: LifecycleState, event: Mapping[str, Any]) -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    try:
        (order_id, side, quantity, status, evidence) = _parse_fill(event)
    except LookupError as error:
        return _done(state, state, 'EXCHANGE_FILL', 'INTENT_ONLY_EVIDENCE', [str(error)], verdict='REFUSED')
    if state.asset is None or not event['symbol'].startswith(state.asset):
        raise ValueError('exchange fill symbol does not bind the lifecycle asset')
    role = None
    if state.stage in (LIABILITY_CONFIRMED, ENTRY_MARGIN_SELL_WORKING) and side == 'SELL':
        role = 'ENTRY'
    elif state.stage in (ENTRY_MARGIN_SELL_WORKING, HEDGE_WORKING) and side == 'BUY':
        role = 'HEDGE'
    elif state.stage in (OPEN, EXITING) and side == 'BUY':
        role = 'COVER'
    elif state.stage == EXITING and side == 'SELL' and (event.get('reduceOnly') is True):
        role = 'CLOSE'
    elif state.stage == RECOVERY_REQUIRED and event.get('risk_reducing') is True:
        role = 'RECOVERY'
    if role is None:
        return _done(state, state, 'EXCHANGE_FILL', 'FILL_OUT_OF_SEQUENCE_OR_NOT_RISK_REDUCING', evidence, verdict='REFUSED')
    (recorded, changed) = _record(state, role, order_id, quantity, status)
    if not changed:
        return _done(state, state, 'EXCHANGE_FILL', 'DUPLICATE_FILL_REPLAY', evidence, verdict='OBSERVED')
    (entry, hedge) = (_total(recorded, 'ENTRY'), _total(recorded, 'HEDGE'))
    (cover, close) = (_total(recorded, 'COVER'), _total(recorded, 'CLOSE'))
    if role == 'ENTRY':
        hedged = hedge if hedge is not None else ZERO
        if len({fact[1] for fact in recorded.fills if fact[0] == role}) != 1:
            (new, reason) = (_move(recorded, RECOVERY_REQUIRED), 'MULTIPLE_ENTRY_ORDERS')
            return _done(state, new, 'EXCHANGE_FILL', reason, evidence)
        if entry is None or hedged > entry:
            raise ValueError('entry and hedge fill totals disagree')
        if recorded.pending_intent_kind == 'FUTURES_HEDGE':
            new = _move(recorded, RECOVERY_REQUIRED)
            return _done(state, new, 'EXCHANGE_FILL', 'ENTRY_GREW_WHILE_HEDGE_OUTCOME_UNKNOWN', evidence)
        stage = RECOVERY_REQUIRED if state.stop_present else HEDGE_WORKING \
            if _terminal(recorded, 'ENTRY') else ENTRY_MARGIN_SELL_WORKING
        base = replace(recorded, pending_intent_kind=None, pending_intent_id=None)
        unhedged = entry - hedged
        if unhedged > ZERO:
            (new, intents) = _emit(base, 'FUTURES_HEDGE', format(unhedged, 'f'), stage)
        else:
            new, intents = _move(base, OPEN if stage == HEDGE_WORKING \
                                 and _terminal(base, 'HEDGE') else stage), []
        reason = 'STOPPED_ENTRY_FILL_REQUIRES_RECOVERY' if state.stop_present \
            else 'ACTUAL_MARGIN_FILL_RECORDED'
        return _done(state, new, 'EXCHANGE_FILL', reason, evidence, intents)
    if role == 'HEDGE':
        if entry is None or hedge is None or hedge > entry:
            new = _move(recorded, RECOVERY_REQUIRED)
            return _done(state, new, 'EXCHANGE_FILL', 'HEDGE_FILL_EXCEEDED_MARGIN_FILL', evidence)
        stage = OPEN if hedge == entry and _terminal(recorded, 'ENTRY') and _terminal(recorded, 'HEDGE') else HEDGE_WORKING if _terminal(recorded, 'ENTRY') else ENTRY_MARGIN_SELL_WORKING
        new = _move(recorded, stage, pending_intent_kind=None, pending_intent_id=None)
        return _done(state, new, 'EXCHANGE_FILL', 'ACTUAL_FUTURES_HEDGE_FILL_RECORDED', evidence)
    if role == 'COVER':
        if entry is None or cover is None or cover > entry:
            new = _move(recorded, RECOVERY_REQUIRED)
            return _done(state, new, 'EXCHANGE_FILL', 'COVER_FILL_EXCEEDED_OPEN_MARGIN_FILL', evidence)
        closed = close if close is not None else ZERO
        if recorded.pending_intent_kind == 'FUTURES_CLOSE' or closed > cover:
            new = _move(recorded, RECOVERY_REQUIRED)
            return _done(state, new, 'EXCHANGE_FILL', 'COVER_GREW_WITH_CLOSE_OUTCOME_UNKNOWN', evidence)
        base = replace(recorded, pending_intent_kind=None, pending_intent_id=None)
        remaining = cover - closed
        if remaining > ZERO:
            (new, intents) = _emit(base, 'FUTURES_CLOSE', format(remaining, 'f'), EXITING)
        elif cover == entry and _terminal(base, 'COVER') and _terminal(base, 'CLOSE') \
                and base.liability_total is not None:
            (new, intents) = _emit(base, 'REPAY', base.liability_total, REPAYING)
        else:
            new, intents = _move(base, EXITING), []
        return _done(state, new, 'EXCHANGE_FILL', 'ACTUAL_MARGIN_COVER_FILL_RECORDED', evidence, intents)
    if role == 'CLOSE':
        if entry is None or cover is None or close is None or (close > cover) or (close > entry):
            new = _move(recorded, RECOVERY_REQUIRED)
            return _done(state, new, 'EXCHANGE_FILL', 'FUTURES_CLOSE_FILL_MISMATCH', evidence)
        exact = close == cover == entry and _terminal(recorded, 'COVER') and _terminal(recorded, 'CLOSE')
        base = replace(recorded, pending_intent_kind=None, pending_intent_id=None)
        if exact and base.liability_total is not None:
            (new, intents) = _emit(base, 'REPAY', base.liability_total, REPAYING)
            reason = 'MATCHED_EXIT_FILLS_ALLOW_REPAY'
        else:
            (new, intents, reason) = (_move(base, EXITING), [], 'PARTIAL_EXIT_FILL_RECORDED')
        return _done(state, new, 'EXCHANGE_FILL', reason, evidence, intents)
    new = _move(recorded, RECOVERING, recovery_witness=True, pending_intent_kind=None, pending_intent_id=None)
    return _done(state, new, 'EXCHANGE_FILL', 'RISK_REDUCING_RECOVERY_FILL_RECORDED', evidence)

def _operator(state: LifecycleState, event: Mapping[str, Any], borrow_enabled: bool) -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    kind = event.get('event', event.get('signal', event.get('type')))
    if kind is None and 'stop_present' in event:
        kind = 'STOP'
    elif kind is None and 'ticket_present' in event:
        kind = 'TICKET'
    evidence = ['operator.event=' + str(kind)]
    if kind == 'STOP':
        if event.get('present', event.get('stop_present')) is not True:
            return _done(state, state, 'STOP', 'STOP_ABSENCE_CANNOT_UNSEAL', evidence, verdict='REFUSED')
        new = state if state.stop_present else _move(state, stop_present=True)
        return _done(state, new, 'STOP', 'STOP_SEALED_NEW_ENTRIES', evidence, verdict='OBSERVED')
    if kind == 'TICKET':
        present = event.get('present', event.get('ticket_present'))
        if not isinstance(present, bool):
            return _done(state, state, 'TICKET', 'TICKET_SIGNAL_MALFORMED', evidence, verdict='REFUSED')
        new = _move(state, ticket_present=present)
        return _done(state, new, 'TICKET', 'TICKET_PRESENCE_RECORDED', evidence, verdict='OBSERVED')
    if kind == 'PREFLIGHT_PASSED':
        ticket = state.ticket_present or event.get('ticket_present') is True
        stop = state.stop_present or event.get('stop_present') is True
        if state.stage != BLOCKED:
            reason = 'PREFLIGHT_OUT_OF_SEQUENCE'
        elif stop:
            new = _move(state, stop_present=True, ticket_present=ticket)
            return _done(state, new, kind, 'STOP_SEALS_ENTRY', evidence, verdict='REFUSED')
        elif not ticket:
            reason = 'TICKET_REQUIRED'
        elif borrow_enabled is not True:
            reason = 'BORROW_CAPABILITY_DISABLED'
        else:
            (principal, asset) = (event.get('principal'), event.get('asset'))
            _decimal(principal, 'requested principal', True)
            if isinstance(asset, str) and ASSET_RE.fullmatch(asset):
                base = replace(state, asset=asset, requested_principal=principal, ticket_present=True, borrow_capability=True)
                (new, intents) = _emit(base, 'BORROW', principal, PREFLIGHTED)
                return _done(state, new, kind, 'CAPABILITY_AND_TICKET_PREFLIGHTED', evidence + ['caller.borrow_enabled=true', 'ticket.present'], intents)
            reason = 'PREFLIGHT_ASSET_MALFORMED'
        return _done(state, state, kind, reason, evidence, verdict='REFUSED')
    if kind == 'EXIT_REQUESTED':
        entry = _total(state, 'ENTRY')
        if state.stage == OPEN and entry is not None:
            (new, intents) = _emit(state, 'MARGIN_COVER', format(entry, 'f'))
        elif state.stage == LIABILITY_CONFIRMED and state.liability_total:
            (new, intents) = _emit(state, 'REPAY', state.liability_total)
        else:
            return _done(state, state, kind, 'NO_PROVEN_EXIT_TARGET', evidence, verdict='REFUSED')
        return _done(state, new, kind, 'EXIT_ALLOWED_REGARDLESS_OF_STOP', evidence, intents, verdict='INTENT_EMITTED')
    if kind == 'RECOVERY_APPROVED':
        quantity = event.get('quantity')
        _decimal(quantity, 'recovery quantity', True)
        if state.stage != RECOVERY_REQUIRED or event.get('risk_reducing') is not True:
            return _done(state, state, kind, 'RECOVERY_ACTION_NOT_PROVEN_RISK_REDUCING', evidence, verdict='REFUSED')
        (new, intents) = _emit(state, 'RECOVERY_REDUCE', quantity)
        return _done(state, new, kind, 'BOUNDED_RISK_REDUCTION_EMITTED', evidence, intents, verdict='INTENT_EMITTED')
    if kind == 'MANUAL_REQUIRED' and state.stage == RECOVERY_REQUIRED:
        new = _move(state, MANUAL_INTERVENTION, frozen_from=None)
        return _done(state, new, kind, 'OPERATOR_ESCALATED_WITHOUT_ERASING_DEBT', evidence)
    if kind in ('INTENT', 'ORDER_INTENT', 'BORROW_INTENT', 'REPAY_INTENT') or 'intent' in event:
        return _done(state, state, 'INTENT', 'INTENT_ONLY_EVIDENCE', evidence, verdict='REFUSED')
    return _done(state, state, 'OPERATOR', 'EVENT_UNSUPPORTED', evidence, verdict='REFUSED')

def advance(state: LifecycleState, event: Any, *, borrow_enabled: bool=False) -> Tuple[LifecycleState, List[IntentRow], TransitionRow]:
    """Apply one evidence event deterministically; never call an executor."""
    if isinstance(state, str) and state in STATE_NAMES:
        state = LifecycleState(stage=state)
    if isinstance(state, Mapping):
        try:
            state = from_row(state)
        except ValueError:
            state = LifecycleState(MANUAL_INTERVENTION, frozen_from=BLOCKED)
    if not isinstance(state, LifecycleState):
        raise TypeError('state must be LifecycleState or its persisted row')
    if not isinstance(event, Mapping):
        return _freeze(state, 'event is not a plain mapping', ['event.mapping'])
    try:
        if event.get('event') == 'MARGIN_RECONCILE':
            rows = event.get('rows')
            return _reconcile(state, rows if isinstance(rows, (list, tuple)) else (), event.get('fresh') is True)
        if 'verdict' in event:
            return _reconcile(state, [event], True)
        if state.stage == MANUAL_INTERVENTION and state.frozen_from is not None:
            return (state, [], _row(state, state, 'FROZEN', 'REFUSED', 'FROZEN_AWAITING_FRESH_RECONCILIATION', ['margin_reconcile fresh verdict required']))
        status = str(event.get('status', '')).upper()
        if status in BORROW_STATUSES | REPAY_STATUSES:
            return _ledger(state, event)
        if 'executedQty' in event or ('side' in event and 'status' in event):
            return _fill(state, event)
        return _operator(state, event, borrow_enabled)
    except ValueError as error:
        return _freeze(state, str(error), ['event.validated_fields'])

def derive_resume(rows: Sequence[Mapping[str, Any]], fresh_verdicts: Sequence[VerdictRow]=()) -> LifecycleState:
    """Restore only when a continuous persisted trail matches fresh truth."""
    try:
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ValueError('persisted trail is missing')
        states = [from_row(item) for item in rows]
        if any((now.transition_count != before.transition_count + 1 for (before, now) in zip(states, states[1:]))):
            raise ValueError('persisted trail is discontinuous')
        (latest, fresh_digest) = (states[-1], _digest(fresh_verdicts))
        if latest.reconcile_digest != fresh_digest or any((item['verdict'] == 'UNKNOWN_FREEZE' for item in fresh_verdicts)):
            raise ValueError('fresh truth mismatches persisted truth')
        return latest
    except (KeyError, TypeError, ValueError):
        try:
            latest = from_row(rows[-1]) if rows else LifecycleState()
        except (KeyError, TypeError, ValueError):
            latest = LifecycleState()
        return replace(latest, stage=MANUAL_INTERVENTION, transition_count=latest.transition_count + 1, frozen_from=latest.frozen_from or latest.stage, pending_intent_kind=None, pending_intent_id=None)
CANONICAL_VERDICT_TYPES = (ReconciledFlat, LiabilityOpen, PartialUnwind, UnknownFreeze)
