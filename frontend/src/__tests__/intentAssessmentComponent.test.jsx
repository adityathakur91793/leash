import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { IntentAssessmentCard } from '../components/IntentAssessmentCard';

const baseAssessment = {
  status: 'VALID',
  risk_level: 'LOW',
  finding_codes: ['BUDGET_TENSION'],
  affected_fields: ['mandate_budget'],
  created_at: '2030-01-02T03:04:05+00:00',
  is_current: true,
};

function assessmentState(assessment = baseAssessment, overrides = {}) {
  return {
    assessment,
    error: null,
    loaded: true,
    loading: false,
    unavailable: assessment === null,
    refresh: vi.fn().mockResolvedValue({ status: 'success' }),
    ...overrides,
  };
}

describe('IntentAssessmentCard', () => {
  it.each([
    ['LOW', 'Risk level: Low'],
    ['MEDIUM', 'Risk level: Medium'],
    ['HIGH', 'Risk level: High'],
    ['UNCERTAIN', 'Risk level: Uncertain'],
  ])('renders backend risk level %s with a text label', (risk, label) => {
    render(<IntentAssessmentCard assessmentState={assessmentState({ ...baseAssessment, risk_level: risk })} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it.each([
    ['VALID', 'Valid assessment'],
    ['FAILED', 'Assessment failed'],
    ['STALE', 'Stale assessment'],
  ])('renders backend status %s distinctly from freshness', (status, label) => {
    render(<IntentAssessmentCard assessmentState={assessmentState({
      ...baseAssessment, status, is_current: false,
    })} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText('Not current')).toBeInTheDocument();
  });

  it('shows current freshness only from the backend is_current value', () => {
    const { rerender } = render(
      <IntentAssessmentCard assessmentState={assessmentState({ ...baseAssessment, is_current: true })} />,
    );
    expect(screen.getByText('Current assessment')).toBeInTheDocument();
    rerender(
      <IntentAssessmentCard assessmentState={assessmentState({ ...baseAssessment, is_current: false })} />,
    );
    expect(screen.getByText('Not current')).toBeInTheDocument();
  });

  it('renders exact finding and affected-field codes with deterministic readable labels', () => {
    render(<IntentAssessmentCard assessmentState={assessmentState({
      ...baseAssessment,
      finding_codes: ['BUDGET_TENSION', 'PRICE_INCONSISTENCY'],
      affected_fields: ['mandate_budget', 'unit_price'],
    })} />);
    expect(screen.getByText('Budget tension')).toBeInTheDocument();
    expect(screen.getByText('BUDGET_TENSION')).toBeInTheDocument();
    expect(screen.getByText('Price inconsistency')).toBeInTheDocument();
    expect(screen.getByText('PRICE_INCONSISTENCY')).toBeInTheDocument();
    expect(screen.getByText('Mandate budget')).toBeInTheDocument();
    expect(screen.getByText('mandate_budget')).toBeInTheDocument();
    expect(screen.getByText('Unit price')).toBeInTheDocument();
    expect(screen.getByText('unit_price')).toBeInTheDocument();
  });

  it('shows neutral messages for empty findings and affected fields', () => {
    render(<IntentAssessmentCard assessmentState={assessmentState({
      ...baseAssessment, finding_codes: [], affected_fields: [],
    })} />);
    expect(screen.getByText('No findings were recorded.')).toBeInTheDocument();
    expect(screen.getByText('No affected fields were recorded.')).toBeInTheDocument();
  });

  it('shows a neutral no-record state without inferring auditor execution', () => {
    render(<IntentAssessmentCard assessmentState={assessmentState(null)} />);
    expect(screen.getByText('No persisted assessment is available for this proposal.')).toBeInTheDocument();
    expect(screen.getByText('This does not indicate whether an assessment was run or bypassed.')).toBeInTheDocument();
  });

  it('shows an accessible loading state and disables refresh while loading', () => {
    render(<IntentAssessmentCard assessmentState={assessmentState(null, {
      loading: true, loaded: false, unavailable: false,
    })} />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading assessment data');
    expect(screen.getByRole('button', { name: 'Refresh Intent Auditor assessment' })).toBeDisabled();
  });

  it('renders a retrieval error and retries through the hook refresh method', () => {
    const refresh = vi.fn().mockResolvedValue({ status: 'success' });
    render(<IntentAssessmentCard assessmentState={assessmentState(null, {
      error: new Error('Assessment retrieval unavailable.'), loaded: false, refresh,
    })} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Assessment retrieval unavailable.');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('formats timestamps safely and has a fallback for invalid values', () => {
    const { rerender } = render(
      <IntentAssessmentCard assessmentState={assessmentState({ ...baseAssessment, created_at: 'bad-date' })} />,
    );
    expect(screen.getByText('Timestamp unavailable')).toBeInTheDocument();
    rerender(<IntentAssessmentCard assessmentState={assessmentState(baseAssessment)} />);
    const recordedTime = document.querySelector('.intent-assessment-timestamp time');
    expect(recordedTime).toBeInTheDocument();
    expect(recordedTime).toHaveAttribute('dateTime', baseAssessment.created_at);
    expect(recordedTime).not.toHaveTextContent('Timestamp unavailable');
  });

  it('keeps deterministic BLOCK authoritative and explains the assessment is advisory', () => {
    render(<IntentAssessmentCard
      assessmentState={assessmentState()}
      policyDecision={{ decision: 'BLOCK' }}
    />);
    expect(screen.getByRole('status')).toHaveTextContent('Deterministic policy BLOCK remains in force');
    expect(screen.getByText(/does not approve a purchase or authorize payment/i)).toBeInTheDocument();
  });

  it('provides a keyboard-operable refresh button with an accessible name', () => {
    const refresh = vi.fn().mockResolvedValue({ status: 'success' });
    render(<IntentAssessmentCard assessmentState={assessmentState(baseAssessment, { refresh })} />);
    const button = screen.getByRole('button', { name: 'Refresh Intent Auditor assessment' });
    button.focus();
    expect(button).toHaveFocus();
    fireEvent.click(button);
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
