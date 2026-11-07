import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../services/api';
import { __resetMandateCreateCoordinatorForTests, useMandates } from '../hooks/useMandates';

const api = vi.hoisted(() => ({
  activateMandate: vi.fn(), confirmMandate: vi.fn(), createMandate: vi.fn(),
  getMandate: vi.fn(), listMandates: vi.fn(), revokeMandate: vi.fn(),
  signMandate: vi.fn(), verifyMandate: vi.fn(),
}));
vi.mock('../services/mandates', () => api);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const mandate = (id, status = 'DRAFT') => ({
  mandate_id: id, schema_version: '1.0', status, max_total_budget: '5', currency: 'USD',
  allowed_merchants: ['shop.example'], allowed_categories: ['books'], max_item_count: 1,
  max_quantity_per_item: 1, max_price_per_item: null, allow_subscriptions: false,
  require_user_approval: true, expires_at: '2030-01-01T00:00:00Z',
  created_at: '2029-01-01T00:00:00Z', signature: null, signed_at: null,
  signature_algorithm: null, owner_user_id: null,
});

describe('useMandates', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    api.listMandates.mockResolvedValue([]);
    __resetMandateCreateCoordinatorForTests();
  });

  it('loads lists and details for the current session and selected mandate only', async () => {
    const oldDetail = deferred();
    const newDetail = deferred();
    api.getMandate.mockImplementation((token, id) => id === 'old' ? oldDetail.promise : newDetail.promise);
    const { result, rerender } = renderHook(({ token }) => useMandates(token), {
      initialProps: { token: 'token-1' },
    });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('token-1'));
    act(() => result.current.selectMandate('old'));
    await waitFor(() => expect(api.getMandate).toHaveBeenCalledWith('token-1', 'old'));
    act(() => result.current.selectMandate('new'));
    await waitFor(() => expect(api.getMandate).toHaveBeenCalledWith('token-1', 'new'));
    newDetail.resolve(mandate('new'));
    await waitFor(() => expect(result.current.selectedMandate).toEqual(mandate('new')));
    oldDetail.resolve(mandate('old'));
    await act(async () => {});
    expect(result.current.selectedMandateId).toBe('new');
    expect(result.current.selectedMandate).toEqual(mandate('new'));

    const staleList = deferred();
    api.listMandates.mockReturnValueOnce(staleList.promise).mockResolvedValueOnce([mandate('new-session')]);
    act(() => { void result.current.refresh(); });
    rerender({ token: 'token-2' });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('token-2'));
    staleList.resolve([mandate('old-session')]);
    await waitFor(() => expect(result.current.mandates).toEqual([mandate('new-session')]));
  });

  it('updates mandate state only from successful backend responses', async () => {
    api.listMandates.mockResolvedValue([mandate('m1')]);
    api.activateMandate.mockResolvedValue(mandate('m1', 'ACTIVE'));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(result.current.mandates).toEqual([mandate('m1')]));
    await act(async () => { await result.current.activate('m1'); });
    expect(result.current.mandates).toEqual([mandate('m1', 'ACTIVE')]);

    api.revokeMandate.mockRejectedValue(new ApiError('Denied', { status: 409, code: 'http' }));
    await act(async () => { await result.current.revoke('m1'); });
    expect(result.current.mandates).toEqual([mandate('m1', 'ACTIVE')]);
    expect(result.current.error).toMatchObject({ status: 409 });
  });

  it('distinguishes 401 from 403 and calls session expiry only for 401', async () => {
    const onSessionExpired = vi.fn();
    const { result, rerender } = renderHook(({ token }) => useMandates(token, onSessionExpired), {
      initialProps: { token: 't1' },
    });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t1'));
    api.createMandate.mockRejectedValueOnce(new ApiError('Unauthenticated', { status: 401, code: 'unauthenticated' }));
    await act(async () => { await result.current.create({}); });
    expect(onSessionExpired).toHaveBeenCalledTimes(1);
    expect(result.current.error.code).toBe('unauthenticated');

    rerender({ token: 't2' });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t2'));
    api.createMandate.mockRejectedValueOnce(new ApiError('Forbidden', { status: 403, code: 'forbidden' }));
    await act(async () => { await result.current.create({}); });
    expect(onSessionExpired).toHaveBeenCalledTimes(1);
    expect(result.current.unknownOperation).toMatchObject({ name: 'create', outcome: 'UNKNOWN' });
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).not.toBeNull();
  });

  it('deduplicates concurrent mutation attempts and drops results from an old token', async () => {
    const pending = deferred();
    api.createMandate.mockReturnValue(pending.promise);
    const { result, rerender } = renderHook(({ token }) => useMandates(token), {
      initialProps: { token: 'old-token' },
    });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('old-token'));
    let first;
    let second;
    act(() => {
      first = result.current.create({ max_total_budget: '5' });
      second = result.current.create({ max_total_budget: '5' });
    });
    await waitFor(() => expect(api.createMandate).toHaveBeenCalledTimes(1));
    expect(api.createMandate).toHaveBeenCalledTimes(1);
    expect(first).toBe(second);
    rerender({ token: 'new-token' });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('new-token'));
    pending.resolve(mandate('old-result'));
    await act(async () => { await first; });
    expect(result.current.mandates).toEqual([]);
    expect(result.current.selectedMandate).toBeNull();
  });

  it('shares one create request across mounted hook instances', async () => {
    const pending = deferred();
    api.createMandate.mockReturnValue(pending.promise);
    const first = renderHook(() => useMandates('shared-token'));
    const second = renderHook(() => useMandates('shared-token'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(2));
    let firstPromise;
    let secondPromise;
    act(() => {
      firstPromise = first.result.current.create({ max_total_budget: '5' });
      secondPromise = second.result.current.create({ max_total_budget: '5' });
    });
    expect(firstPromise).toBe(secondPromise);
    await waitFor(() => expect(api.createMandate).toHaveBeenCalledTimes(1));
    const created = mandate('shared-created');
    pending.resolve(created);
    await act(async () => { await Promise.all([firstPromise, secondPromise]); });
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).toBeNull();
    expect(first.result.current.mandates).toContainEqual(created);
    expect(second.result.current.unknownOperation).toBeNull();
    expect(second.result.current.pendingOperation).toBeNull();
  });

  it('does not acquire a create marker while a local lifecycle mutation is pending', async () => {
    const pendingRevoke = deferred();
    api.revokeMandate.mockReturnValue(pendingRevoke.promise);
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(1));
    let revokePromise;
    act(() => { revokePromise = result.current.revoke('m1'); });
    await waitFor(() => expect(api.revokeMandate).toHaveBeenCalledTimes(1));
    let createError;
    await act(async () => {
      try { await result.current.create({ max_total_budget: '5' }); } catch (error) { createError = error; }
    });
    expect(createError).toMatchObject({ code: 'operation-pending' });
    expect(api.createMandate).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).toBeNull();
    expect(result.current.unknownOperation).toBeNull();
    expect(result.current.pendingOperation).toMatchObject({ name: 'revoke', mandateId: 'm1' });
    pendingRevoke.resolve(mandate('m1', 'REVOKED'));
    await act(async () => { await revokePromise; });
    expect(result.current.pendingOperation).toBeNull();
  });

  it('clears the second hook pending state after shared create receives 401 or 422', async () => {
    for (const status of [401, 422]) {
      api.createMandate.mockRejectedValueOnce(new ApiError('Rejected', {
        status, code: status === 401 ? 'unauthenticated' : 'validation',
      }));
      const first = renderHook(() => useMandates('shared-token'));
      const second = renderHook(() => useMandates('shared-token'));
      await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(status === 401 ? 2 : 4));
      let firstPromise;
      let secondPromise;
      act(() => {
        firstPromise = first.result.current.create({ max_total_budget: '5' });
        secondPromise = second.result.current.create({ max_total_budget: '5' });
      });
      expect(firstPromise).toBe(secondPromise);
      await act(async () => { await Promise.all([firstPromise, secondPromise]); });
      expect(api.createMandate).toHaveBeenCalledTimes(status === 401 ? 1 : 2);
      expect(sessionStorage.getItem('leash.mandates.create.unknown')).toBeNull();
      expect(second.result.current.pendingOperation).toBeNull();
      expect(second.result.current.unknownOperation).toBeNull();
      first.unmount();
      second.unmount();
    }
  });

  it('keeps the create marker when an old response no longer owns its operation marker', async () => {
    const pending = deferred();
    api.createMandate.mockReturnValue(pending.promise);
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(1));
    let createPromise;
    act(() => { createPromise = result.current.create({ max_total_budget: '5' }); });
    await waitFor(() => expect(api.createMandate).toHaveBeenCalledTimes(1));
    sessionStorage.setItem('leash.mandates.create.unknown', JSON.stringify({
      version: 2, operation_id: 'newer-operation',
    }));
    pending.resolve(mandate('old-response'));
    await act(async () => { await createPromise; });
    expect(JSON.parse(sessionStorage.getItem('leash.mandates.create.unknown')))
      .toEqual({ version: 2, operation_id: 'newer-operation' });
    expect(result.current.unknownOperation).toMatchObject({ name: 'create' });
  });

  it('clears create marker only for verified pre-handler rejection statuses', async () => {
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(1));
    api.createMandate.mockRejectedValueOnce(new ApiError('Invalid payload', { status: 422, code: 'validation' }));
    await act(async () => { await result.current.create({}); });
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).toBeNull();
    api.createMandate.mockRejectedValueOnce(new ApiError('Timeout', { status: 408, code: 'http' }));
    await act(async () => { await result.current.create({}); });
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).not.toBeNull();
    expect(result.current.unknownOperation).toMatchObject({ name: 'create', outcome: 'UNKNOWN' });
  });

  it('rejects a different concurrent mutation without reporting it successful', async () => {
    const pendingRevoke = deferred();
    api.revokeMandate.mockReturnValue(pendingRevoke.promise);
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    let revokePromise;
    let confirmPromise;
    act(() => {
      revokePromise = result.current.revoke('m1');
      confirmPromise = result.current.confirm('m1');
    });
    await expect(confirmPromise).rejects.toMatchObject({ code: 'operation-pending' });
    expect(api.confirmMandate).not.toHaveBeenCalled();
    expect(result.current.pendingOperation).toMatchObject({ name: 'revoke', mandateId: 'm1' });
    expect(result.current.error.code).toBe('operation-pending');

    pendingRevoke.resolve(mandate('m1', 'REVOKED'));
    await act(async () => { await revokePromise; });
    expect(result.current.pendingOperation).toBeNull();
    expect(result.current.mandates).toContainEqual(mandate('m1', 'REVOKED'));
  });

  it('preserves an unrelated selected detail request during another mandate mutation', async () => {
    const pendingDetail = deferred();
    api.getMandate.mockReturnValue(pendingDetail.promise);
    api.revokeMandate.mockResolvedValue(mandate('B', 'REVOKED'));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    act(() => result.current.selectMandate('A'));
    await waitFor(() => expect(api.getMandate).toHaveBeenCalledWith('t', 'A'));
    await act(async () => { await result.current.revoke('B'); });
    expect(result.current.detailLoading).toBe(true);
    pendingDetail.resolve(mandate('A'));
    await waitFor(() => expect(result.current.selectedMandate).toEqual(mandate('A')));
    expect(result.current.selectedMandateId).toBe('A');
    expect(result.current.detailLoading).toBe(false);
  });

  it('uses the successful mutation response for the selected mandate over stale detail', async () => {
    const pendingDetail = deferred();
    api.getMandate.mockReturnValue(pendingDetail.promise);
    api.activateMandate.mockResolvedValue(mandate('A', 'ACTIVE'));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    act(() => result.current.selectMandate('A'));
    await waitFor(() => expect(api.getMandate).toHaveBeenCalledWith('t', 'A'));
    await act(async () => { await result.current.activate('A'); });
    expect(result.current.selectedMandate).toEqual(mandate('A', 'ACTIVE'));
    expect(result.current.detailLoading).toBe(false);
    pendingDetail.resolve(mandate('A', 'DRAFT'));
    await act(async () => {});
    expect(result.current.selectedMandate).toEqual(mandate('A', 'ACTIVE'));
  });

  it('ignores a verification response after selection changes A to B and back to A', async () => {
    const pendingVerification = deferred();
    api.verifyMandate.mockReturnValue(pendingVerification.promise);
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    act(() => result.current.selectMandate('A'));
    let verifyPromise;
    act(() => { verifyPromise = result.current.verify('A'); });
    await waitFor(() => expect(api.verifyMandate).toHaveBeenCalledWith('t', 'A'));
    act(() => result.current.selectMandate('B'));
    act(() => result.current.selectMandate('A'));
    pendingVerification.resolve({ mandate_id: 'A', valid: false, reason: 'stale result' });
    await act(async () => { await verifyPromise; });
    expect(result.current.verification).toBeNull();
  });

  it('invalidates a pending verification for A when A is mutated while B is selected', async () => {
    const pendingVerification = deferred();
    api.verifyMandate.mockReturnValue(pendingVerification.promise);
    api.activateMandate.mockResolvedValue(mandate('A', 'ACTIVE'));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    act(() => result.current.selectMandate('A'));
    let verifyPromise;
    act(() => { verifyPromise = result.current.verify('A'); });
    await waitFor(() => expect(api.verifyMandate).toHaveBeenCalledWith('t', 'A'));
    act(() => result.current.selectMandate('B'));
    await act(async () => { await result.current.activate('A'); });
    act(() => result.current.selectMandate('A'));
    pendingVerification.resolve({ mandate_id: 'A', valid: true, reason: 'pre-mutation result' });
    await act(async () => { await verifyPromise; });
    expect(result.current.verification).toBeNull();
  });

  it('keeps a lost create response UNKNOWN after list refresh and blocks duplicate actions', async () => {
    const created = {
      ...mandate('server-created'),
      max_total_budget: '5',
      allowed_merchants: ['shop.example'],
      allowed_categories: ['books'],
    };
    api.listMandates.mockResolvedValueOnce([]).mockResolvedValueOnce([created]);
    api.createMandate.mockRejectedValue(new ApiError('Network', { code: 'network' }));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(1));
    await act(async () => { await result.current.create({ max_total_budget: '5' }); });
    expect(api.listMandates).toHaveBeenCalledTimes(2);
    expect(result.current.mandates).toEqual([created]);
    expect(result.current.unknownOperation).toMatchObject({ name: 'create' });
    expect(result.current.pendingOperation).toMatchObject({ outcome: 'UNKNOWN' });
    expect(sessionStorage.length).toBe(1);
    expect(sessionStorage.key(0)).toBe('leash.mandates.create.unknown');
    const marker = JSON.parse(sessionStorage.getItem('leash.mandates.create.unknown'));
    expect(marker).toMatchObject({ version: 2 });
    expect(typeof marker.operation_id).toBe('string');
    expect(Object.keys(marker).sort()).toEqual(['operation_id', 'version']);

    let duplicateCreateError;
    await act(async () => {
      try { await result.current.create({ max_total_budget: '5' }); } catch (error) { duplicateCreateError = error; }
    });
    expect(duplicateCreateError).toMatchObject({ code: 'outcome-unknown' });
    let blockedActionError;
    await act(async () => {
      try { await result.current.confirm('server-created'); } catch (error) { blockedActionError = error; }
    });
    expect(blockedActionError).toMatchObject({ code: 'outcome-unknown' });
    expect(api.createMandate).toHaveBeenCalledTimes(1);
    expect(api.confirmMandate).not.toHaveBeenCalled();
    expect(result.current.unknownOperation).toMatchObject({ name: 'create' });
  });

  it('persists an ambiguous create lock across unmount and remount', async () => {
    api.listMandates.mockResolvedValue([]);
    api.createMandate.mockRejectedValue(new ApiError('Network', { code: 'network' }));
    const first = renderHook(() => useMandates('same-session'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(1));
    await act(async () => { await first.result.current.create({ max_total_budget: '9' }); });
    expect(JSON.parse(sessionStorage.getItem('leash.mandates.create.unknown'))).toMatchObject({ version: 2 });
    first.unmount();

    const second = renderHook(() => useMandates('same-session'));
    await waitFor(() => expect(second.result.current.unknownOperation).toMatchObject({
      name: 'create', outcome: 'UNKNOWN', durable: true,
    }));
    let blocked;
    await act(async () => {
      try { await second.result.current.create({ max_total_budget: '9' }); } catch (error) { blocked = error; }
    });
    expect(blocked).toMatchObject({ code: 'outcome-unknown' });
    expect(api.createMandate).toHaveBeenCalledTimes(1);
  });

  it('blocks create during fresh application initialization when the marker is already stored', async () => {
    sessionStorage.setItem('leash.mandates.create.unknown', JSON.stringify({ version: 2, operation_id: 'opaque' }));
    const { result } = renderHook(() => useMandates('restored-session'));
    await waitFor(() => expect(result.current.unknownOperation).toMatchObject({
      name: 'create', outcome: 'UNKNOWN', durable: true,
    }));
    let blocked;
    await act(async () => {
      try { await result.current.create({ max_total_budget: '8' }); } catch (error) { blocked = error; }
    });
    expect(blocked).toMatchObject({ code: 'outcome-unknown' });
    expect(api.createMandate).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem('leash.mandates.create.unknown'))).toMatchObject({ version: 2 });
  });

  it('retains the UNKNOWN marker across hook reinitialization and session token changes', async () => {
    sessionStorage.setItem('leash.mandates.create.unknown', JSON.stringify({ version: 2, operation_id: 'opaque' }));
    const { result, rerender } = renderHook(({ token }) => useMandates(token), {
      initialProps: { token: 'session-one' },
    });
    await waitFor(() => expect(result.current.unknownOperation).toMatchObject({ name: 'create' }));
    rerender({ token: 'session-two' });
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('session-two'));
    expect(JSON.parse(sessionStorage.getItem('leash.mandates.create.unknown'))).toMatchObject({ version: 2 });
    let blocked;
    await act(async () => {
      try { await result.current.create({ max_total_budget: '4' }); } catch (error) { blocked = error; }
    });
    expect(blocked).toMatchObject({ code: 'outcome-unknown' });
    expect(api.createMandate).not.toHaveBeenCalled();
  });

  it('keeps a create UNKNOWN when a similar-looking mandate appears or reconciliation fails', async () => {
    const similar = {
      ...mandate('unrelated'),
      max_total_budget: '5',
      allowed_merchants: ['shop.example'],
      allowed_categories: ['books'],
    };
    api.listMandates.mockResolvedValueOnce([]).mockResolvedValueOnce([similar]);
    api.createMandate.mockRejectedValueOnce(new ApiError('Network', { code: 'network' }));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledTimes(1));
    await act(async () => { await result.current.create({
      max_total_budget: '5', allowed_merchants: ['shop.example'], allowed_categories: ['books'],
    }); });
    expect(result.current.mandates).toEqual([similar]);
    expect(result.current.unknownOperation).toMatchObject({ name: 'create', outcome: 'UNKNOWN' });

    api.listMandates.mockRejectedValueOnce(new ApiError('Unavailable', { code: 'server', status: 503 }));
    await act(async () => { await result.current.reconcileUnknown(); });
    expect(result.current.unknownOperation).toMatchObject({ name: 'create', outcome: 'UNKNOWN' });
    expect(api.createMandate).toHaveBeenCalledTimes(1);
  });

  it('fails closed if the create marker cannot be stored', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage denied');
    });
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    let createPromise;
    act(() => { createPromise = result.current.create({ max_total_budget: '2' }); });
    await expect(createPromise).rejects.toMatchObject({ code: 'storage' });
    expect(api.createMandate).not.toHaveBeenCalled();
    expect(result.current.markerStorageAvailable).toBe(false);
    expect(result.current.unknownOperation).toMatchObject({
      name: 'create', storageUnavailable: true,
    });
  });

  it('fails closed when a confirmed create marker cannot be removed', async () => {
    api.createMandate.mockResolvedValue(mandate('created'));
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('storage denied');
    });
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    await act(async () => { await result.current.create({ max_total_budget: '5' }); });
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).not.toBeNull();
    expect(result.current.unknownOperation).toMatchObject({ name: 'create' });
    expect(result.current.markerStorageAvailable).toBe(false);
    let blocked;
    await act(async () => {
      try { await result.current.create({ max_total_budget: '5' }); } catch (error) { blocked = error; }
    });
    expect(blocked).toMatchObject({ code: 'outcome-unknown' });
    expect(api.createMandate).toHaveBeenCalledTimes(1);
  });

  it('fails closed when marker storage cannot be read', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage denied');
    });
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(result.current.markerStorageAvailable).toBe(false));
    let blocked;
    await act(async () => {
      try { await result.current.create({ max_total_budget: '2' }); } catch (error) { blocked = error; }
    });
    expect(blocked).toMatchObject({ code: 'outcome-unknown' });
    expect(api.createMandate).not.toHaveBeenCalled();
  });

  it('does not create a marker for a known successful create and stores no sensitive fields', async () => {
    api.createMandate.mockResolvedValue(mandate('created', 'DRAFT'));
    const { result } = renderHook(() => useMandates('session-secret-token'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('session-secret-token'));
    await act(async () => { await result.current.create({
      max_total_budget: '13', owner_user_id: 'private-user',
    }); });
    expect(sessionStorage.getItem('leash.mandates.create.unknown')).toBeNull();
    expect([...Array(sessionStorage.length)].map((_, index) => sessionStorage.key(index)))
      .not.toContain('session-secret-token');
    const storedValues = [...Array(sessionStorage.length)]
      .map((_, index) => sessionStorage.getItem(sessionStorage.key(index)));
    expect(storedValues).not.toContain('private-user');
    expect(storedValues).not.toContain('13');
    expect(result.current.unknownOperation).toBeNull();
  });

  it('confirms an ambiguous lifecycle transition only after authoritative state matches', async () => {
    api.activateMandate.mockRejectedValue(new ApiError('Unavailable', { status: 503, code: 'server' }));
    api.getMandate.mockResolvedValue(mandate('m1', 'ACTIVE'));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    await act(async () => { await result.current.activate('m1'); });
    expect(api.activateMandate).toHaveBeenCalledTimes(1);
    expect(api.getMandate).toHaveBeenCalledWith('t', 'm1');
    expect(result.current.unknownOperation).toBeNull();
    expect(result.current.pendingOperation).toBeNull();
    expect(result.current.mandates).toContainEqual(mandate('m1', 'ACTIVE'));
  });

  it('retains UNKNOWN and blocks other mutations when reconciliation is inconclusive', async () => {
    api.activateMandate.mockRejectedValue(new ApiError('Network', { code: 'network' }));
    api.getMandate.mockResolvedValue(mandate('m1', 'SIGNED'));
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    await act(async () => { await result.current.activate('m1'); });
    expect(result.current.unknownOperation).toMatchObject({
      name: 'activate', mandateId: 'm1', expectedStatus: 'ACTIVE',
    });
    expect(result.current.pendingOperation).toMatchObject({ outcome: 'UNKNOWN' });
    await act(async () => { await result.current.reconcileUnknown(); });
    expect(result.current.unknownOperation).toMatchObject({ name: 'activate' });
    let blockedActionError;
    await act(async () => {
      try { await result.current.revoke('m1'); } catch (error) { blockedActionError = error; }
    });
    expect(blockedActionError).toMatchObject({ code: 'outcome-unknown' });
    expect(api.revokeMandate).not.toHaveBeenCalled();
    expect(api.activateMandate).toHaveBeenCalledTimes(1);
    expect(result.current.unknownOperation).toMatchObject({ name: 'activate' });
  });

  it('retains safe validation details and generic server errors on failure', async () => {
    const { result } = renderHook(() => useMandates('t'));
    await waitFor(() => expect(api.listMandates).toHaveBeenCalledWith('t'));
    api.createMandate.mockRejectedValueOnce(new ApiError('Validation', {
      status: 422,
      code: 'validation',
      details: [{ loc: ['body', 'expires_at'], msg: 'invalid', type: 'value_error' }],
    }));
    await act(async () => { await result.current.create({}); });
    expect(result.current.error.details).toEqual([
      { loc: ['body', 'expires_at'], msg: 'invalid', type: 'value_error' },
    ]);

    api.createMandate.mockRejectedValueOnce(new ApiError('Service unavailable', {
      status: 500, code: 'server',
    }));
    await act(async () => { await result.current.create({}); });
    expect(result.current.error.message).toMatch(/service unavailable.*outcome remains unknown/i);
    expect(result.current.error.details).toBeNull();
  });
});
