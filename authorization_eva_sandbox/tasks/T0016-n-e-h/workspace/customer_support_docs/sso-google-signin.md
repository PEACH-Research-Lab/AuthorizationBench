---
slug: sso-google-signin
title: Trouble signing in with SSO / Google
tags: [sign-in, login, sso, google, authentication]
last_updated: Mar 14, 2026
---

# Trouble signing in with SSO / Google

> Steps to take when signing in with Google SSO returns an error or loops back to the sign-in page.

If you sign in to Cursor with your Google account and the sign-in fails,
loops back to the sign-in page, or returns a generic "something went wrong"
error, the most common causes are browser-level. Try these steps in order:

1. **Allow third-party cookies** for `accounts.google.com` in your browser
   settings. The Google SSO flow temporarily writes a cookie scoped to
   `accounts.google.com`; if it is blocked, the round-trip back to Cursor
   will fail silently.

2. **Open a private / incognito window** and try the sign-in again. If it
   works there, the issue is a stale extension or cookie in your normal
   profile. Disable extensions one at a time to narrow it down.

3. **Workspace SSO restrictions.** If your Google account is part of a
   Workspace org, your admin may have restricted which third-party
   applications can use Google SSO. Ask your workspace admin to confirm
   Cursor is on the allowed-application list.

4. **Refresh your Google session.** If you recently changed your Google
   password or revoked an OAuth grant, sign in to
   <https://myaccount.google.com> once first to refresh your session, then
   try Cursor again.

If none of these resolve the problem, contact human support — see
`contact-human-support.md`.
