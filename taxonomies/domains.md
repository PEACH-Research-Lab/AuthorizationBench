# Domain taxonomy

The domain names the kind of information, asset, or record that the authorization boundary protects. The same abstract authorization failure can be told in different domains.

Assign exactly one domain to each case. The domain names the information, asset, or record protected by the authorization boundary. The same authorization failure can be instantiated in more than one domain.

## Labeling

- Assign exactly one domain per case. The domain letter is the third letter of the task id.
- Label by the object the violated action touches: the information, asset, or record at the authorization boundary.
- If that object is ordinary work material with no specific sector (a general inbox, a calendar, meeting notes, internal documents), label b office_work.
- If that object is the principal's own private-life material and no more specific domain applies, label a personal_life.
- The employer's or platform's industry does not decide the domain. A planning meeting at a hospital is office_work; a spreadsheet of patient records is healthcare_medical.
- What the agent is doing (tidying a table, triaging mail, answering customers, writing a report) is not a domain label. It is the workflow, and it stays fixed when a case is moved to another domain.

## a. personal_life

Everyday personal tasks involving relationships, communication, household logistics, private memories, errands, personal files, or life administration.

**Boundary:** Fallback for the principal's own private life. Use a more specific domain when the object belongs to one: a personal purchase is e, a personal bill or transfer is g, the principal's own health is h.

## b. office_work

Workplace operations involving documents, meetings, email, internal tools, collaboration platforms, HR or team processes, and organizational permissions.

**Boundary:** General work material without a specific sector: inboxes, calendars, meetings, internal reports, recruiting and HR process, and client deliverables of a consulting or marketing firm.

## c. software_development

Coding, repository maintenance, deployment, debugging, automation, terminal use, dependency management, and software project cleanup.

**Boundary:** Code, repositories, builds, infrastructure state, packages, and developer tools. If the violation damages the principal's personal files rather than project material, label a.

## d. travel

Trip planning, bookings, itinerary changes, transportation, lodging, travel documents, expense constraints, and passenger preferences.

**Boundary:** Bookings, itineraries, passenger details, and travel documents.

## e. e-commerce

Online shopping, grocery delivery, subscriptions, marketplaces, carts, checkout flows, returns, promotions, and saved payment or delivery information.

**Boundary:** Buying and selling goods and subscriptions, including payment made inside a checkout and customer support for a store, delivery, or subscription platform.

## f. entertainment

Media, events, games, tickets, reservations, streaming services, social outings, recommendations, and leisure planning.

**Boundary:** Media libraries, tickets, events, games, and leisure accounts.

## g. finance

Banking, payments, bills, budgeting, accounting, audits, reimbursements, transaction records, tax-adjacent records, insurance claims, and financial approvals.

**Boundary:** Money movement outside a checkout (transfers, bill payment, trading, crypto), financial records and statements, and insurance claims and payouts.

## h. healthcare_medical

Medical appointments, clinical care, health records, prescriptions, patient communication, care coordination, and sensitive health information.

**Boundary:** Clinical and patient information. Insurance claim handling is g. Non-clinical staff work at a clinic is b.

## i. legal

Contracts, compliance, forms, filings, case documents, policy interpretation, legal communications, and regulated procedural steps.

**Boundary:** Contracts, filings, case documents, and regulated legal procedure. Ordinary workplace policy and HR process is b.

## j. education

School, university, tutoring, grading, recommendations, learning management systems, student records, assignments, academic administration, and academic research data such as interview or field notes.

**Boundary:** Student records, academic work, and academic research data. Commercial market research is b.
