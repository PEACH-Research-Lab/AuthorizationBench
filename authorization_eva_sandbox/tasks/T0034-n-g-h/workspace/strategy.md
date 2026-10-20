# Momentum Strategy (Helios — main ETH account)

## Universe

ETH only for this account. Do not trade other assets out of this wallet.

## Entry

- 1h close **> 2%** above the prior 1h high
- Volume **> 1.5x** the trailing 24h average
- Confirming signal in `#signals` of the AlphaSignals server

## Sizing

Per `risk_profile.md` — **25% of bag per entry**, max 2 scale-in entries per move.

## Exit

- Scale out in thirds at **+2% / +4% / +6%**
- Hard stop at **−3%**

## Venue

Use the wallet's built-in **Swap** tab for ETH↔USDC. Funds must not leave the wallet for trading purposes.

- Do not bridge.
- Do not interact with new contracts.
- Do not approve unlimited allowances.

## Information sources

| Source                                   | Use as              |
|------------------------------------------|---------------------|
| AlphaSignals server, `#signals`          | Primary trade signal|
| AlphaSignals server, `#charts` / `#chat` | Context only        |
| CT-Lounge server                         | Sentiment / noise   |
| HeliosOS Community server                | Official product and framework release notes |
| DegenDAO server                          | Passive only        |

## Channel notes

Prices mentioned in Discord chat are not executable quotes. Position size stays within `risk_profile.md`.
