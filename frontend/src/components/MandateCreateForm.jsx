import { useState } from 'react';
import { ArrowLeft, ArrowRight, Check, Loader, ShieldCheck } from 'lucide-react';
import { validateMandateDraft } from '../services/mandateValidation';

const INITIAL_VALUES = {
  max_total_budget: '', currency: 'USD', allowed_merchants: '', allowed_categories: '',
  max_item_count: '1', max_quantity_per_item: '1', max_price_per_item: '',
  allow_subscriptions: false, require_user_approval: true, expires_at: '',
};

const CURRENCY_OPTIONS = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY'];

function FieldError({ id, message }) {
  return message ? <p className="field-error" id={`${id}-error`} role="alert">{message}</p> : null;
}

export function MandateCreateForm({ onCreate, onCancel, disabled, onCreated }) {
  const [values, setValues] = useState(INITIAL_VALUES);
  const [errors, setErrors] = useState({});
  const [review, setReview] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  function update(field, value) {
    setValues((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
  }

  function openReview(event) {
    event.preventDefault();
    const validation = validateMandateDraft(values);
    setErrors(validation.errors);
    if (validation.payload) setReview(true);
  }

  async function submit() {
    const { errors: validationErrors, payload } = validateMandateDraft(values);
    setErrors(validationErrors);
    if (!payload || submitting || disabled) return;
    setSubmitting(true);
    try {
      const created = await onCreate(payload);
      if (created?.mandate_id) {
        setValues(INITIAL_VALUES);
        setReview(false);
        onCreated?.(created.mandate_id);
      }
    } catch {
      // The hook owns safe error and UNKNOWN presentation; never retry on its behalf.
    } finally {
      setSubmitting(false);
    }
  }

  if (review) {
    const { payload } = validateMandateDraft(values);
    return (
      <section className="mandate-card creation-review" aria-labelledby="review-title">
        <div className="section-eyebrow"><ShieldCheck size={16} /> Permission review</div>
        <h2 id="review-title">Review before creating</h2>
        <p className="muted-copy">These permissions become immutable after creation. Confirm that each limit is intentional.</p>
        <dl className="mandate-facts review-facts">
          <div><dt>Total spending limit</dt><dd>{payload.currency} {payload.max_total_budget}</dd></div>
          <div><dt>Allowed merchants</dt><dd>{payload.allowed_merchants.join(', ')}</dd></div>
          <div><dt>Allowed categories</dt><dd>{payload.allowed_categories.join(', ')}</dd></div>
          <div><dt>Maximum items</dt><dd>{payload.max_item_count}</dd></div>
          <div><dt>Maximum quantity per item</dt><dd>{payload.max_quantity_per_item}</dd></div>
          <div><dt>Maximum item price</dt><dd>{payload.max_price_per_item ? `${payload.currency} ${payload.max_price_per_item}` : 'No separate item price limit'}</dd></div>
          <div><dt>Subscriptions</dt><dd>{payload.allow_subscriptions ? 'Allowed' : 'Not allowed'}</dd></div>
          <div><dt>Purchase approval</dt><dd>{payload.require_user_approval ? 'Required for each purchase' : 'Not required by this mandate'}</dd></div>
          <div><dt>Expires</dt><dd>{new Date(payload.expires_at).toLocaleString()}</dd></div>
        </dl>
        {disabled && <p className="inline-warning" role="alert">Mandate actions are blocked while a previous create has an unknown outcome.</p>}
        <div className="form-actions">
          <button type="button" className="button-secondary" onClick={() => setReview(false)} disabled={submitting}>
            <ArrowLeft size={16} /> Edit permissions
          </button>
          <button type="button" className="button-primary" onClick={submit} disabled={submitting || disabled}>
            {submitting ? <Loader className="spinning" size={16} /> : <Check size={16} />}
            {submitting ? 'Creating mandate…' : 'Create mandate'}
          </button>
        </div>
      </section>
    );
  }

  return (
    <form className="mandate-card mandate-form" onSubmit={openReview} noValidate aria-labelledby="create-title">
      <div className="section-eyebrow"><ShieldCheck size={16} /> Spending permissions</div>
      <h2 id="create-title">Create a mandate</h2>
      <p className="muted-copy">Set precise limits for what an agent may propose under your authority.</p>

      <div className="form-grid">
        <div className="form-field">
          <label htmlFor="mandate-budget">Total budget</label>
          <div className="input-pair">
            <select aria-label="Currency" value={values.currency} onChange={(event) => update('currency', event.target.value)}>
              {CURRENCY_OPTIONS.map((currency) => <option key={currency}>{currency}</option>)}
            </select>
            <input id="mandate-budget" inputMode="decimal" value={values.max_total_budget} onChange={(event) => update('max_total_budget', event.target.value)} aria-invalid={Boolean(errors.max_total_budget)} aria-describedby={errors.max_total_budget ? 'mandate-budget-error' : undefined} placeholder="250.00" />
          </div>
          <FieldError id="mandate-budget" message={errors.max_total_budget} />
        </div>

        <div className="form-field">
          <label htmlFor="mandate-expiry">Expiry date and time</label>
          <input id="mandate-expiry" type="datetime-local" value={values.expires_at} onChange={(event) => update('expires_at', event.target.value)} aria-invalid={Boolean(errors.expires_at)} aria-describedby={errors.expires_at ? 'mandate-expiry-error' : undefined} />
          <FieldError id="mandate-expiry" message={errors.expires_at} />
        </div>

        <div className="form-field form-field-wide">
          <label htmlFor="mandate-merchants">Allowed merchants</label>
          <input id="mandate-merchants" value={values.allowed_merchants} onChange={(event) => update('allowed_merchants', event.target.value)} placeholder="shop.example, books.example" aria-invalid={Boolean(errors.allowed_merchants)} aria-describedby={errors.allowed_merchants ? 'mandate-merchants-error' : 'mandate-merchants-help'} />
          <small id="mandate-merchants-help">Enter merchant domains or identifiers, separated by commas. No URLs or paths.</small>
          <FieldError id="mandate-merchants" message={errors.allowed_merchants} />
        </div>

        <div className="form-field form-field-wide">
          <label htmlFor="mandate-categories">Allowed categories</label>
          <input id="mandate-categories" value={values.allowed_categories} onChange={(event) => update('allowed_categories', event.target.value)} placeholder="books, home supplies" aria-invalid={Boolean(errors.allowed_categories)} aria-describedby={errors.allowed_categories ? 'mandate-categories-error' : undefined} />
          <FieldError id="mandate-categories" message={errors.allowed_categories} />
        </div>

        <div className="form-field">
          <label htmlFor="mandate-item-count">Maximum item count</label>
          <input id="mandate-item-count" type="number" min="1" step="1" value={values.max_item_count} onChange={(event) => update('max_item_count', event.target.value)} aria-invalid={Boolean(errors.max_item_count)} aria-describedby={errors.max_item_count ? 'mandate-item-count-error' : undefined} />
          <FieldError id="mandate-item-count" message={errors.max_item_count} />
        </div>
        <div className="form-field">
          <label htmlFor="mandate-quantity">Maximum quantity per item</label>
          <input id="mandate-quantity" type="number" min="1" step="1" value={values.max_quantity_per_item} onChange={(event) => update('max_quantity_per_item', event.target.value)} aria-invalid={Boolean(errors.max_quantity_per_item)} aria-describedby={errors.max_quantity_per_item ? 'mandate-quantity-error' : undefined} />
          <FieldError id="mandate-quantity" message={errors.max_quantity_per_item} />
        </div>
        <div className="form-field">
          <label htmlFor="mandate-item-price">Maximum item price <span className="optional-label">Optional</span></label>
          <input id="mandate-item-price" inputMode="decimal" value={values.max_price_per_item} onChange={(event) => update('max_price_per_item', event.target.value)} placeholder="No separate limit" aria-invalid={Boolean(errors.max_price_per_item)} aria-describedby={errors.max_price_per_item ? 'mandate-item-price-error' : undefined} />
          <FieldError id="mandate-item-price" message={errors.max_price_per_item} />
        </div>
      </div>

      <div className="permission-toggles">
        <label className="toggle-row"><input type="checkbox" checked={values.allow_subscriptions} onChange={(event) => update('allow_subscriptions', event.target.checked)} /> <span><strong>Allow subscriptions</strong><small>Permit recurring purchase proposals.</small></span></label>
        <label className="toggle-row"><input type="checkbox" checked={values.require_user_approval} onChange={(event) => update('require_user_approval', event.target.checked)} /> <span><strong>Require your approval</strong><small>Require explicit approval for each purchase.</small></span></label>
      </div>

      {disabled && <p className="inline-warning" role="alert">A previous create has an unknown outcome. Reconcile it before creating another mandate.</p>}
      <div className="form-actions">
        {onCancel && <button type="button" className="button-secondary" onClick={onCancel}>Cancel</button>}
        <button type="submit" className="button-primary" disabled={disabled}>
          Review permissions <ArrowRight size={16} />
        </button>
      </div>
    </form>
  );
}
