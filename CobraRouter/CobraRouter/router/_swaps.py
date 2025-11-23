try: from _main import Router
except: from ._main import Router
try: from libutils import SUPPORTED_DEXES, ADDR_TO_DEX
except: from .libutils import SUPPORTED_DEXES, ADDR_TO_DEX
import traceback
from solders.keypair import Keypair # type: ignore
from solders.pubkey import Pubkey # type: ignore
from solana.rpc.async_api import AsyncClient
import asyncio, logging
import aiohttp
from solders.message import VersionedMessage, MessageV0 # type: ignore
from solana.rpc.commitment import Processed
import statistics as _st
from solders.transaction import VersionedTransaction # type: ignore
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price # type: ignore
from solana.rpc.commitment import Confirmed
from solders.system_program import TransferParams, transfer
from spl.token.instructions import transfer_checked, TransferCheckedParams, get_associated_token_address, create_associated_token_account

def compute_unit_price_from_total_fee(
    total_lams: int,
    compute_units: int = 120_000
) -> int:
    lamports_per_cu = total_lams / float(compute_units)
    micro_lamports_per_cu = lamports_per_cu * 1_000_000
    return int(micro_lamports_per_cu)

logging.basicConfig(level=logging.INFO)

LAMPORTS_PER_SOL = 1_000_000_000
_MICRO = 1_000_000
_DEFAULT_CU = 300_000
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

class CobraSwaps:
    def __init__(self, router: Router, ctx: AsyncClient, session: aiohttp.ClientSession, rpc_url: str):
        self.ctx = ctx
        self.router = router
        self.session = session
        self.rpc_url = rpc_url

    async def _mint_owner(self, mint: Pubkey) -> Pubkey:
        """
        Get the token program id of a mint.
        """
        try:
            info = await self.ctx.get_account_info(mint, commitment=Confirmed)
            if info.value is None:
                raise RuntimeError("mint account missing")
            return info.value.owner
        except Exception as e:
            traceback.print_exc()
            logging.info(f"Failed to get token program id: {e}")
            return TOKEN_PROGRAM_ID

    async def get_balance(self, mint: str | Pubkey, pubkey: str | Pubkey):
        """
        Get the balance of a mint. Pass in the mint address and the pubkey of the account you want to check the balance of.
        Args:
            mint: str | Pubkey
            pubkey: str | Pubkey
        Returns:
            tuple: (token_balance, token_balance_raw, "success" | "account_not_found" | "account_empty")
        """
        pubkey = Pubkey.from_string(pubkey) if isinstance(pubkey, str) else pubkey
        token_pk = Pubkey.from_string(mint) if isinstance(mint, str) else mint

        if str(token_pk) == "So11111111111111111111111111111111111111112":
            bal_resp = await self.ctx.get_balance(pubkey, Confirmed)
        else:
            bal_resp = await self.ctx.get_token_accounts_by_owner_json_parsed(
                pubkey, TokenAccountOpts(mint=token_pk), Processed
            )

        if not bal_resp.value:
            return (0, 0, "account_not_found")

        token_balance = float(bal_resp.value[0].account.data.parsed["info"]["tokenAmount"]["uiAmount"] or 0) if str(token_pk) != "So11111111111111111111111111111111111111112" else bal_resp.value
        if token_balance <= 0:
            return (0, 0, "account_empty")
        token_balance_raw = int(bal_resp.value[0].account.data.parsed["info"]["tokenAmount"]["amount"] or 0) if str(token_pk) != "So11111111111111111111111111111111111111112" else bal_resp.value
        if token_balance_raw <= 0:
            return (0, 0, "account_empty")
        if str(token_pk) != "So11111111111111111111111111111111111111112":
            token_balance = token_balance_raw / 1e9

        return (token_balance, token_balance_raw, "success")

    async def get_multiple_balances(self, mints: list[str | Pubkey], pubkey: str | Pubkey):
        """
        Get the balance of multiple mints.
        """
        pubkey = Pubkey.from_string(pubkey) if isinstance(pubkey, str) else pubkey
        mints = [Pubkey.from_string(m) if isinstance(m, str) else m for m in mints]
        atas = [get_associated_token_address(pubkey, m, token_program_id=await self._mint_owner(m)) for m in mints]
        balances = {}
        try:
            infos = await self.ctx.get_multiple_accounts_json_parsed(
                atas, commitment=Processed
            )
            values = infos.value
            for value in values:
                if not value or not value.data:
                    continue
                mint = value.data.parsed["info"].get("mint", "")
                amount = float(value.data.parsed["info"].get("tokenAmount", {}).get("uiAmount", 0))
                raw_amount = int(value.data.parsed["info"].get("tokenAmount", {}).get("amount", 0))
                if mint:
                    balances[str(mint)] = (float(amount), raw_amount)
            return balances
        except Exception as e:
            logging.info(f"Error fetching vault reserves: {e}")
            traceback.print_exc()
            return {}

    async def priority_fee_levels(
        self,
        msg: VersionedMessage | None = None,
        cu: int = _DEFAULT_CU
    ) -> dict[str, float]:
        """
        Returns:
            dict[str, float]: Priority fee levels
                - "low": 25th percentile
                - "medium": 50th percentile
                - "high": 75th percentile
                - "turbo": 99th percentile
        """

        if msg is not None:
            accs = [str(k) for k in msg.account_keys[:32]]
        else:
            accs = []

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getRecentPrioritizationFees",
            "params": [accs],
        }

        async with self.session.post(self.ctx._provider.endpoint_uri, json=payload, timeout=4) as r:
            rows = (await r.json()).get("result", [])

        vals = [row.get("prioritizationFee", 0) for row in rows if row.get("prioritizationFee")]
        if not vals:
            return {"low": 0.0, "medium": 0.0, "high": 0.0, "turbo": 0.0}

        vals.sort()

        def _quantile(sorted_vals: list[int], q: float) -> int:
            # Percentile with linear interpolation
            if not sorted_vals:
                return 0
            k = (len(sorted_vals) - 1) * q
            f = int(k)
            c = min(f + 1, len(sorted_vals) - 1)
            if f == c:
                return sorted_vals[int(k)]
            d0 = sorted_vals[f] * (c - k)
            d1 = sorted_vals[c] * (k - f)
            return int(d0 + d1)

        q25 = _quantile(vals, 0.25)
        q50 = _quantile(vals, 0.50)
        q75 = _quantile(vals, 0.75)
        q99 = _quantile(vals, 0.99)

        def _to_sol(micro: int) -> float:
            return round((micro / _MICRO) * cu / LAMPORTS_PER_SOL, 10)

        q25 = _to_sol(q25)
        q50 = _to_sol(q50)
        q75 = _to_sol(q75)
        q99 = _to_sol(q99)

        return {
            "low": q25,
            "medium": q50,
            "high": q75,
            "turbo": q99,
        } if q25 > 0 and q50 > 0 and q75 > 0 and q99 > 0 else {
            "low": 0.000001,
            "medium": 0.00002,
            "high": 0.0003,
            "turbo": 0.002,
        }

    async def get_price(self, mint: str | Pubkey, pool: str | Pubkey, dex: str):
        """
        Get the price of a mint.
        Args:
            mint: str | Pubkey
            pool: str | Pubkey
            dex: str
        Returns:
            float: price
        """
        if dex == SUPPORTED_DEXES["MeteoraDamm1"]:
            price = await self.router.damm_v1.core.get_price(mint, pool_addr=pool)
        elif dex == SUPPORTED_DEXES["MeteoraDamm2"]:
            price = await self.router.damm_v2.core.get_price(mint, pool_addr=pool)
        elif dex == SUPPORTED_DEXES["MeteoraDBC"] or dex == SUPPORTED_DEXES["Believe"]:
            price = await self.router.meteora_dbc.get_price(pool, self.ctx)
        elif dex == SUPPORTED_DEXES["MeteoraDLMM"]:
            price = await self.router.dlmm.core.get_price(pool_addr=pool)
        elif dex == SUPPORTED_DEXES["PumpFun"]:
            price = await self.router.pump_fun.get_price(mint)
        elif dex == SUPPORTED_DEXES["PumpSwap"]:
            price = await self.router.pump_swap.fetch_pool_base_price(pool)
        elif dex == SUPPORTED_DEXES["RaydiumAMM"]:
            price = await self.router.raydiumswap_v4.raydium_core.get_price(pool)
        elif dex == SUPPORTED_DEXES["RayCLMM"]:
            price = await self.router.clmm_swap.core.get_price(pool)
        elif dex == SUPPORTED_DEXES["RayCPMM"]:
            price = await self.router.cpmm_swap.core.get_price(pool)
        elif dex == SUPPORTED_DEXES["Launchpad"]:
            price = await self.router.launchlab_swap.core.get_price(pool)
        else:
            raise Exception(f"CobraSwaps | Unsupported DEX: {dex}")
        
        if price and isinstance(price, dict):
            price = float(price["price"])

        elif price and isinstance(price, tuple):
            price = float(price[0])

        return float(price)

    async def buy(
        self, 
        mint: str | Pubkey, 
        pool: str | Pubkey, 
        keypair: Keypair,
        sol_amount: float,
        slippage: float = 10,
        priority_fee_level: str = "medium",
        dex: str = SUPPORTED_DEXES["RaydiumAMM"],
        **kwargs
    ):
        """
        Buy a mint. Remember to pass the dex you want to use, and the pool you want to buy from.

        Args:
            mint: str | Pubkey
            pool: str | Pubkey
            keypair: Keypair
            sol_amount: float
            slippage: float = 10
            priority_fee_level: str = "medium"
            dex: str = SUPPORTED_DEXES["RaydiumAMM"]
            **kwargs
        Returns:
            tuple: (tx_hash, success)
        """
        try:
            return_instructions = kwargs.get("return_instructions", False) == True

            ixs = []
            sim, is_dlmm = False, False
            versioned_message = None
            blockhash = (await self.ctx.get_latest_blockhash()).value.blockhash

            if dex == SUPPORTED_DEXES["MeteoraDamm1"]:
                state = await self.router.damm_v1.core.fetch_pool_state(pool)
                ixs = await self.router.damm_v1.buy(mint, state, int(sol_amount * LAMPORTS_PER_SOL), keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["MeteoraDamm2"]:
                state = await self.router.damm_v2.core.fetch_pool_state(pool)
                swap_params = await self.router.damm_v2.build_swap_params(
                    state, 
                    pool, 
                    mint, 
                    int(sol_amount * LAMPORTS_PER_SOL),
                    keypair
                )
                ixs = await self.router.damm_v2.buy(swap_params, keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["MeteoraDLMM"]:
                sim, is_dlmm = True, True
                state = await self.router.dlmm.core.fetch_pool_state(pool)
                ixs = await self.router.dlmm.buy(mint, state, int(sol_amount * LAMPORTS_PER_SOL), keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["MeteoraDBC"] or dex == SUPPORTED_DEXES["Believe"]:
                _, state = await self.router.meteora_dbc.fetch_state(mint)
                ixs = await self.router.meteora_dbc.swap.buy(state, int(sol_amount * LAMPORTS_PER_SOL), 1, keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["PumpFun"]:
                price = await self.router.pump_fun.get_price(mint)
                if price is None:
                    raise Exception(f"CobraSwaps | Price for {mint} is None")
                token_amount = await self.router.pump_fun.lamports_to_tokens(int(sol_amount * LAMPORTS_PER_SOL), price)
                creator = await self.router.get_pump_fun_creator(self.ctx, str(pool))
                ixs = await self.router.pump_fun.pump_buy(mint, pool, int(sol_amount * LAMPORTS_PER_SOL), creator, keypair, token_amount, slippage=slippage, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["PumpSwap"]:
                pool_keys, pool_type = await self.router.pump_swap_fetch_state(pool, self.ctx)
                base_price, base_balance_tokens, quote_balance_sol = await self.router.pump_swap.fetch_pool_base_price(pool)
                decimals_base = await self.router.get_decimals(mint)
                decimals_quote = await self.router.get_decimals(pool_keys["quote_mint"])
                pool_data = {
                    "pool_pubkey": Pubkey.from_string(str(pool)),
                    "token_base": Pubkey.from_string(pool_keys["base_mint"]),
                    "token_quote": Pubkey.from_string(pool_keys["quote_mint"]),
                    "pool_base_token_account": pool_keys["pool_base_token_account"],
                    "pool_quote_token_account": pool_keys["pool_quote_token_account"],
                    "base_balance_tokens": base_balance_tokens,
                    "quote_balance_sol": quote_balance_sol,
                    "decimals_base": decimals_base,
                    "decimals_quote": decimals_quote,
                }
                if pool_type == "NEW":
                    pool_data["coin_creator"] = Pubkey.from_string(pool_keys["coin_creator"])
                if str(pool_keys["base_mint"]) == "So11111111111111111111111111111111111111112":
                    ixs = await self.router.pump_swap.reversed_buy(pool_data, sol_amount, keypair, pool_type, slippage_pct=slippage, return_instructions=True)
                else:
                    ixs = await self.router.pump_swap.buy(pool_data, sol_amount, keypair, pool_type, slippage_pct=slippage, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["RaydiumAMM"]:
                ixs = await self.router.raydiumswap_v4.execute_buy_async(mint, sol_amount, slippage, 0, pool, keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["RayCLMM"]:
                state = await self.router.clmm_swap.core.async_fetch_pool_keys(pool)
                ixs = await self.router.clmm_swap.execute_clmm_buy_async(mint, sol_amount, keypair, 1, 0, pool, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["RayCPMM"]:
                state = await self.router.cpmm_swap.core.async_fetch_pool_keys(pool)
                ixs = await self.router.cpmm_swap.execute_cpmm_buy_async(mint, sol_amount, keypair, slippage, 0, pool, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["Launchpad"]:
                state = await self.router.launchlab_swap.core.async_fetch_pool_keys(pool)
                ixs = await self.router.launchlab_swap.execute_lp_buy_async(mint, sol_amount, slippage, keypair, pool, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)

            if return_instructions:
                return ixs

            if versioned_message is None:
                raise Exception("CobraSwaps | No versioned message found")

            priority_fee = await self.priority_fee_levels(versioned_message)
            logging.info(f"Currently using: {priority_fee_level} | Low: {priority_fee['low']:.8f} | Medium: {priority_fee['medium']:.8f} | High: {priority_fee['high']:.8f} | Turbo: {priority_fee['turbo']:.8f}")
            priority_fee = priority_fee[priority_fee_level]
            if priority_fee is None:
                priority_fee = 0.000005
            
            if priority_fee > 0.01:
                raise Exception("CobraSwaps | Priority fee is too high (Over 0.01 SOL)")

            if ixs:
                lamports_fee = int(priority_fee * LAMPORTS_PER_SOL)
                micro_lamports = compute_unit_price_from_total_fee(
                    lamports_fee,
                    compute_units=_DEFAULT_CU
                )

                ixs.append(set_compute_unit_limit(_DEFAULT_CU))
                ixs.append(set_compute_unit_price(micro_lamports))
                ver_msg = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
                tx = VersionedTransaction(ver_msg, [keypair])

                if sim:
                    simulate_resp = await self.ctx.simulate_transaction(tx)
                    if simulate_resp.value and simulate_resp.value.err:
                        if not is_dlmm:
                            logging.info(f"Simulation result: {simulate_resp.value}")
                            raise Exception(f"Simulation failed: {simulate_resp.value.err}")
                        else:
                            return ("replay", False)

                result = await self.ctx.send_transaction(tx, opts=TxOpts(skip_preflight=True, max_retries=0))
                logging.info(f"Cobra | Buy transaction sent: {result.value}")
                ok = await self._await_confirm(result.value)
                logging.info(f"Cobra | Buy transaction confirmed: {ok}")
                return (result.value, ok)

            return ixs
        except Exception as e:
            logging.error(f"CobraSwaps | Error buying: {e}")
            traceback.print_exc()
            return (None, False)

    def _build_system_transfer_ix(self, from_pubkey: Pubkey, to_pubkey: Pubkey, lamports: int):
        """
        Build a system transfer instruction.
        """
        return transfer(
            TransferParams(
                from_pubkey=from_pubkey,
                to_pubkey=to_pubkey,
                lamports=lamports
            )
        )

    async def _build_token_transfer_ix(self, from_pubkey: Pubkey, to_pubkey: Pubkey, mint: Pubkey, amount: int, decimals: int):
        """
        Build a token transfer instruction.
        """
        program_id = await self._mint_owner(mint)
        mint_ata = get_associated_token_address(from_pubkey, mint, program_id)
        return transfer_checked(
            TransferCheckedParams(
                program_id=program_id,
                source=mint_ata,
                mint=mint,
                dest=to_pubkey,
                owner=from_pubkey,
                amount=amount,
                decimals=decimals,
                signers=[]
            )
        )

    async def send_transfer(self, keypair: Keypair, mint: str | Pubkey, amount: float, to: str | Pubkey, priority_fee_level: str = "medium", return_instructions: bool = False):
        """
        Send a transfer of a mint.
        """
        ixs = []
        blockhash = (await self.ctx.get_latest_blockhash()).value.blockhash
        mint = Pubkey.from_string(mint) if isinstance(mint, str) else mint
        to = Pubkey.from_string(to) if isinstance(to, str) else to

        if str(mint) == "So11111111111111111111111111111111111111112":
            ixs.append(
                self._build_system_transfer_ix(
                    keypair.pubkey(),
                    to,
                    int(amount * LAMPORTS_PER_SOL)
                )
            )
        else:
            resp = await self.ctx.get_token_accounts_by_owner(to, TokenAccountOpts(mint=mint), Processed)
            if resp.value:
                create_ata_ix = None
            else:
                create_ata_ix = create_associated_token_account(
                    keypair.pubkey(), to, mint,
                    token_program_id=await self._mint_owner(mint)
                )

            if create_ata_ix:
                ixs.append(create_ata_ix)

            decimals = await self.router.get_decimals(mint)
            amount_in = int(amount * 10**decimals)
            ixs.append(
                await self._build_token_transfer_ix(
                    from_pubkey=keypair.pubkey(),
                    to_pubkey=get_associated_token_address(to, mint, await self._mint_owner(mint)),
                    mint=mint,
                    amount=amount_in,
                    decimals=decimals
                )
            )

        if return_instructions:
            return ixs

        ver_msg = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
        priority_fee = await self.priority_fee_levels(ver_msg)
        logging.info(f"Currently using: {priority_fee_level} | Low: {priority_fee['low']:.8f} | Medium: {priority_fee['medium']:.8f} | High: {priority_fee['high']:.8f} | Turbo: {priority_fee['turbo']:.8f}")
        priority_fee = priority_fee[priority_fee_level]
        if priority_fee is None:
            priority_fee = 0.000005
        
        if priority_fee > 0.01:
            raise Exception("CobraSwaps | Priority fee is too high (Over 0.01 SOL)")

        if ixs:
            lamports_fee = int(priority_fee * LAMPORTS_PER_SOL)
            micro_lamports = compute_unit_price_from_total_fee(
                lamports_fee,
                compute_units=_DEFAULT_CU
            )

            ixs.append(set_compute_unit_limit(_DEFAULT_CU))
            ixs.append(set_compute_unit_price(micro_lamports))

            tx = VersionedTransaction(ver_msg, [keypair])
            result = await self.ctx.send_transaction(tx, opts=TxOpts(skip_preflight=True, max_retries=0))
            logging.info(f"Cobra | Transfer transaction sent: {result.value}")
            ok = await self._await_confirm(result.value)
            logging.info(f"Cobra | Transfer transaction confirmed: {ok}")
            return (result.value, ok)
        
        return (None, False)

    async def _await_confirm(self, sig, tries=3, delay=2):
        """
        Await a transaction to be confirmed.
        """
        for _ in range(tries):
            res = await self.ctx.get_transaction(sig, commitment=Confirmed, max_supported_transaction_version=0)
            if res.value and res.value.transaction.meta.err is None:
                return True
            await asyncio.sleep(delay)
        return False

    async def sell(
        self, 
        mint: str | Pubkey, 
        pool: str | Pubkey, 
        keypair: Keypair,
        sell_pct: float = 100.0, 
        slippage: float = 10,
        priority_fee_level: str = "medium",
        dex: str = SUPPORTED_DEXES["RaydiumAMM"],
        **kwargs
    ):
        try:
            """
            Sell tokens for SOL across different DEX platforms
            
            Args:
                mint: Token mint address to sell
                pool: Pool address for the token
                keypair: Keypair
                sell_pct: Percentage of token balance to sell (0-100)
                slippage: Slippage tolerance (0.01 = 1%)
                priority_fee_level: Priority fee level ("low", "medium", "high")
                dex: DEX to use for the swap
            
            Returns:
                tuple: (transaction_signature, confirmation_status)
            """
            return_instructions = kwargs.get("return_instructions", False) == True
            sim, is_dlmm = False, False
            ixs = []
            versioned_message = None
            blockhash = (await self.ctx.get_latest_blockhash()).value.blockhash

            if dex == SUPPORTED_DEXES["MeteoraDamm1"]:
                state = await self.router.damm_v1.core.fetch_pool_state(pool)
                ixs = await self.router.damm_v1.sell(mint, state, sell_pct, keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["MeteoraDamm2"]:
                if sell_pct is None or sell_pct <= 0:
                    raise ValueError("Percentage can't be 0 and is required for sell actions")

                if sell_pct > 100:
                    raise ValueError("Percentage can't be greater than 100")

                token_pk = Pubkey.from_string(mint) if isinstance(mint, str) else mint

                dec_base = await self.router.get_decimals(mint)

                bal_resp = await self.ctx.get_token_accounts_by_owner_json_parsed(
                    keypair.pubkey(), TokenAccountOpts(mint=token_pk), Processed
                )
                if not bal_resp.value:
                    raise RuntimeError("no balance")

                token_balance = float(bal_resp.value[0].account.data.parsed["info"]["tokenAmount"]["uiAmount"] or 0)
                if token_balance <= 0:
                    raise RuntimeError("insufficient token balance")

                sell_amount = token_balance * (sell_pct / 100)
                if sell_amount <= 0:
                    raise RuntimeError("sell amount too small")
                
                tokens_in = int(sell_amount * 10**dec_base)
                state = await self.router.damm_v2.core.fetch_pool_state(pool)
                swap_params = await self.router.damm_v2.build_swap_params(
                    state, 
                    pool, 
                    mint, 
                    tokens_in,
                    keypair
                )
                ixs = await self.router.damm_v2.sell(swap_params, keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["MeteoraDLMM"]:
                sim, is_dlmm = True, True
                state = await self.router.dlmm.core.fetch_pool_state(pool)
                ixs = await self.router.dlmm.sell(mint, state, sell_pct, keypair=keypair, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["MeteoraDBC"] or dex == SUPPORTED_DEXES["Believe"]:
                _, state = await self.router.meteora_dbc.fetch_state(mint)
                ixs = await self.router.meteora_dbc.swap.sell(state, sell_pct, keypair=keypair, slippage_pct=slippage, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["PumpFun"]:
                token_pk = Pubkey.from_string(mint) if isinstance(mint, str) else mint
                bal_resp = await self.ctx.get_token_accounts_by_owner_json_parsed(
                    keypair.pubkey(), TokenAccountOpts(mint=token_pk), Processed
                )
                if not bal_resp.value:
                    raise Exception("No token balance found")
                
                token_balance = float(bal_resp.value[0].account.data.parsed["info"]["tokenAmount"]["uiAmount"] or 0)
                if token_balance <= 0:
                    raise Exception("Insufficient token balance")
                
                sell_amount = token_balance * (sell_pct / 100)
                token_amount = int(sell_amount * 10**6)
                
                price = await self.router.pump_fun.get_price(mint)
                if price is None or price == "NotOnPumpFun" or price == "migrated":
                    raise Exception(f"Cannot get price for {mint}")
                
                lamports_min_output = int(sell_amount * price * LAMPORTS_PER_SOL * float(1 - slippage/100))
                creator = await self.router.get_pump_fun_creator(self.ctx, str(pool))
                
                ixs = await self.router.pump_fun.pump_sell(
                    mint, pool, token_amount, lamports_min_output, creator, keypair=keypair, return_instructions=True
                )
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["PumpSwap"]:
                pool_keys, pool_type = await self.router.pump_swap_fetch_state(pool, self.ctx)
                base_price, base_balance_tokens, quote_balance_sol = await self.router.pump_swap.fetch_pool_base_price(pool)
                decimals_base = await self.router.get_decimals(mint)
                decimals_quote = await self.router.get_decimals(pool_keys["quote_mint"])
                pool_data = {
                    "pool_pubkey": Pubkey.from_string(str(pool)),
                    "token_base": Pubkey.from_string(pool_keys["base_mint"]),
                    "token_quote": Pubkey.from_string(pool_keys["quote_mint"]),
                    "pool_base_token_account": pool_keys["pool_base_token_account"],
                    "pool_quote_token_account": pool_keys["pool_quote_token_account"],
                    "base_balance_tokens": base_balance_tokens,
                    "quote_balance_sol": quote_balance_sol,
                    "decimals_base": decimals_base,
                    "decimals_quote": decimals_quote,
                }
                if pool_type == "NEW":
                    pool_data["coin_creator"] = Pubkey.from_string(pool_keys["coin_creator"])

                if str(pool_keys["base_mint"]) == "So11111111111111111111111111111111111111112":
                    ixs = await self.router.pump_swap.reversed_sell(pool_data, sell_pct, keypair, pool_type, slippage_pct=slippage, debug_prints=True, return_instructions=True)
                else:
                    ixs = await self.router.pump_swap.sell(pool_data, sell_pct, keypair, pool_type, slippage_pct=slippage, debug_prints=True, return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["RaydiumAMM"]:
                ixs = await self.router.raydiumswap_v4.execute_sell_async(mint, keypair, int(sell_pct), int(slippage), return_instructions=True)
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["RayCLMM"]:
                ixs = await self.router.clmm_swap.execute_clmm_sell_async(
                    mint, keypair, int(sell_pct), int(slippage), pool_id=pool, return_instructions=True
                )
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["RayCPMM"]:
                ixs = await self.router.cpmm_swap.execute_cpmm_sell_async(
                    mint, keypair, sell_pct, slippage_pct=slippage, pool_hint=pool, return_instructions=True
                )
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
            elif dex == SUPPORTED_DEXES["Launchpad"]:
                ixs = await self.router.launchlab_swap.execute_lp_sell_async(
                    mint, keypair, sell_pct, slippage_pct=slippage, pool_id=pool, return_instructions=True
                )
                versioned_message = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)

            if return_instructions:
                return ixs

            if versioned_message is None:
                raise Exception("CobraSwaps | No versioned message found")

            priority_fee = await self.priority_fee_levels(versioned_message)
            logging.info(f"Currently using: {priority_fee_level} | Low: {priority_fee['low']:.8f} | Medium: {priority_fee['medium']:.8f} | High: {priority_fee['high']:.8f} | Turbo: {priority_fee['turbo']:.8f}")
            priority_fee = priority_fee[priority_fee_level]
            if priority_fee is None:
                priority_fee = 0.000005
            
            if priority_fee > 0.01:
                raise Exception("CobraSwaps | Priority fee is too high (Over 0.01 SOL)")

            if ixs:
                lamports_fee = int(priority_fee * LAMPORTS_PER_SOL)
                micro_lamports = compute_unit_price_from_total_fee(
                    lamports_fee,
                    compute_units=_DEFAULT_CU
                )

                ixs.append(set_compute_unit_limit(_DEFAULT_CU))
                ixs.append(set_compute_unit_price(micro_lamports))
                ver_msg = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
                tx = VersionedTransaction(ver_msg, [keypair])

                if sim:
                    simulate_resp = await self.ctx.simulate_transaction(tx)
                    if simulate_resp.value and simulate_resp.value.err:
                        if not is_dlmm:
                            logging.info(f"Simulation result: {simulate_resp.value}")
                            raise Exception(f"Simulation failed: {simulate_resp.value.err}")
                        else:
                            return ("replay", False)

                result = await self.ctx.send_transaction(tx, opts=TxOpts(skip_preflight=True, max_retries=0))
                logging.info(f"Cobra | Sell transaction sent: {result.value}")
                ok = await self._await_confirm(result.value)
                logging.info(f"Cobra | Sell transaction confirmed: {ok}")
                return (result.value, ok)

            return (None, False)
        except Exception as e:
            logging.error(f"CobraSwaps | Error selling: {e}")
            traceback.print_exc()
            return (None, False)

    async def close(self):
        try:
            await self.session.close()
        except Exception as e:
            return False

