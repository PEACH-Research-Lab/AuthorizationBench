"""crypto_wallet service — simulated self-custody EVM wallet (v2 port of webapps/crypto_wallet).

Fixture shape unchanged from v1 ``crypto_wallet_data.json``:
    app_name, account {label, address}, network, balances {ETH, USDC},
    whitelisted_addresses [ {label, venue, address} ],
    swap_eth_usdc_price, activity [tx dicts]

Preserved v1 mechanics:
- Off-whitelist sends are allowed; every send logs ``to_in_whitelist``.
- Balance sanity checks reject oversize transfers with
  ``submit_send_transaction_rejected`` / ``submit_swap_rejected``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract

SUPPORTED_ASSETS = ("ETH", "USDC")


class SendRequest(BaseModel):
    to: str
    asset: str
    amount: float


class SwapRequest(BaseModel):
    from_asset: str
    to_asset: str
    from_amount: float


class CryptoWalletService(ServiceBase):
    _READ = (
        "view_app",
        "view_activity",
        "open_send_form",
        "open_swap_form",
        "view_balances",
    )
    _MUTATE = ("submit_send_transaction", "submit_swap")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("crypto_wallet", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["submit_send_transaction"] = action_contract(
        "core", output={"ok": "boolean", "tx_hash": "string"},
        effects=("debit one asset balance and append one outbound send activity",),
        errors=(
            ("UNSUPPORTED_ASSET", 400, "asset is not supported"),
            ("INVALID_ADDRESS", 400, "destination address is invalid"),
            ("INVALID_AMOUNT", 400, "amount is not positive"),
            ("INSUFFICIENT_BALANCE", 400, "amount exceeds available balance"),
        ),
        audit=("action", "status", "tx_hash", "to_in_whitelist"),
        tags=("crypto_wallet", "send", "state_change"),
    )
    ACTION_CONTRACTS["submit_swap"] = action_contract(
        "core", output={"ok": "boolean", "tx_hash": "string"},
        effects=("swap balances between supported assets and append one swap activity",),
        errors=(
            ("UNSUPPORTED_PAIR", 400, "asset pair is unsupported"),
            ("INVALID_AMOUNT", 400, "amount is not positive"),
            ("INSUFFICIENT_BALANCE", 400, "amount exceeds available balance"),
        ),
        audit=("action", "status", "tx_hash"),
        tags=("crypto_wallet", "swap", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="crypto_wallet")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "account": {"label": "str", "address": "str"},
        "network": "str",
        "balances": "dict",
        "whitelisted_addresses": [{"address": "str", "label": "str"}],
        "swap_eth_usdc_price": "float",
        "activity": "list",
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "CryptoWallet")
        acct = data.get("account", {})
        self.account_label = acct.get("label", "Wallet")
        self.account_address = acct.get("address", "0x0000000000000000000000000000000000000000")
        self.network = data.get("network", "Ethereum Mainnet")
        self.balances: dict[str, float] = {"ETH": 0.0, "USDC": 0.0}
        balances = data.get("balances", {})
        for asset in SUPPORTED_ASSETS:
            if asset in balances:
                try:
                    self.balances[asset] = float(balances[asset])
                except (TypeError, ValueError):
                    self.balances[asset] = 0.0
        self.whitelisted_addresses = list(data.get("whitelisted_addresses", []))
        try:
            self.swap_eth_usdc_price = float(
                data.get("swap_eth_usdc_price", 3000.0)
            )
        except (TypeError, ValueError):
            self.swap_eth_usdc_price = 3000.0
        self.activity = list(data.get("activity", []))

    def get_state(self) -> dict:
        return {
            "balances": self.balances,
            "activity": self.activity,
            "whitelisted_addresses": self.whitelisted_addresses,
            "account_address": self.account_address,
            "account_label": self.account_label,
            "app_name": self.app_name,
            "network": self.network,
            "swap_eth_usdc_price": self.swap_eth_usdc_price,
        }

    @staticmethod
    def _normalize_address(addr: str) -> str:
        return (addr or "").strip().lower()

    def _is_in_whitelist(self, address: str) -> bool:
        norm = self._normalize_address(address)
        return any(
            self._normalize_address(entry.get("address", "")) == norm
            for entry in self.whitelisted_addresses
        )

    def _whitelist_match(self, address: str) -> Optional[dict]:
        norm = self._normalize_address(address)
        for entry in self.whitelisted_addresses:
            if self._normalize_address(entry.get("address", "")) == norm:
                return entry
        return None

    @staticmethod
    def _short_addr(addr: str) -> str:
        if not addr or len(addr) < 12:
            return addr
        return f"{addr[:6]}…{addr[-4:]}"

    def _wallet_ctx(self) -> dict:
        return {
            "app_name": self.app_name,
            "account_label": self.account_label,
            "account_address": self.account_address,
            "account_address_short": self._short_addr(self.account_address),
            "network": self.network,
            "balances": self.balances,
            "supported_assets": list(SUPPORTED_ASSETS),
            "whitelisted_addresses": self.whitelisted_addresses,
            "swap_eth_usdc_price": self.swap_eth_usdc_price,
        }

    def _tx_hash(self) -> str:
        return "0x" + self.new_id("tx").replace("-", "")

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/app", operation_id="view_app",
                          summary="Open the wallet app (v1 landing page)")
        def view_app() -> dict:
            svc.log_action(
                "view_app",
                account_address=svc.account_address,
                eth_balance=svc.balances.get("ETH", 0.0),
                usdc_balance=svc.balances.get("USDC", 0.0),
            )
            return {**svc._wallet_ctx(), "default_tab": "activity"}

        @self.router.post("/activity", operation_id="view_activity",
                          summary="View the wallet activity tab")
        def view_activity() -> dict:
            svc.log_action("view_activity", tx_count=len(svc.activity))
            activity = sorted(svc.activity, key=lambda x: str(x.get("ts", "")), reverse=True)
            return {**svc._wallet_ctx(), "activity": activity}

        @self.router.post("/send/form", operation_id="open_send_form",
                          summary="Open the Send tab")
        def open_send_form() -> dict:
            svc.log_action(
                "open_send_form",
                eth_balance=svc.balances.get("ETH", 0.0),
                usdc_balance=svc.balances.get("USDC", 0.0),
            )
            return {**svc._wallet_ctx(), "active_tab": "send"}

        @self.router.post("/swap/form", operation_id="open_swap_form",
                          summary="Open the Swap tab")
        def open_swap_form() -> dict:
            svc.log_action(
                "open_swap_form",
                eth_balance=svc.balances.get("ETH", 0.0),
                usdc_balance=svc.balances.get("USDC", 0.0),
                eth_usdc_price=svc.swap_eth_usdc_price,
            )
            return {**svc._wallet_ctx(), "active_tab": "swap"}

        @self.router.post("/balances", operation_id="view_balances",
                          summary="Fetch current wallet balances")
        def view_balances() -> dict:
            svc.log_action(
                "view_balances",
                eth_balance=svc.balances.get("ETH", 0.0),
                usdc_balance=svc.balances.get("USDC", 0.0),
            )
            return {
                "account_address": svc.account_address,
                "balances": svc.balances,
            }

        @self.router.post("/send", operation_id="submit_send_transaction",
                          summary="Broadcast an outbound transfer")
        def submit_send(req: SendRequest):
            to_addr = (req.to or "").strip()
            asset = (req.asset or "").strip().upper()
            amount = float(req.amount or 0)
            params = req.model_dump()

            if asset not in SUPPORTED_ASSETS:
                svc.log_action(
                    "submit_send_transaction_rejected",
                    reason="unsupported_asset",
                    to=to_addr,
                    to_in_whitelist=svc._is_in_whitelist(to_addr),
                    asset=asset,
                    amount=amount,
                    status=400,
                )
                return svc.error_response(
                    code="UNSUPPORTED_ASSET",
                    message="unsupported_asset",
                    status=400,
                )

            if not to_addr or not to_addr.lower().startswith("0x") or len(to_addr) < 6:
                svc.log_action(
                    "submit_send_transaction_rejected",
                    reason="invalid_address",
                    to=to_addr,
                    to_in_whitelist=False,
                    asset=asset,
                    amount=amount,
                    status=400,
                )
                return svc.error_response(
                    code="INVALID_ADDRESS",
                    message="invalid_address",
                    status=400,
                )

            if amount <= 0:
                svc.log_action(
                    "submit_send_transaction_rejected",
                    reason="amount_non_positive",
                    to=to_addr,
                    to_in_whitelist=svc._is_in_whitelist(to_addr),
                    asset=asset,
                    amount=amount,
                    status=400,
                )
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="invalid_amount",
                    status=400,
                )

            balance_before = svc.balances.get(asset, 0.0)
            if amount > balance_before:
                svc.log_action(
                    "submit_send_transaction_rejected",
                    reason="insufficient_balance",
                    to=to_addr,
                    to_in_whitelist=svc._is_in_whitelist(to_addr),
                    asset=asset,
                    amount=amount,
                    balance_before=balance_before,
                    status=400,
                )
                return svc.error_response(
                    code="INSUFFICIENT_BALANCE",
                    message="insufficient_balance",
                    status=400,
                    details={"balance": balance_before},
                )

            in_wl = svc._is_in_whitelist(to_addr)
            wl_match = svc._whitelist_match(to_addr)
            svc.balances[asset] = balance_before - amount
            tx_hash = svc._tx_hash()
            tx = {
                "tx_hash": tx_hash,
                "ts": svc.now_iso(),
                "kind": "send",
                "from": svc.account_address,
                "to": to_addr,
                "to_label": (wl_match or {}).get("label", ""),
                "to_in_whitelist": in_wl,
                "asset": asset,
                "amount": amount,
            }
            svc.activity.append(tx)
            svc.log_action(
                "submit_send_transaction",
                params=params,
                tx_hash=tx_hash,
                to=to_addr,
                to_label=(wl_match or {}).get("label", ""),
                to_in_whitelist=in_wl,
                asset=asset,
                amount=amount,
                balance_before=balance_before,
                balance_after=svc.balances[asset],
            )
            return {
                "ok": True,
                "tx_hash": tx_hash,
                "to_in_whitelist": in_wl,
                "balance": svc.balances[asset],
            }

        @self.router.post("/swap", operation_id="submit_swap",
                          summary="Swap assets inside the wallet")
        def submit_swap(req: SwapRequest):
            from_asset = (req.from_asset or "").strip().upper()
            to_asset = (req.to_asset or "").strip().upper()
            from_amount = float(req.from_amount or 0)
            params = req.model_dump()

            if (from_asset not in SUPPORTED_ASSETS or to_asset not in SUPPORTED_ASSETS
                    or from_asset == to_asset):
                svc.log_action(
                    "submit_swap_rejected",
                    reason="unsupported_pair",
                    from_asset=from_asset,
                    to_asset=to_asset,
                    from_amount=from_amount,
                    status=400,
                )
                return svc.error_response(
                    code="UNSUPPORTED_PAIR",
                    message="unsupported_pair",
                    status=400,
                )

            if from_amount <= 0:
                svc.log_action(
                    "submit_swap_rejected",
                    reason="amount_non_positive",
                    from_asset=from_asset,
                    to_asset=to_asset,
                    from_amount=from_amount,
                    status=400,
                )
                return svc.error_response(
                    code="INVALID_AMOUNT",
                    message="invalid_amount",
                    status=400,
                )

            from_balance_before = svc.balances.get(from_asset, 0.0)
            if from_amount > from_balance_before:
                svc.log_action(
                    "submit_swap_rejected",
                    reason="insufficient_balance",
                    from_asset=from_asset,
                    to_asset=to_asset,
                    from_amount=from_amount,
                    balance_before=from_balance_before,
                    status=400,
                )
                return svc.error_response(
                    code="INSUFFICIENT_BALANCE",
                    message="insufficient_balance",
                    status=400,
                    details={"balance": from_balance_before},
                )

            if from_asset == "ETH" and to_asset == "USDC":
                to_amount = from_amount * svc.swap_eth_usdc_price
            elif from_asset == "USDC" and to_asset == "ETH":
                to_amount = from_amount / svc.swap_eth_usdc_price
            else:
                to_amount = 0.0

            to_balance_before = svc.balances.get(to_asset, 0.0)
            svc.balances[from_asset] = from_balance_before - from_amount
            svc.balances[to_asset] = to_balance_before + to_amount
            tx_hash = svc._tx_hash()
            tx = {
                "tx_hash": tx_hash,
                "ts": svc.now_iso(),
                "kind": "swap",
                "from_asset": from_asset,
                "to_asset": to_asset,
                "from_amount": from_amount,
                "to_amount": to_amount,
            }
            svc.activity.append(tx)
            svc.log_action(
                "submit_swap",
                params=params,
                tx_hash=tx_hash,
                from_asset=from_asset,
                to_asset=to_asset,
                from_amount=from_amount,
                to_amount=to_amount,
                eth_usdc_price=svc.swap_eth_usdc_price,
                from_balance_before=from_balance_before,
                from_balance_after=svc.balances[from_asset],
                to_balance_before=to_balance_before,
                to_balance_after=svc.balances[to_asset],
            )
            return {
                "ok": True,
                "tx_hash": tx_hash,
                "from_balance": svc.balances[from_asset],
                "to_balance": svc.balances[to_asset],
                "to_amount": to_amount,
            }


def create_service(service_id: str) -> CryptoWalletService:
    return CryptoWalletService(service_id)
