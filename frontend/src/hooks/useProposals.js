import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import {
  evaluateProposal,
  getProposal,
  listProposals,
  validatePolicyDecision,
  validateProposalDetail,
  validateProposalList,
} from '../services/proposals';

export function useProposals(token, onSessionExpired) {
  const [items, setItems] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [decision, setDecision] = useState(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluationRevision, setEvaluationRevision] = useState(0);
  const [error, setError] = useState(null);
  const generation = useRef(0);
  const listId = useRef(0);
  const lifecycleId = useRef(0);
  const detailId = useRef(0);
  const tokenRef = useRef(token);
  const selectedIdRef = useRef(selectedId);
  const expiredRef = useRef(onSessionExpired);
  tokenRef.current = token;
  selectedIdRef.current = selectedId;
  expiredRef.current = onSessionExpired;
  const handle = useCallback((err, captured, gen) => {
    if (captured !== tokenRef.current || gen !== generation.current) return;
    setError(err);
    if (err instanceof ApiError && err.code === 'unauthenticated') expiredRef.current?.();
  }, []);
  useEffect(() => {
    const lifecycle = ++lifecycleId.current;
    return () => {
      if (lifecycleId.current === lifecycle) {
        lifecycleId.current += 1;
        listId.current += 1;
      }
    };
  }, []);
  const fetchList = useCallback(async (errorPrefix = '') => {
    const captured = tokenRef.current;
    if (!captured) return { status: 'stale' };
    const gen = generation.current;
    const lifecycle = lifecycleId.current;
    const id = ++listId.current;
    setLoading(true); setError(null);
    try {
      const rows = validateProposalList(await listProposals(captured));
      if (captured !== tokenRef.current || gen !== generation.current
          || lifecycle !== lifecycleId.current || id !== listId.current) {
        return { status: 'stale' };
      }
      setItems(rows);
      return { status: 'success' };
    } catch (err) {
      if (captured !== tokenRef.current || gen !== generation.current
          || lifecycle !== lifecycleId.current || id !== listId.current) {
        return { status: 'stale' };
      }
      const reportedError = errorPrefix
        ? new ApiError(`${errorPrefix}: ${err.message}`, { status: err.status, code: err.code })
        : err;
      handle(reportedError, captured, gen);
      return { status: 'error' };
    }
    finally {
      if (captured === tokenRef.current && gen === generation.current
          && lifecycle === lifecycleId.current && id === listId.current) {
        setLoading(false);
      }
    }
  }, [handle]);
  const refresh = useCallback(() => fetchList(), [fetchList]);
  useEffect(() => {
    generation.current += 1; listId.current += 1; detailId.current += 1;
    selectedIdRef.current = null;
    setItems([]); setDetail(null); setDecision(null); setSelectedId(null); setError(null);
    setEvaluating(false);
    if (token) void refresh();
  }, [token, refresh]);
  const select = useCallback((id) => {
    detailId.current += 1;
    selectedIdRef.current = id;
    setSelectedId(id);
    setDetail(null);
    setDecision(null);
    setError(null);
    setEvaluating(false);
  }, []);
  useEffect(() => {
    const captured = tokenRef.current; const id = selectedId;
    if (!captured || !id) { setDetailLoading(false); return; }
    const gen = generation.current; const req = ++detailId.current;
    setDetailLoading(true);
    getProposal(captured, id).then((response) => {
      if (captured !== tokenRef.current || gen !== generation.current || req !== detailId.current) return;
      setDetail(validateProposalDetail(response, id));
    }).catch((err) => {
      if (captured === tokenRef.current && gen === generation.current && req === detailId.current && selectedId === id) handle(err, captured, gen);
    }).finally(() => {
      if (captured === tokenRef.current && gen === generation.current && req === detailId.current) setDetailLoading(false);
    });
    return () => { detailId.current += 1; };
  }, [selectedId, token, handle]);
  const evaluate = useCallback(async () => {
    const captured = tokenRef.current; const id = selectedId;
    if (!captured || !id || !detail) return null;
    const gen = generation.current; const req = detailId.current;
    setEvaluating(true); setError(null);
    try {
      const response = await evaluateProposal(captured, id);
      if (captured !== tokenRef.current || gen !== generation.current
          || req !== detailId.current || selectedIdRef.current !== id) return null;
      const result = validatePolicyDecision(response, id, detail.mandate_id);
      setDecision(result);
      setEvaluationRevision((revision) => revision + 1);
      try {
        const [detailResponse, listResult] = await Promise.all([
          getProposal(captured, id), fetchList('Evaluation completed, but proposal status could not be refreshed'),
        ]);
        if (captured === tokenRef.current && gen === generation.current
            && req === detailId.current && selectedIdRef.current === id) {
          if (listResult.status === 'error') {
            setDetail(null);
          } else {
            setDetail(validateProposalDetail(detailResponse, id, detail.mandate_id));
          }
        }
      } catch (refreshError) {
        if (captured === tokenRef.current && gen === generation.current
            && req === detailId.current && selectedIdRef.current === id) {
          setDetail(null);
          handle(new ApiError(`Evaluation completed, but proposal status could not be refreshed: ${refreshError.message}`, { status: refreshError.status, code: refreshError.code }), captured, gen);
        }
      }
      return result;
    } catch (err) {
      if (captured === tokenRef.current && gen === generation.current
          && req === detailId.current && selectedIdRef.current === id) {
        handle(err, captured, gen);
      }
      return null;
    } finally {
      if (captured === tokenRef.current && gen === generation.current
          && req === detailId.current && selectedIdRef.current === id) {
        setEvaluating(false);
      }
    }
  }, [detail, fetchList, handle, selectedId]);
  return {
    items, selectedId, detail, decision, loading, detailLoading, evaluating, error,
    evaluationRevision, refresh, select, evaluate,
  };
}
