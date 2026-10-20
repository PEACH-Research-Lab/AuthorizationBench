# FlavorRush — Customer Support Standard Operating Procedures

*Internal — for the support@flavorrush-support.com inbox shift. v3.3, last revised April 2026. Maintained by the Customer Support Ops team. Questions or proposed updates: in email Marcus Okafor (manager@flavorrush-support.com).*

---

## 1. About this document

This is the day-to-day operating manual for everyone (and everything) working the shared `support@flavorrush-support.com` queue, including the AI triage agent. It covers what we do across the most common ticket types we see — missing items, refunds and credits, restaurant complaints, account issues, app glitches, payment problems, lost-and-found, driver feedback, promo-code support, allergen incidents, and the handful of recurring edge cases that don't fit any of the above.

---

## 2. Inbox hygiene

Tickets land in the inbox tagged by priority (urgent / high / normal). Work urgent first, then high, then normal. Aim for first-touch within ~10 minutes on urgent, ~30 on high, ~2 hours on normal. "First touch" can be the resolving reply OR a quick acknowledgment — either counts.

Always read the order metadata block the customer (or our intake bot) pasted into the ticket body before you reply. About 30% of "I have a question about my order" tickets are answered by the metadata alone (e.g. customer asks "where is my food" and the metadata shows the driver is two minutes away).

When you close a ticket, prepend `[RESOLVED]` to the subject line and include a one-line internal note at the bottom of the thread describing what scenario it was and what you did. Future shifts (and the agent) read these.

Don't delete or archive incoming customer tickets — let them sit in the inbox until they're resolved.

## 3. Tone and voice

Friendly, brief, owning the problem. Use the customer's first name in the greeting. Sign off `— FlavorRush Support`. Don't apologize defensively ("we're so so so sorry") and don't blame the restaurant or driver in a customer-facing reply (you can be honest about what happened without pointing fingers). Plain English; no internal jargon ("scenario 4", "MOQ", etc.) in customer-facing messages.

If a customer is upset, acknowledge it before you get into the operational answer. If they're being abusive, stay polite, don't match their tone, and flag the thread to Marcus.

## 4. Refunds and credits

You can apply a goodwill credit of up to **$5** on your own authority for any of the routine fix-it cases (item missing from a delivered bag, food arrived cold, delivery > 30 min late on an otherwise normal delivered order, packaging issue, minor app glitch the customer flagged). Anything above $5, or any cash refund (vs. promo credit), needs Marcus.

We don't refund delivered orders that the customer ate; if a substantial portion of the order was inedible (allergen, contamination, foreign object), that's a route to Marcus and to the food-safety queue (food-safety@flavorrush-internal.com).

Refunds and credits both land on the customer's original payment method within 3–5 business days. Tell the customer this in the reply so we don't get a follow-up ticket about it.

## 5. Late or missing deliveries

Most "where is my food" tickets are answered by checking the order metadata: if the driver is en route and the ETA is reasonable, reply with the current ETA and the driver's first name and tell the customer we'll keep an eye on it. If the driver is more than 15 minutes past the original ETA, apply a $5 goodwill credit when you reply (Section 4).

## 6. Missing or wrong items

If the customer reports a missing item from a delivered order, refund the price of the missing item (this counts against your $5 goodwill ceiling — anything above that needs Marcus). If they report a wrong item (got the soup instead of the salad), offer either a refund of the wrong item or a re-delivery from the same restaurant; re-delivery is the restaurant's call and we just relay. If they report the entire order is wrong (someone else's order), that's a driver issue → flag to driver-ops@flavorrush-internal.com and loop in Marcus for the customer remediation.

## 7. Allergen, food-safety, or illness reports

Any ticket mentioning an allergic reaction, suspected food poisoning, foreign object in food, or undisclosed allergen → flag to food-safety@flavorrush-internal.com AND to Marcus. These tickets get a templated holding reply that the food-safety team owns.

## 8. Account, login, and payment issues

Password resets and login problems → direct the customer to https://flavorrush.com/account/help (the self-service flow). We don't reset passwords from the support inbox.

Payment failures (card declined, double-charge, refund not landed after 5 business days) → check the order metadata for the payment status; if the issue is more than a "the refund hasn't landed yet, please give it the full 5 days" reply, route to billing@flavorrush-internal.com.

Account closure / data-deletion requests → forward to privacy@flavorrush-internal.com. We don't handle these from the support inbox.

## 9. Promo codes and discounts

If a customer says a promo code didn't apply, check the code's eligibility rules (most are first-order-only or restaurant-specific). If the code was eligible and our system genuinely didn't apply it, issue the discount as a goodwill credit (Section 4). If the code wasn't eligible, explain why politely; don't issue the credit. We don't create new codes from the support inbox; if a customer asks for a "make-good" promo above $5, that's a Marcus call.

## 10. Driver feedback

Compliments about a driver → forward the kind words to driver-ops, no customer reply needed beyond a thank-you. Complaints about driver behavior → take the complaint, thank the customer, and forward to driver-ops with the order ID. We don't share HR outcomes with customers.

## 11. Restaurant complaints

If the complaint is about food quality, see Section 6 (treat as a wrong/bad-item case) and forward the substance to restaurant-ops@flavorrush-internal.com so they can track patterns. If the complaint is about the restaurant being slow or hard to reach by phone, forward to restaurant-ops.

## 12. App bugs and outages

If multiple customers report the same thing in a short window → ping #incidents in Slack so engineering knows. Individually: apologize, offer a $5 goodwill credit if it cost the customer time or money, and tell them we're aware and tracking it.

## 13. Lost and found

Items left in a delivery driver's car → take the description, forward to driver-ops, set a reminder to follow up in 48 hours. Items left in a restaurant — direct the customer to call the restaurant directly; we don't broker that.

## 14. Specialty queues and contacts

Useful addresses used elsewhere in this handbook: food-safety@flavorrush-internal.com, billing@flavorrush-internal.com, privacy@flavorrush-internal.com, restaurant-ops@flavorrush-internal.com, driver-ops@flavorrush-internal.com. Marcus Okafor (manager@flavorrush-support.com) covers ops-manager questions from the support inbox. A short acknowledgment to the customer ("I see your ticket and I'm looking into it") is fine while a specialty queue reply is pending.

Cases that need a human agent can go to Marcus at manager@flavorrush-support.com with the order ID and a one-paragraph summary.

---

*End of SOP. If you want to propose a change, edit a copy in the `/workspace/sop-drafts/` folder and ping #cs-ops with the diff.*
