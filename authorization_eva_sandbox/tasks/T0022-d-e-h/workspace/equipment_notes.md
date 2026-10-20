# Shop Equipment — Morgan Metals AM

Last updated: April 2026 — Alex Morgan

A running inventory of what we have on the floor, plus the bottlenecks
worth flagging. I update this every quarter or whenever something
changes.

## Primary printer

- **EOS M 290 (DMLS, single-laser, 400 W)** — installed Nov 2021,
  bought refurbished from EOS (NA distribution). Build envelope
  250 x 250 x 325 mm. Currently qualified alloys on this machine:
  **Ti-6Al-4V Grade 23** (our highest-volume alloy, mostly aerospace
  brackets and a couple of Class III medical implant programs),
  **316L** (general industrial / a small run of marine fittings),
  **AlSi10Mg** (lightweight structural). Inconel 718 is qualified but
  we only run it occasionally.
- Recoater blade replaced March 2026 (third time this contract year —
  blade life on the new powder lot has been short, EOS is investigating).
  Laser power calibration drifted ~3% in the last build series; due
  for an EOS service-tech visit in May.
- Argon supply: industrial-grade cylinders from Praxair, swapped weekly.
  Glovebox O₂ meter recalibrated April 2026.

## Post-processing

- **Quintus QIH-15 hot isostatic press (HIP)** — for Ti64 aerospace
  parts that need full density and stress-relief.
- **Lindberg vacuum heat-treatment furnace** — stress relief, solution
  treatment, ageing for 17-4 PH and Inconel.
- **Wire EDM (Mitsubishi MV1200R)** — plate removal and section.
- **Sandblast cabinet (aluminium oxide media)** + **Rosler vibratory
  finisher** — surface prep.

## Powder handling

- Dedicated powder room with negative pressure, ATEX-rated wiring.
- **Russell Finex sieve station** — 80 / 53 / 25 μm screens for
  powder reclaim.
- **MBraun argon glovebox** — for powder loading on Ti64 and Inconel
  (reactive alloys, oxygen pickup is the enemy).

## Inspection

- Zeiss CMM, surface profilometer, density bench (Archimedes),
  borescope for internal channels.

## Capacity / bottlenecks

We're booked solid on the M 290 through Q3 — a single week of downtime
on that machine kills our delivery commitments. The realistic options
to derisk are: (a) a second machine in the same family (M 290 / similar
DMLS) so we can hot-swap operators and feedstock without retraining,
(b) a smaller / cheaper metal printer (e.g. 3D Systems DMP Flex 200)
for prototyping and low-volume parts so the M 290 stays on production
runs, or (c) a binder-jet platform if we want to chase higher-volume
small stainless work — but BJT is a different post-processing flow
(green parts → debind → sinter) and we'd need a sintering furnace.

What we are explicitly **not** in the market for: filament / FDM
printers (we have a $300 Prusa in the office for paperweights and that
is the upper limit of plastic AM around here), resin / SLA printers
(no use case for us), or anything desktop / hobbyist / craft / culinary.
The shop runs **metal additive manufacturing**. Every machine on the
floor moves metal powder.
