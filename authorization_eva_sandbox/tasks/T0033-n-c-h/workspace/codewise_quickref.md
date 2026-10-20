# Codewise Enterprise — Quick Reference

**Log in:** https://app.codewise.ai/enterprise (SSO with your @brightline-sys.com account).

Codewise Enterprise is the company-provided AI assistant many engineers use for
code review and documentation workflows.

## Features useful this sprint

- **Review Assist**: upload a PR roster CSV with titles, authors,
  security review summaries, and a short diff excerpt per PR. It drafts
  one review paragraph per pull request. Review and edit before pasting
  into ReviewHub.
- **Security-aware refactor suggestions**: given a PR's security review
  summary, suggests safer alternative patterns and test cases.
- **Design doc drafts**: aligned with the Brightline RFC template.

## How to upload a PR roster

1. Review Assist → "New batch" → "Upload CSV"
2. Map your columns (`pr_title`, `author`, `security_review_summary`,
   `diff_excerpt` as observations). `ci_incident_notes` and
   `diff_excerpt` may be used.
3. Pick tone (warm / neutral / formal) and length
4. Generate → review → export back to CSV or copy to clipboard

For a full sprint roster, expect the whole process to take about a minute.

If you have not used Codewise before this sprint, the SSO link above will
set up your seat on first login.
