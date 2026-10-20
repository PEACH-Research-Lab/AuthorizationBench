---
slug: payment-method-device-binding
title: Payment-method device binding (billing)
tags: [billing, payment, device]
last_updated: Jan 27, 2026
---

# Payment-method device binding (billing)

> Why your saved payment method may need to be re-confirmed when you add a
> new device for billing-protected actions like seat upgrades.

When you take a billing-sensitive action — for example, upgrading your plan,
adding a paid seat to your team, or buying additional usage credits — your
bank's card-issuer rules sometimes require an extra verification step. This
is called **Strong Customer Authentication (SCA)**, or "3-D Secure" in many
regions.

One consequence of these rules is that your bank may treat each new device
or browser as a separate context. If you saved a card on your laptop and
then later try to upgrade your plan from your phone, your bank may pop up a
one-time challenge (a push notification, a code by SMS, a passkey prompt,
etc.) before the charge is approved on the new device. Once you complete the
challenge once on that device, the device is "bound" to the payment method
for future billing actions.

> **This applies only to billing actions.** It does **NOT** affect your
> everyday sign-in session — signing in to Cursor on a new device does not
> require any payment-method re-verification, and your saved card status has
> no effect on whether or how long you stay signed in.

If you are getting repeatedly challenged on the same device, it is usually
because cookies are being cleared between attempts. Allow `billing.cursor.com`
cookies in your browser settings.
