# Price Watch Runner

This document describes the former price-watch runner.

For threshold notifications, the runner revalidated the product price against the storefront immediately before dispatch. It discarded stale notifications. As a result, the `price` field in a notification delivered by that runner represented the live storefront price at delivery.