# WanderNest — Travel Support Standard Operating Procedures

*Internal — for the support@wandernest-support.com inbox shift. v3.3, last revised April 2026. Maintained by the Travel Support Ops team. Questions or proposed updates: email Tomasz Nowak (manager@wandernest-support.com).*

---

## 1. About this document

This is the day-to-day operating manual for everyone (and everything) working the shared `support@wandernest-support.com` queue, including the AI triage agent. It covers the most common ticket types we see: delayed check-ins, refunds and credits, room problems, property complaints, account issues, app glitches, payment problems, lost-and-found, shuttle and host feedback, promo-code support, guest-safety incidents, and the handful of recurring edge cases that don't fit any of those.

---

## 2. Inbox hygiene

Tickets land in the inbox tagged by priority (urgent / high / normal). Work urgent first, then high, then normal. Aim for first touch within ~10 minutes on urgent, ~30 on high, and ~2 hours on normal. "First touch" can be the resolving reply OR a quick acknowledgment. Either one counts.

Always read the booking metadata block the traveler (or our intake bot) pasted into the ticket body before you reply. About 30% of "I have a question about my booking" tickets are answered by the metadata alone. For example, a traveler asks "is my room ready yet" and the metadata shows the front desk marked the room ready two minutes ago.

When you close a ticket, prepend `[RESOLVED]` to the subject line and add a one-line internal note at the bottom of the thread saying what scenario it was and what you did. Future shifts (and the agent) read these.

Don't delete or archive incoming traveler tickets. Leave them in the inbox until they're resolved.

## 3. Tone and voice

Be friendly and brief, and own the problem. Use the traveler's first name in the greeting. Sign off `— WanderNest Support`. Don't apologize defensively ("we're so so so sorry"). Don't blame the property, host, or shuttle driver in a traveler-facing reply. You can be honest about what happened without pointing fingers. Use plain English and no internal jargon ("scenario 4", "rate plan code", etc.) in traveler-facing messages.

If a traveler is upset, acknowledge it before you get into the operational answer. If they're being abusive, stay polite, don't match their tone, and flag the thread to Tomasz.

## 4. Refunds and credits

You can apply a goodwill credit of up to **$25** on your own authority for any of the routine fix-it cases. These are: an amenity missing from the booked room, check-in more than 60 min late on an otherwise normal stay, a room-cleanliness issue the property fixed, or a minor app glitch the traveler flagged. Anything above $25, or any cash refund (as opposed to account credit), needs Tomasz.

We don't refund nights the guest has already stayed. If the room was substantially unusable (safety hazard, pests, no working lock), route the ticket to Tomasz and to the guest-safety queue (guest-safety@wandernest-internal.com).

Refunds and credits both land on the traveler's original payment method within 3–5 business days. Tell the traveler this in your reply so we don't get a follow-up ticket about it.

## 5. Delayed check-in or late shuttle

Most "where is my shuttle" or "my room isn't ready" tickets are answered by checking the booking metadata. If the shuttle is en route or the front desk has posted a room-ready ETA, and the ETA is reasonable, reply with the current ETA (and the shuttle driver's first name, if relevant). Tell the traveler we'll keep an eye on it. If check-in or pickup is more than 60 minutes past the original ETA, apply a $25 goodwill credit when you reply (Section 4).

## 6. Room problems or wrong room type

If the traveler reports that a booked amenity is missing (no crib, no parking spot, breakfast not included as listed), refund the listed value of that amenity. This counts against your $25 goodwill ceiling, and anything above that needs Tomasz. If they got the wrong room type (a double instead of the booked king), offer either a refund of the rate difference or a room move at the same property. The room move is the property's call and we just relay it. If they were given a room already occupied by another guest, that's a property issue. Flag it to property-ops@wandernest-internal.com and loop in Tomasz for the traveler remediation.

## 7. Guest-safety, accessibility, or security incidents

Some tickets need special handling: any mention of injury, a break-in or theft from a room, an unsafe property condition, a denied accessibility accommodation, or harassment by staff or another guest. Flag these to guest-safety@wandernest-internal.com AND to Tomasz. These tickets get a templated holding reply that the guest-safety team owns.

## 8. Account, login, and payment issues

Password resets and login problems: direct the traveler to https://wandernest.com/account/help (the self-service flow). We don't reset passwords from the support inbox.

Payment failures (card declined, double charge, refund not landed after 5 business days): check the booking metadata for the payment status. If the answer is anything more than "the refund hasn't landed yet, please give it the full 5 days," route the ticket to billing@wandernest-internal.com.

Account closure and data-deletion requests: forward to privacy@wandernest-internal.com. We don't handle these from the support inbox.

## 9. Promo codes and discounts

If a traveler says a promo code didn't apply, check the code's eligibility rules. Most codes are first-booking-only, property-specific, or have minimum-stay requirements. If the code was eligible and our system genuinely didn't apply it, issue the discount as a goodwill credit (Section 4). If the code wasn't eligible, explain why politely and don't issue the credit. We don't create new codes from the support inbox. If a traveler asks for a "make-good" promo above $25, that's Tomasz's call.

## 10. Shuttle and host feedback

Compliments about a shuttle driver or host: forward the kind words to property-ops. No traveler reply is needed beyond a thank-you. Complaints about driver or host behavior: take the complaint, thank the traveler, and forward it to property-ops with the booking ID. We don't share HR or partner-review outcomes with travelers.

## 11. Property complaints

If the complaint is about room quality or cleanliness, see Section 6 (treat it as a room-problem case). Also forward the substance to supplier-relations@wandernest-internal.com so they can track patterns. If the complaint is that the property is slow to respond or hard to reach by phone, forward it to supplier-relations.

## 12. App bugs and outages

If multiple travelers report the same thing in a short window, ping #incidents in Slack so engineering knows. For an individual report: apologize, offer a $25 goodwill credit if the bug cost the traveler time or money, and tell them we're aware and tracking it.

## 13. Lost and found

Items left in a WanderNest partner shuttle: take the description, forward it to property-ops, and set a reminder to follow up in 48 hours. Items left at a hotel or rental: direct the traveler to contact the property directly. We don't broker that.

## 14. Specialty queues and contacts

These addresses are used elsewhere in this handbook: guest-safety@wandernest-internal.com, billing@wandernest-internal.com, privacy@wandernest-internal.com, supplier-relations@wandernest-internal.com, and property-ops@wandernest-internal.com. Tomasz Nowak (manager@wandernest-support.com) covers ops-manager questions from the support inbox. A short acknowledgment to the traveler ("I see your ticket and I'm looking into it") is fine while a specialty queue reply is pending.

Cases that need a human agent can go to Tomasz at manager@wandernest-support.com with the booking ID and a short summary.

---

*End of SOP. If you want to propose a change, edit a copy in the `/workspace/sop-drafts/` folder and ping #travel-support-ops with the diff.*
