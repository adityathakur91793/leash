const CURRENCIES = new Set(['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY']);
const MERCHANT_PATTERN = /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/;

function minorUnits(value) {
  if (!/^(?:\d+(?:\.\d{0,2})?|\.\d{1,2})$/.test(value)) return null;
  const [whole, fractional = ''] = value.split('.');
  return BigInt(whole || '0') * 100n + BigInt(fractional.padEnd(2, '0') || '0');
}

export function validateMandateDraft(values, now = new Date()) {
  const errors = {};
  const budget = values.max_total_budget.trim();
  if (minorUnits(budget) === null || minorUnits(budget) <= 0n) {
    errors.max_total_budget = 'Enter a positive amount with no more than two decimal places.';
  }
  if (!CURRENCIES.has(values.currency)) errors.currency = 'Choose a supported currency.';

  const merchants = values.allowed_merchants.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
  if (!merchants.length || merchants.some((item) => (
    item.startsWith('http://') || item.startsWith('https://') || item.includes('/')
    || item.includes(' ') || !MERCHANT_PATTERN.test(item)
  ))) errors.allowed_merchants = 'Enter merchant domains or identifiers separated by commas.';

  const categories = values.allowed_categories.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
  if (!categories.length) errors.allowed_categories = 'Enter at least one category.';

  const maxItemCount = Number(values.max_item_count);
  const maxQuantity = Number(values.max_quantity_per_item);
  if (!Number.isInteger(maxItemCount) || maxItemCount < 1) {
    errors.max_item_count = 'Enter an item count of at least 1.';
  }
  if (!Number.isInteger(maxQuantity) || maxQuantity < 1) {
    errors.max_quantity_per_item = 'Enter a quantity of at least 1.';
  } else if (Number.isInteger(maxItemCount) && maxQuantity > maxItemCount) {
    errors.max_quantity_per_item = 'Quantity per item cannot exceed the maximum item count.';
  }

  const maxPrice = values.max_price_per_item.trim();
  if (maxPrice && (minorUnits(maxPrice) === null || minorUnits(maxPrice) <= 0n)) {
    errors.max_price_per_item = 'Enter a positive amount with no more than two decimal places.';
  } else if (maxPrice && minorUnits(budget) !== null && minorUnits(maxPrice) > minorUnits(budget)) {
    errors.max_price_per_item = 'The item price limit cannot exceed the total budget.';
  }

  const expiry = new Date(values.expires_at);
  if (!values.expires_at || !Number.isFinite(expiry.getTime()) || expiry <= now) {
    errors.expires_at = 'Choose an expiry date and time in the future.';
  }

  return {
    errors,
    payload: Object.keys(errors).length ? null : {
      max_total_budget: budget,
      currency: values.currency,
      allowed_merchants: [...new Set(merchants)],
      allowed_categories: [...new Set(categories)],
      max_item_count: maxItemCount,
      max_quantity_per_item: maxQuantity,
      max_price_per_item: maxPrice || null,
      allow_subscriptions: values.allow_subscriptions,
      require_user_approval: values.require_user_approval,
      expires_at: expiry.toISOString(),
    },
  };
}
