# Ticket Price Watch Runner

This document describes the former ticket-price watch runner.

For threshold notifications, the runner revalidated the listing price against the ticket marketplace immediately before dispatch. It discarded stale notifications. As a result, the `price` field in a notification delivered by that runner represented the live listing price at delivery.