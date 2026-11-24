# PartnerConsole v1.0.8

The partner dashboard, subscription list, purchase form, subscription details,
and transaction history now share the updated responsive layout. The purchase
quote identifies the destination account and product, itemizes volume and day
credits, and displays the server-calculated wallet charge for each component.

A change in pricing or the payment mix requires a fresh quote. If the new
quote exceeds the wallet balance, the form shows the shortfall. A new
affordable quote requires a fresh confirmation. Pending purchases can still be
resumed without taking payment twice.

For new purchases, the operation records the exact ledger row IDs when taking
payment. Completion links those rows to the subscription, including on recovery
after a lost panel response. The partner history links to a subscription only
when the corresponding record belongs to that partner. Older unlinked ledger
entries are left as recorded. This release adds no database migration or
configuration requirement.

For existing installations, follow the [Update instructions](../../../../README.md#upgrade-an-existing-installation).
