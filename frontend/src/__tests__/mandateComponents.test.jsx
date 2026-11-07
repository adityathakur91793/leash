import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { MandateCreateForm } from '../components/MandateCreateForm';
import { MandateDetail } from '../components/MandateDetail';
import { MandateList } from '../components/MandateList';

function fillForm() {
  fireEvent.change(screen.getByLabelText('Total budget'), { target: { value: '25.00' } });
}

const mandate = {
  mandate_id: 'man_abc123', status: 'DRAFT', currency: 'USD', max_total_budget: '25.00',
  allowed_merchants: ['shop.example'], allowed_categories: ['books'], max_item_count: 2,
  max_quantity_per_item: 1, max_price_per_item: null, allow_subscriptions: false,
  require_user_approval: true, created_at: '2029-01-01T00:00:00Z', expires_at: '2030-01-01T00:00:00Z',
};

function RevokeHarness({ onRevoke, status = 'ACTIVE', onStatusChange }) {
  const [currentStatus, setCurrentStatus] = useState(status);
  useEffect(() => setCurrentStatus(status), [status]);
  async function revoke(id) {
    const result = await onRevoke(id);
    if (result?.status) {
      setCurrentStatus(result.status);
      onStatusChange?.(result.status);
    }
    return result;
  }
  return <MandateDetail
    mandate={{ ...mandate, status: currentStatus }}
    verification={null}
    loading={false}
    busy={false}
    unknown={false}
    onRefresh={vi.fn()}
    onVerify={vi.fn()}
    onConfirm={vi.fn()}
    onSign={vi.fn()}
    onActivate={vi.fn()}
    onRevoke={revoke}
  />;
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

describe('mandate management components', () => {
  it('validates then reviews permissions before create and shows create loading', async () => {
    const onCreate = vi.fn().mockResolvedValue({ mandate_id: mandate.mandate_id });
    const onCreated = vi.fn();
    render(<MandateCreateForm onCreate={onCreate} onCreated={onCreated} disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: /review permissions/i }));
    expect(await screen.findByText(/enter a positive amount/i)).toBeInTheDocument();

    fillForm();
    fireEvent.change(screen.getByLabelText('Allowed merchants'), { target: { value: 'shop.example' } });
    fireEvent.change(screen.getByLabelText('Allowed categories'), { target: { value: 'books' } });
    fireEvent.change(screen.getByLabelText('Expiry date and time'), { target: { value: '2030-01-01T00:00' } });
    fireEvent.click(screen.getByRole('button', { name: /review permissions/i }));
    expect(await screen.findByRole('heading', { name: 'Review before creating' })).toBeInTheDocument();
    expect(screen.getByText('shop.example')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Create mandate' }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate.mock.calls[0][0]).not.toHaveProperty('owner_user_id');
    expect(onCreate.mock.calls[0][0]).not.toHaveProperty('signature');
    expect(onCreated).toHaveBeenCalledWith(mandate.mandate_id);
  });

  it('keeps unknown create outcomes blocked and does not submit again', () => {
    const onCreate = vi.fn();
    render(<MandateCreateForm onCreate={onCreate} disabled />);
    expect(screen.getByRole('alert')).toHaveTextContent(/unknown outcome/i);
    expect(screen.getByRole('button', { name: /review permissions/i })).toBeDisabled();
  });

  it('shows an explicit verification failure for backend valid:false', async () => {
    function DetailHarness() {
      const [verification, setVerification] = useState(null);
      return <MandateDetail
        mandate={{ ...mandate, status: 'ACTIVE' }}
        verification={verification}
        onVerify={async () => setVerification({ valid: false, reason: 'Signature mismatch.' })}
        onRefresh={vi.fn()} onConfirm={vi.fn()} onSign={vi.fn()} onActivate={vi.fn()} onRevoke={vi.fn()}
      />;
    }
    render(<DetailHarness />);
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    expect(await screen.findByText('Verification failed')).toBeInTheDocument();
    expect(screen.getByText('Signature mismatch.')).toBeInTheDocument();
    expect(screen.queryByText(/signature.*[a-f0-9]{16}/i)).not.toBeInTheDocument();
  });

  it('offers only the next backend lifecycle action and allows revoking expired mandates', () => {
    const onConfirm = vi.fn();
    const props = {
      mandate, verification: null, onRefresh: vi.fn(), onVerify: vi.fn(),
      onConfirm, onSign: vi.fn(), onActivate: vi.fn(), onRevoke: vi.fn(),
    };
    const view = render(<MandateDetail {...props} />);
    expect(screen.getByRole('button', { name: 'Confirm permissions' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Sign mandate' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Activate mandate' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm permissions' }));
    expect(onConfirm).toHaveBeenCalledWith(mandate.mandate_id);
    view.rerender(<MandateDetail {...props} mandate={{ ...mandate, status: 'EXPIRED' }} />);
    expect(screen.getByRole('button', { name: 'Revoke mandate' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Confirm permissions' })).not.toBeInTheDocument();
  });

  it('opens a clearly labeled revocation dialog without calling the backend', () => {
    const onRevoke = vi.fn();
    render(<RevokeHarness onRevoke={onRevoke} />);

    fireEvent.click(screen.getByRole('button', { name: 'Revoke mandate' }));

    const dialog = screen.getByRole('dialog', { name: 'Permanently revoke this mandate?' });
    expect(dialog).toHaveTextContent('This action is permanent');
    expect(dialog).toHaveTextContent('will no longer authorize future purchases');
    expect(dialog).toHaveTextContent('revocation cannot be undone');
    expect(dialog).toHaveTextContent(mandate.mandate_id);
    expect(onRevoke).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  });

  it('cancels revocation without sending a request and restores focus', () => {
    const onRevoke = vi.fn();
    render(<RevokeHarness onRevoke={onRevoke} />);
    const revokeButton = screen.getByRole('button', { name: 'Revoke mandate' });

    revokeButton.focus();
    fireEvent.click(revokeButton);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(onRevoke).not.toHaveBeenCalled();
    expect(revokeButton).toHaveFocus();
  });

  it('confirms revocation once and closes only after the backend reports REVOKED', async () => {
    const onRevoke = vi.fn().mockResolvedValue({ ...mandate, status: 'REVOKED' });
    render(<RevokeHarness onRevoke={onRevoke} />);

    fireEvent.click(screen.getByRole('button', { name: 'Revoke mandate' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Revocation' }));

    await waitFor(() => {
      expect(onRevoke).toHaveBeenCalledTimes(1);
      expect(onRevoke).toHaveBeenCalledWith(mandate.mandate_id);
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(screen.getByText('This mandate was revoked and cannot be restored.')).toBeInTheDocument();
    expect(screen.getByText('REVOKED')).toBeInTheDocument();
  });

  it('prevents duplicate revoke requests and shows a pending state', async () => {
    const pending = deferred();
    const onRevoke = vi.fn(() => pending.promise);
    render(<RevokeHarness onRevoke={onRevoke} />);

    fireEvent.click(screen.getByRole('button', { name: 'Revoke mandate' }));
    const confirm = screen.getByRole('button', { name: 'Confirm Revocation' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    expect(onRevoke).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Revoking…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    pending.resolve({ ...mandate, status: 'REVOKED' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(onRevoke).toHaveBeenCalledTimes(1);
  });

  it('keeps the authoritative status unchanged and offers recovery when revoke is not confirmed', async () => {
    const onRevoke = vi.fn().mockResolvedValue(null);
    render(<RevokeHarness onRevoke={onRevoke} />);

    fireEvent.click(screen.getByRole('button', { name: 'Revoke mandate' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Revocation' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/did not confirm revocation/i);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();
    expect(screen.queryByText(/this mandate was revoked/i)).not.toBeInTheDocument();
    expect(onRevoke).toHaveBeenCalledTimes(1);
  });

  it('closes after reconciliation supplies authoritative REVOKED state without retrying', async () => {
    let publishStatus;
    const onRevoke = vi.fn().mockResolvedValue(null);
    function IntegrationHarness() {
      const [status, setStatus] = useState('ACTIVE');
      publishStatus = setStatus;
      return <RevokeHarness status={status} onRevoke={onRevoke} />;
    }
    render(<IntegrationHarness />);
    fireEvent.click(screen.getByRole('button', { name: 'Revoke mandate' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Revocation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/did not confirm revocation/i);
    expect(onRevoke).toHaveBeenCalledTimes(1);
    publishStatus('REVOKED');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('REVOKED')).toBeInTheDocument();
    expect(onRevoke).toHaveBeenCalledTimes(1);
  });

  it('does not offer revoke for a mandate already marked REVOKED', () => {
    render(<RevokeHarness onRevoke={vi.fn()} status="REVOKED" />);
    expect(screen.queryByRole('button', { name: 'Revoke mandate' })).not.toBeInTheDocument();
  });

  it('renders useful list loading and empty states', () => {
    const props = { mandates: [], selectedId: null, onSelect: vi.fn(), onRefresh: vi.fn(), onCreate: vi.fn() };
    const view = render(<MandateList {...props} loading />);
    expect(screen.getByRole('status')).toHaveTextContent(/loading your mandates/i);
    view.rerender(<MandateList {...props} loading={false} />);
    expect(screen.getByText('No mandates yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /create your first mandate/i }));
    expect(props.onCreate).toHaveBeenCalledTimes(1);
  });
});
