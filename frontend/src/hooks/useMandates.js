import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import {
  activateMandate,
  confirmMandate,
  createMandate,
  getMandate,
  listMandates,
  revokeMandate,
  signMandate,
  verifyMandate,
} from '../services/mandates';

const UNKNOWN_CREATE_STORAGE_KEY = 'leash.mandates.create.unknown';
const UNKNOWN_CREATE_MARKER_VERSION = 2;

// Create is the only operation whose lost response cannot be reconciled by id.
// Keep its lock module-scoped so hook remounts and token switches cannot release it.
const createCoordinator = {
  initialized: false, status: 'uninitialized', generation: 0, operationId: null,
  key: null, promise: null, listeners: new Set(),
};

// Reset is used only by isolated hook tests; production code never calls it.
export function __resetMandateCreateCoordinatorForTests() {
  createCoordinator.initialized = false;
  createCoordinator.status = 'uninitialized';
  createCoordinator.generation += 1;
  createCoordinator.operationId = null;
  createCoordinator.key = null;
  createCoordinator.promise = null;
}

function publishCreateCoordinator() {
  const snapshot = {
    status: createCoordinator.status,
    generation: createCoordinator.generation,
    operationId: createCoordinator.operationId,
  };
  createCoordinator.listeners.forEach((listener) => listener(snapshot));
}

function readCreateMarker() {
  try {
    const raw = sessionStorage.getItem(UNKNOWN_CREATE_STORAGE_KEY);
    if (raw === null) return { status: 'clear' };
    let parsed;
    try { parsed = JSON.parse(raw); } catch { parsed = null; }
    // Any pre-existing/malformed marker is fail-closed and can never be auto-cleared.
    return { status: 'unknown', operationId: parsed?.operation_id || null };
  } catch {
    return { status: 'blocked' };
  }
}

function initializeCreateCoordinator() {
  if (createCoordinator.initialized) return;
  const marker = readCreateMarker();
  createCoordinator.initialized = true;
  createCoordinator.status = marker.status;
  createCoordinator.operationId = marker.operationId || null;
  createCoordinator.generation += 1;
}

function createCoordinatorSnapshot() {
  initializeCreateCoordinator();
  return { status: createCoordinator.status, generation: createCoordinator.generation };
}

function markerMatches(operationId) {
  try {
    const marker = JSON.parse(sessionStorage.getItem(UNKNOWN_CREATE_STORAGE_KEY));
    return marker?.version === UNKNOWN_CREATE_MARKER_VERSION
      && marker?.operation_id === operationId;
  } catch {
    return false;
  }
}

function acquireCreate(key) {
  initializeCreateCoordinator();
  if (createCoordinator.status === 'pending') {
    return createCoordinator.key === key
      ? { shared: true, promise: createCoordinator.promise }
      : { error: new ApiError('A different mandate create is already in progress.', { code: 'operation-pending' }) };
  }
  if (createCoordinator.status !== 'clear') {
    return { error: new ApiError('A mandate create has an unknown outcome. Do not retry it.', { code: 'outcome-unknown' }) };
  }
  const operationId = globalThis.crypto?.randomUUID?.()
    || `op-${Date.now().toString(36)}-${(++createCoordinator.generation).toString(36)}`;
  const generation = ++createCoordinator.generation;
  try {
    sessionStorage.setItem(UNKNOWN_CREATE_STORAGE_KEY, JSON.stringify({
      version: UNKNOWN_CREATE_MARKER_VERSION, operation_id: operationId,
    }));
  } catch {
    createCoordinator.status = 'blocked';
    publishCreateCoordinator();
    return { error: new ApiError('Session storage is unavailable. Mandate create is blocked.', { code: 'storage' }) };
  }
  createCoordinator.status = 'pending';
  createCoordinator.operationId = operationId;
  createCoordinator.generation = generation;
  createCoordinator.key = key;
  createCoordinator.promise = null;
  publishCreateCoordinator();
  return { operationId, generation };
}

function finishCreate(operationId, generation, outcome) {
  if (createCoordinator.operationId !== operationId
    || createCoordinator.generation !== generation) return false;
  if (outcome === 'UNKNOWN') {
    createCoordinator.status = 'unknown';
    createCoordinator.promise = null;
    publishCreateCoordinator();
    return true;
  }
  if (!markerMatches(operationId)) {
    createCoordinator.status = 'blocked';
    createCoordinator.promise = null;
    publishCreateCoordinator();
    return false;
  }
  try {
    sessionStorage.removeItem(UNKNOWN_CREATE_STORAGE_KEY);
  } catch {
    createCoordinator.status = 'blocked';
    createCoordinator.promise = null;
    publishCreateCoordinator();
    return false;
  }
  createCoordinator.status = 'clear';
  createCoordinator.operationId = null;
  createCoordinator.key = null;
  createCoordinator.promise = null;
  createCoordinator.generation += 1;
  publishCreateCoordinator();
  return true;
}

function createUnknownState(storageUnavailable = false) {
  return {
    name: 'create',
    mandateId: null,
    expectedStatus: null,
    outcome: 'UNKNOWN',
    durable: true,
    storageUnavailable,
    message: storageUnavailable
      ? 'Session storage is unavailable. Mandate actions are blocked to protect against a duplicate create.'
      : 'A mandate create may have completed, but the backend cannot identify its result. Do not retry.',
  };
}

function createUnknownLock() {
  return {
    key: 'durable-create-unknown',
    name: 'create',
    promise: Promise.resolve(null),
    unknown: true,
    durable: true,
  };
}

function nextVerificationGeneration(generations, mandateId) {
  const next = (generations.get(mandateId) || 0) + 1;
  generations.set(mandateId, next);
  return next;
}

function upsertMandate(items, mandate) {
  if (!mandate?.mandate_id) return items;
  const exists = items.some((item) => item.mandate_id === mandate.mandate_id);
  return exists
    ? items.map((item) => item.mandate_id === mandate.mandate_id ? mandate : item)
    : [mandate, ...items];
}

function stableSerialize(value) {
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${stableSerialize(value[key])}`
    )).join(',')}}`;
  }
  return JSON.stringify(value);
}

function expectedStatusFor(name) {
  return {
    confirm: 'CONFIRMED', sign: 'SIGNED', activate: 'ACTIVE', revoke: 'REVOKED',
  }[name] || null;
}

export function useMandates(token, onSessionExpired) {
  const initialCreateSnapshotRef = useRef(null);
  if (initialCreateSnapshotRef.current === null) {
    initialCreateSnapshotRef.current = createCoordinatorSnapshot();
  }
  const initialCreateUnknown = initialCreateSnapshotRef.current.status !== 'clear';
  const [mandates, setMandates] = useState([]);
  const [selectedMandateId, setSelectedMandateId] = useState(null);
  const [selectedMandate, setSelectedMandate] = useState(null);
  const [verification, setVerification] = useState(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pendingOperation, setPendingOperation] = useState(null);
  const [unknownOperation, setUnknownOperation] = useState(() => (
    initialCreateUnknown
      ? createUnknownState(initialCreateSnapshotRef.current.status === 'blocked')
      : null
  ));
  const [reconciliationLoading, setReconciliationLoading] = useState(false);
  const [markerStorageAvailable, setMarkerStorageAvailable] = useState(() => (
    initialCreateSnapshotRef.current.status !== 'blocked'
  ));
  const [error, setError] = useState(null);

  const mountedRef = useRef(false);
  const tokenRef = useRef(token);
  const callbackRef = useRef(onSessionExpired);
  const sessionGenerationRef = useRef(0);
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const selectionGenerationRef = useRef(0);
  const verificationGenerationsRef = useRef(new Map());
  const verificationMandateIdRef = useRef(null);
  const selectedIdRef = useRef(null);
  const mutationRef = useRef(initialCreateUnknown ? createUnknownLock() : null);
  const unknownOperationRef = useRef(initialCreateUnknown
    ? createUnknownState(initialCreateSnapshotRef.current.status === 'blocked')
    : null);
  const reconciliationRef = useRef(null);
  const loadedTokenRef = useRef(null);

  tokenRef.current = token;
  callbackRef.current = onSessionExpired;

  const isCurrentSession = useCallback((capturedToken, generation) => (
    mountedRef.current
    && tokenRef.current === capturedToken
    && sessionGenerationRef.current === generation
  ), []);

  const reportError = useCallback((failure, capturedToken, generation) => {
    if (!isCurrentSession(capturedToken, generation)) return;
    setError(failure);
    if (failure instanceof ApiError && failure.code === 'unauthenticated') {
      callbackRef.current?.();
    }
  }, [isCurrentSession]);

  useEffect(() => {
    mountedRef.current = true;
    let coordinatorOwnsPendingState = false;
    const listener = (snapshot) => {
      if (!mountedRef.current) return;
      const locked = snapshot.status !== 'clear';
      const hasOtherMutation = Boolean(
        mutationRef.current && mutationRef.current.name !== 'create',
      );
      if (locked) {
        const unknown = snapshot.status === 'pending'
          ? { ...createUnknownState(), outcome: 'PENDING', message: 'A mandate create is in progress.' }
          : createUnknownState(snapshot.status === 'blocked');
        if (hasOtherMutation) return;
        unknownOperationRef.current = snapshot.status === 'pending' ? null : unknown;
        if (snapshot.status !== 'pending'
          && (!mutationRef.current || mutationRef.current.durable || mutationRef.current.name === 'create')) {
          mutationRef.current = createUnknownLock();
        }
        setUnknownOperation(snapshot.status === 'pending' ? null : unknown);
        setPendingOperation(snapshot.status === 'pending'
          ? { name: 'create', mandateId: null }
          : { name: 'create', mandateId: null, outcome: 'UNKNOWN' });
        coordinatorOwnsPendingState = true;
        setMarkerStorageAvailable(snapshot.status !== 'blocked');
      } else {
        if (unknownOperationRef.current?.name === 'create') {
          unknownOperationRef.current = null;
          setUnknownOperation(null);
        }
        if (mutationRef.current?.durable || mutationRef.current?.name === 'create') {
          mutationRef.current = null;
        }
        if (coordinatorOwnsPendingState && !hasOtherMutation) setPendingOperation(null);
        coordinatorOwnsPendingState = false;
        setMarkerStorageAvailable(true);
      }
    };
    createCoordinator.listeners.add(listener);
    listener(createCoordinatorSnapshot());
    return () => {
      mountedRef.current = false;
      sessionGenerationRef.current += 1;
      listRequestRef.current += 1;
      detailRequestRef.current += 1;
      createCoordinator.listeners.delete(listener);
    };
  }, []);

  useEffect(() => {
    sessionGenerationRef.current += 1;
    selectionGenerationRef.current += 1;
    listRequestRef.current += 1;
    detailRequestRef.current += 1;
    verificationGenerationsRef.current.clear();
    verificationMandateIdRef.current = null;
    selectedIdRef.current = null;
    loadedTokenRef.current = null;
    const createState = createCoordinatorSnapshot();
    if (createState.status !== 'clear') {
      const unknown = createUnknownState(createState.status === 'blocked');
      unknownOperationRef.current = unknown;
      if (createState.status !== 'pending') {
        mutationRef.current = createUnknownLock();
        setUnknownOperation(unknown);
        setPendingOperation({ name: 'create', mandateId: null, outcome: 'UNKNOWN' });
        setError(new ApiError(unknown.message, { code: 'outcome-unknown' }));
      }
    } else {
      mutationRef.current = null;
      unknownOperationRef.current = null;
      setUnknownOperation(null);
      setPendingOperation(null);
    }
    reconciliationRef.current = null;
    setMandates([]);
    setSelectedMandateId(null);
    setSelectedMandate(null);
    setVerification(null);
    setListLoading(false);
    setDetailLoading(false);
    setReconciliationLoading(false);
    if (createState.status === 'clear') setError(null);
  }, [token]);

  const refresh = useCallback(async () => {
    const capturedToken = tokenRef.current;
    if (!capturedToken || !mountedRef.current) return [];
    const generation = sessionGenerationRef.current;
    const requestId = ++listRequestRef.current;
    setListLoading(true);
    setError(null);
    try {
      const result = await listMandates(capturedToken);
      if (!isCurrentSession(capturedToken, generation) || requestId !== listRequestRef.current) {
        return result;
      }
      const rows = Array.isArray(result) ? result : [];
      setMandates(rows);
      loadedTokenRef.current = capturedToken;
      return rows;
    } catch (failure) {
      reportError(failure, capturedToken, generation);
      return [];
    } finally {
      if (isCurrentSession(capturedToken, generation) && requestId === listRequestRef.current) {
        setListLoading(false);
      }
    }
  }, [isCurrentSession, reportError]);

  useEffect(() => {
    if (token) void refresh();
  }, [token, refresh]);

  const selectMandate = useCallback((mandateId) => {
    selectionGenerationRef.current += 1;
    selectedIdRef.current = mandateId || null;
    setSelectedMandateId(mandateId || null);
    setSelectedMandate(null);
    setVerification(null);
    verificationMandateIdRef.current = null;
    setError(null);
  }, []);

  useEffect(() => {
    const capturedToken = tokenRef.current;
    const mandateId = selectedMandateId;
    if (!capturedToken || !mandateId) {
      setDetailLoading(false);
      return undefined;
    }
    const generation = sessionGenerationRef.current;
    const requestId = ++detailRequestRef.current;
    setDetailLoading(true);
    getMandate(capturedToken, mandateId).then((result) => {
      if (!isCurrentSession(capturedToken, generation)
        || requestId !== detailRequestRef.current
        || selectedIdRef.current !== mandateId) return;
      setSelectedMandate(result);
      setMandates((items) => upsertMandate(items, result));
      loadedTokenRef.current = capturedToken;
    }).catch((failure) => {
      if (selectedIdRef.current === mandateId && requestId === detailRequestRef.current) {
        reportError(failure, capturedToken, generation);
      }
    }).finally(() => {
      if (isCurrentSession(capturedToken, generation)
        && requestId === detailRequestRef.current
        && selectedIdRef.current === mandateId) setDetailLoading(false);
    });
    return () => { detailRequestRef.current += 1; };
  }, [selectedMandateId, token, isCurrentSession, reportError]);

  const reconcileUnknown = useCallback(() => {
    if (reconciliationRef.current) return reconciliationRef.current;
    const unknown = unknownOperationRef.current;
    const capturedToken = tokenRef.current;
    if (!unknown || !capturedToken || !mountedRef.current) {
      return Promise.resolve({ resolved: false, outcome: 'UNKNOWN' });
    }
    const generation = sessionGenerationRef.current;
    setReconciliationLoading(true);
    let reconciliationListRequestId = null;
    const task = (async () => {
      try {
        if (unknown.name === 'create') {
          reconciliationListRequestId = ++listRequestRef.current;
          setListLoading(true);
          const rows = await listMandates(capturedToken);
          if (!isCurrentSession(capturedToken, generation)
            || reconciliationListRequestId !== listRequestRef.current) {
            return { resolved: false, outcome: 'UNKNOWN' };
          }
          if (Array.isArray(rows)) {
            setMandates(rows);
            loadedTokenRef.current = capturedToken;
          }
          setListLoading(false);
          // No mandate idempotency key exists, so list similarity cannot prove
          // which row was created by a request whose response was lost.
          return { resolved: false, outcome: 'UNKNOWN' };
        }

        const current = await getMandate(capturedToken, unknown.mandateId);
        if (!isCurrentSession(capturedToken, generation)) {
          return { resolved: false, outcome: 'UNKNOWN' };
        }
        listRequestRef.current += 1;
        setListLoading(false);
        nextVerificationGeneration(verificationGenerationsRef.current, unknown.mandateId);
        if (selectedIdRef.current === current?.mandate_id) {
          detailRequestRef.current += 1;
          setDetailLoading(false);
          setVerification(null);
          verificationMandateIdRef.current = null;
        }
        setMandates((items) => upsertMandate(items, current));
        if (selectedIdRef.current === current?.mandate_id) setSelectedMandate(current);
        if (current?.status !== unknown.expectedStatus) {
          return { resolved: false, outcome: 'UNKNOWN', current };
        }

        unknownOperationRef.current = null;
        setUnknownOperation(null);
        setPendingOperation(null);
        setError(null);
        if (mutationRef.current?.unknown) mutationRef.current = null;
        return { resolved: true, outcome: 'CONFIRMED', current };
      } catch (failure) {
        reportError(failure, capturedToken, generation);
        return { resolved: false, outcome: 'UNKNOWN', error: failure };
      } finally {
        if (isCurrentSession(capturedToken, generation)) {
          setReconciliationLoading(false);
          if (reconciliationListRequestId === listRequestRef.current) setListLoading(false);
        }
        if (reconciliationRef.current === task) reconciliationRef.current = null;
      }
    })();
    reconciliationRef.current = task;
    return task;
  }, [isCurrentSession, reportError]);

  const runMutation = useCallback((name, mandateId, payload, operation) => {
    if (!tokenRef.current || !mountedRef.current) return Promise.resolve(null);
    const key = name === 'create'
      ? `create:${stableSerialize({ token: tokenRef.current, payload })}`
      : `${name}:${mandateId || ''}`;
    let createOperation = null;
    if (name === 'create') {
      initializeCreateCoordinator();
      const sharedCreatePending = createCoordinator.status === 'pending'
        && mutationRef.current?.name === 'create';
      if (mutationRef.current && !sharedCreatePending) {
        const conflict = new ApiError(
          unknownOperationRef.current
            ? 'This operation has an unknown outcome. Reconcile it before starting another mandate action.'
            : 'Another mandate action is already in progress.',
          { code: unknownOperationRef.current ? 'outcome-unknown' : 'operation-pending' },
        );
        setError(conflict);
        return Promise.reject(conflict);
      }
      const acquired = acquireCreate(key);
      if (acquired.error) {
        setError(acquired.error);
        return Promise.reject(acquired.error);
      }
      if (acquired.shared) return acquired.promise;
      createOperation = acquired;
    } else {
      initializeCreateCoordinator();
      if (createCoordinator.status !== 'clear') {
        const conflict = new ApiError('A mandate create is pending or has an unknown outcome. Resolve it before another mandate action.', {
          code: createCoordinator.status === 'pending' ? 'operation-pending' : 'outcome-unknown',
        });
        setError(conflict);
        return Promise.reject(conflict);
      }
    }
    if (mutationRef.current) {
      if (name !== 'create' && mutationRef.current.key === key) return mutationRef.current.promise;
      const conflict = new ApiError(
        unknownOperationRef.current
          ? 'This operation has an unknown outcome. Reconcile it before starting another mandate action.'
          : 'Another mandate action is already in progress.',
        { code: unknownOperationRef.current ? 'outcome-unknown' : 'operation-pending' },
      );
      setError(conflict);
      return Promise.reject(conflict);
    }
    const capturedToken = tokenRef.current;
    const generation = sessionGenerationRef.current;
    const record = { key, name, mandateId: mandateId || null, promise: null, unknown: false };
    if (mandateId) {
      nextVerificationGeneration(verificationGenerationsRef.current, mandateId);
      if (verificationMandateIdRef.current === mandateId) {
        setVerification(null);
        verificationMandateIdRef.current = null;
      }
    }
    setPendingOperation({ name, mandateId: mandateId || null });
    setError(null);
    const promise = Promise.resolve().then(async () => {
      try {
        const result = await operation(capturedToken);
        if (name === 'create') {
          const cleared = finishCreate(createOperation.operationId, createOperation.generation, 'CONFIRMED');
          if (!cleared) {
            record.unknown = true;
            const unknown = createUnknownState();
            unknownOperationRef.current = unknown;
            if (mountedRef.current) {
              setUnknownOperation(unknown);
              setPendingOperation({ name: 'create', mandateId: null, outcome: 'UNKNOWN' });
              setError(new ApiError(unknown.message, { code: 'outcome-unknown' }));
            }
          }
        }
        if (!isCurrentSession(capturedToken, generation)) return null;
        if (result?.mandate_id) {
          // Requests started before this transition carry older server state.
          listRequestRef.current += 1;
          setListLoading(false);
          loadedTokenRef.current = capturedToken;
          setMandates((items) => upsertMandate(items, result));
          nextVerificationGeneration(verificationGenerationsRef.current, result.mandate_id);
          if (verificationMandateIdRef.current === result.mandate_id) {
            setVerification(null);
            verificationMandateIdRef.current = null;
          }
          if (selectedIdRef.current === result.mandate_id) {
            detailRequestRef.current += 1;
            setDetailLoading(false);
            setSelectedMandate(result);
          } else if (!selectedIdRef.current && name === 'create') {
            selectedIdRef.current = result.mandate_id;
            setSelectedMandateId(result.mandate_id);
            setSelectedMandate(result);
            detailRequestRef.current += 1;
            setDetailLoading(false);
          }
        }
        return result;
      } catch (failure) {
        reportError(failure, capturedToken, generation);
        const ambiguous = name === 'create'
          ? !(failure instanceof ApiError && [401, 422].includes(failure.status))
          : failure?.code === 'network' || failure?.code === 'server' || failure?.status >= 500;
        if (name === 'create') {
          const resolved = finishCreate(
            createOperation.operationId,
            createOperation.generation,
            ambiguous ? 'UNKNOWN' : 'REJECTED',
          );
          if (ambiguous || !resolved) record.unknown = true;
        }
        if (isCurrentSession(capturedToken, generation) && ambiguous) {
          record.unknown = true;
          const unknown = name === 'create'
            ? createUnknownState(createCoordinator.status === 'blocked')
            : { name, mandateId, expectedStatus: expectedStatusFor(name), outcome: 'UNKNOWN' };
          unknownOperationRef.current = unknown;
          setUnknownOperation(unknown);
          setPendingOperation({ name, mandateId: mandateId || null, outcome: 'UNKNOWN' });
          const safeFailureMessage = failure instanceof ApiError
            ? failure.message : 'The service did not confirm the result.';
          setError(new ApiError(
            `${safeFailureMessage} The operation outcome remains unknown; do not retry it.`,
            {
              status: failure instanceof ApiError ? failure.status : null,
              code: 'outcome-unknown',
              details: failure instanceof ApiError ? failure.details : null,
            },
          ));
          await reconcileUnknown();
        }
        return null;
      } finally {
        if (mutationRef.current === record) {
          if (!record.unknown) {
            mutationRef.current = null;
            if (isCurrentSession(capturedToken, generation)) setPendingOperation(null);
          }
        }
      }
    });
    record.promise = promise;
    mutationRef.current = record;
    if (createOperation) createCoordinator.promise = promise;
    return promise;
  }, [isCurrentSession, reconcileUnknown, reportError]);

  const create = useCallback((payload) => runMutation(
    'create', null, payload, (activeToken) => createMandate(activeToken, payload),
  ), [runMutation]);
  const confirm = useCallback((id) => runMutation(
    'confirm', id, null, (activeToken) => confirmMandate(activeToken, id),
  ), [runMutation]);
  const sign = useCallback((id) => runMutation(
    'sign', id, null, (activeToken) => signMandate(activeToken, id),
  ), [runMutation]);
  const activate = useCallback((id) => runMutation(
    'activate', id, null, (activeToken) => activateMandate(activeToken, id),
  ), [runMutation]);
  const revoke = useCallback((id) => runMutation(
    'revoke', id, null, (activeToken) => revokeMandate(activeToken, id),
  ), [runMutation]);

  const verify = useCallback(async (mandateId) => {
    const capturedToken = tokenRef.current;
    if (!capturedToken || !mandateId || !mountedRef.current
      || selectedIdRef.current !== mandateId) return null;
    const generation = sessionGenerationRef.current;
    const selectionGeneration = selectionGenerationRef.current;
    const requestGeneration = nextVerificationGeneration(
      verificationGenerationsRef.current,
      mandateId,
    );
    setError(null);
    try {
      const result = await verifyMandate(capturedToken, mandateId);
      if (!isCurrentSession(capturedToken, generation)
        || selectedIdRef.current !== mandateId
        || selectionGenerationRef.current !== selectionGeneration
        || verificationGenerationsRef.current.get(mandateId) !== requestGeneration) return null;
      setVerification(result);
      verificationMandateIdRef.current = mandateId;
      return result;
    } catch (failure) {
      reportError(failure, capturedToken, generation);
      return null;
    }
  }, [isCurrentSession, reportError]);

  const hasCurrentSessionData = loadedTokenRef.current === token;
  const visibleMandates = hasCurrentSessionData ? mandates : [];
  return {
    mandates: visibleMandates,
    selectedMandateId: hasCurrentSessionData ? selectedMandateId : null,
    selectedMandate: selectedMandate?.mandate_id === selectedMandateId
      && hasCurrentSessionData ? selectedMandate : null,
    verification: verificationMandateIdRef.current === selectedMandateId
      && hasCurrentSessionData ? verification : null,
    markerStorageAvailable,
    listLoading,
    detailLoading,
    pendingOperation,
    unknownOperation,
    reconciliationLoading,
    loading: listLoading || detailLoading,
    error,
    refresh,
    selectMandate,
    create,
    confirm,
    sign,
    activate,
    revoke,
    verify,
    reconcileUnknown,
  };
}
