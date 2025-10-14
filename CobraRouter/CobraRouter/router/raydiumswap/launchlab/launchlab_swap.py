import os, base64, asyncio, traceback
import struct
from typing import Tuple
import logging
from solana.rpc.types import TokenAccountOpts, TxOpts
from solana.rpc.commitment import Processed, Confirmed
from solana.rpc.async_api import AsyncClient

from solders.pubkey import Pubkey # type: ignore
from solders.keypair import Keypair # type: ignore
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price # type: ignore
from solders.transaction import VersionedTransaction # type: ignore
from solders.message import MessageV0 # type: ignore
from spl.token.instructions import (
    create_associated_token_account,
    get_associated_token_address,
    initialize_account,
    InitializeAccountParams,
    CloseAccountParams, close_account
)
from solders.system_program import CreateAccountWithSeedParams, create_account_with_seed
from solders.instruction import Instruction, AccountMeta # type: ignore

try: from launchlab_core import RaydiumLaunchpadCore;
except: from .launchlab_core import RaydiumLaunchpadCore
from solders.system_program import ID as SYSTEM_PROGRAM_ID

RENT_EXEMPT     = 2039280
ACCOUNT_SIZE    = 165
SOL_DECIMALS    = 1e9
COMPUTE_UNITS   = 150_000
TOKEN_PROGRAM   = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

BUY_EXACT_IN_DISCRIM = bytes([250, 234, 13, 123, 213, 156, 19, 236])
SELL_EXACT_IN_DISCRIM = bytes([149, 39, 222, 155, 211, 124, 152, 26])

class RaydiumLaunchpadSwap:
    def __init__(self, client: AsyncClient):
        self.client = client
        self.core   = RaydiumLaunchpadCore(client)

    async def _mint_owner(self, mint: Pubkey) -> Pubkey:
        try:
            info = await self.client.get_account_info(mint, commitment=Processed)
            if info.value is None:
                raise RuntimeError("mint account missing")
            return info.value.owner
        except Exception as e:
            traceback.print_exc()
            logging.info(f"Failed to get token program id: {e}")
            return TOKEN_PROGRAM

    @staticmethod
    def convert_sol_to_tokens(sol: float, r_base: float, r_quote: float, fee_pct=1.0) -> float:
        eff = sol * (1 - fee_pct / 100)
        k   = r_base * r_quote
        new_base = k / (r_quote + eff)
        return round(r_base - new_base, 9)

    @staticmethod
    def convert_tokens_to_sol(tokens: float, r_base: float, r_quote: float, fee_pct=1.0) -> float:
        eff   = tokens * (1 - fee_pct / 100)
        k     = r_base * r_quote
        new_q = k / (r_base + eff)
        return round(r_quote - new_q, 9)

    async def execute_lp_buy_async(
        self,
        token_mint: str,
        sol_amount: float,
        slippage_pct: float,
        keypair: Keypair,
        pool_id: str | Pubkey | None = None,
        fee_micro_lamports: int = 1_000_000,
        return_instructions: bool = False,
    ) -> Tuple[bool, str]:
        
        sol_lamports = int(sol_amount * SOL_DECIMALS)
        if pool_id is None:
            pool = await self.find_launchpad_pool_by_mint(token_mint)
            if not pool:
                raise RuntimeError("no Launchpad pool found")
            pool_id = pool
        pool_pk = pool_id if isinstance(pool_id, Pubkey) else Pubkey.from_string(pool_id)

        keys = await self.core.async_fetch_pool_keys(pool_pk)
        if keys is None:
            raise RuntimeError("cannot decode Launchpad pool")

        out_mint = Pubkey.from_string(token_mint)
        token_program_id = await self._mint_owner(out_mint)
        resp = await self.client.get_token_accounts_by_owner(keypair.pubkey(), TokenAccountOpts(mint=out_mint), Processed)
        if resp.value:
            user_ata = resp.value[0].pubkey
            create_ata_ix = None
        else:
            user_ata = get_associated_token_address(keypair.pubkey(), out_mint, token_program_id=token_program_id)
            create_ata_ix = create_associated_token_account(
                keypair.pubkey(), keypair.pubkey(), out_mint, token_program_id=token_program_id
            )

        expected = self.core.calculate_constant_product_swap(keys, sol_lamports / SOL_DECIMALS)
        min_out  = int(expected * (1 - slippage_pct/100) * 10**keys.decimals_a)
        logging.info(f"expected: {expected}, min_out: {min_out}")

        seed = base64.urlsafe_b64encode(os.urandom(12)).decode()
        temp_wsol = Pubkey.create_with_seed(keypair.pubkey(), seed, TOKEN_PROGRAM)
        create_w_ix = create_account_with_seed(CreateAccountWithSeedParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=temp_wsol,
            base=keypair.pubkey(),
            seed=seed,
            lamports=RENT_EXEMPT + sol_lamports,
            space=ACCOUNT_SIZE,
            owner=TOKEN_PROGRAM,
        ))

        init_w_ix = initialize_account(
            InitializeAccountParams(
                program_id=TOKEN_PROGRAM,
                account=temp_wsol,
                mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
                owner=keypair.pubkey(),
            )
        )
        
        # PLATFORM_FEE_VAULT
        platform_fee_vault, _ = Pubkey.find_program_address(
            [bytes(keys.platform_id), bytes(keys.mint_b)],
            keys.program_id
        )

        # CREATOR_FEE_VAULT
        creator_fee_vault, _ = Pubkey.find_program_address(
            [bytes(keys.creator), bytes(keys.mint_b)],
            keys.program_id
        )

        metas = [
            AccountMeta(keypair.pubkey(), True, False),
            AccountMeta(keys.authority, False, False),
            AccountMeta(keys.config_id, False, False),
            AccountMeta(keys.platform_id, False, False),
            AccountMeta(keys.pool_id, False, True),

            AccountMeta(user_ata, False, True),
            AccountMeta(temp_wsol, False, True),

            AccountMeta(keys.vault_a, False, True),
            AccountMeta(keys.vault_b, False, True),

            AccountMeta(keys.mint_a, False, False),
            AccountMeta(keys.mint_b, False, False),

            AccountMeta(await self._mint_owner(keys.mint_a), False, False),
            AccountMeta(await self._mint_owner(keys.mint_b), False, False),

            AccountMeta(keys.event_auth, False, False),
            AccountMeta(keys.program_id, False, False),
            
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(platform_fee_vault, False, True),
            AccountMeta(creator_fee_vault, False, True),
        ]
        data = (
            BUY_EXACT_IN_DISCRIM
            + struct.pack("<Q", sol_lamports)
            + struct.pack("<Q", min_out)
            + struct.pack("<Q", 0)   # shareFeeRate = 0
        )
        swap_ix = Instruction(keys.program_id, data, metas)

        if not return_instructions:
            ixs = [
                set_compute_unit_limit(COMPUTE_UNITS),
                set_compute_unit_price(fee_micro_lamports),
            ]
        else:
            ixs = []

        ixs.append(create_w_ix)
        ixs.append(init_w_ix)

        if create_ata_ix:
            ixs.append(create_ata_ix)
        ixs.append(swap_ix)
        ixs.append(close_account(CloseAccountParams(
            program_id=TOKEN_PROGRAM,
            account=temp_wsol,
            dest=keypair.pubkey(),
            owner=keypair.pubkey(),
        )))

        if return_instructions:
            return ixs

        blockhash = (await self.client.get_latest_blockhash()).value.blockhash
        msg      = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
        tx       = VersionedTransaction(msg, [keypair])
        sig_resp = await self.client.send_transaction(tx, opts=TxOpts(skip_preflight=True, max_retries=0))
        ok       = await self._await_confirm(sig_resp.value)
        return ok, sig_resp.value

    async def execute_lp_sell_async(
        self,
        token_mint: str,
        keypair: Keypair,
        sell_pct: float = 100,
        slippage_pct: float = 5,
        pool_id: str | Pubkey | None = None,
        fee_micro_lamports: int = 1_000_000,
        return_instructions: bool = False,
    ) -> Tuple[bool, str]:
        if pool_id is None:
            pool = await self.core.find_launchpad_pool_by_mint(token_mint)
            if not pool:
                raise RuntimeError("no Launchpad pool found")
            pool_id = pool
        pool_pk = pool_id if isinstance(pool_id, Pubkey) else Pubkey.from_string(pool_id)

        keys = await self.core.async_fetch_pool_keys(pool_pk)
        if keys is None:
            raise RuntimeError("cannot decode Launchpad pool")

        token_pk = Pubkey.from_string(token_mint)
        bal_resp = await self.client.get_token_accounts_by_owner_json_parsed(
            keypair.pubkey(), TokenAccountOpts(mint=token_pk), Processed
        )
        if not bal_resp.value:
            raise RuntimeError("no balance")

        token_balance = float(bal_resp.value[0].account.data.parsed["info"]["tokenAmount"]["uiAmount"] or 0)
        
        user_ata = bal_resp.value[0].pubkey
        
        if token_balance <= 0:
            raise RuntimeError("insufficient token balance")

        sell_amount = token_balance * (sell_pct / 100)
        if sell_amount <= 0:
            raise RuntimeError("sell amount too small")

        expected_sol = self.core.calculate_constant_product_sell(keys, sell_amount)
        min_sol_out = int(expected_sol * (1 - slippage_pct/100) * SOL_DECIMALS)
        token_amount_raw = int(sell_amount * 10**keys.decimals_a)
        
        logging.info(f"Selling: {sell_amount} tokens, expected SOL: {expected_sol}, min SOL out: {min_sol_out}")

        seed = base64.urlsafe_b64encode(os.urandom(12)).decode()
        temp_wsol = Pubkey.create_with_seed(keypair.pubkey(), seed, TOKEN_PROGRAM)
        create_w_ix = create_account_with_seed(CreateAccountWithSeedParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=temp_wsol,
            base=keypair.pubkey(),
            seed=seed,
            lamports=RENT_EXEMPT,
            space=ACCOUNT_SIZE,
            owner=TOKEN_PROGRAM,
        ))

        init_w_ix = initialize_account(
            InitializeAccountParams(
                program_id=TOKEN_PROGRAM,
                account=temp_wsol,
                mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
                owner=keypair.pubkey(),
            )
        )
        
        # PLATFORM_FEE_VAULT
        platform_fee_vault, _ = Pubkey.find_program_address(
            [bytes(keys.platform_id), bytes(keys.mint_b)],
            keys.program_id
        )

        # CREATOR_FEE_VAULT
        creator_fee_vault, _ = Pubkey.find_program_address(
            [bytes(keys.creator), bytes(keys.mint_b)],
            keys.program_id
        )

        metas = [
            AccountMeta(keypair.pubkey(), True, False),
            AccountMeta(keys.authority, False, False),
            AccountMeta(keys.config_id, False, False),
            AccountMeta(keys.platform_id, False, False),
            AccountMeta(keys.pool_id, False, True),

            AccountMeta(user_ata, False, True),
            AccountMeta(temp_wsol, False, True),

            AccountMeta(keys.vault_a, False, True),
            AccountMeta(keys.vault_b, False, True),

            AccountMeta(keys.mint_a, False, False),
            AccountMeta(keys.mint_b, False, False),

            AccountMeta(await self._mint_owner(keys.mint_a), False, False),
            AccountMeta(await self._mint_owner(keys.mint_b), False, False),

            AccountMeta(keys.event_auth, False, False),
            AccountMeta(keys.program_id, False, False),
            
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(platform_fee_vault, False, True),
            AccountMeta(creator_fee_vault, False, True),
        ]
        data = (
            SELL_EXACT_IN_DISCRIM
            + struct.pack("<Q", token_amount_raw)
            + struct.pack("<Q", min_sol_out)
            + struct.pack("<Q", 0)   # shareFeeRate = 0
        )
        sell_ix = Instruction(keys.program_id, data, metas)

        ixs = [
            create_w_ix, init_w_ix,
            sell_ix,
            close_account(CloseAccountParams(
                program_id=TOKEN_PROGRAM,
                account=temp_wsol,
                dest=keypair.pubkey(),
                owner=keypair.pubkey(),
            ))
        ]

        if not return_instructions:
            ixs.append(set_compute_unit_limit(COMPUTE_UNITS))
            ixs.append(set_compute_unit_price(fee_micro_lamports))

        if return_instructions:
            return ixs

        blockhash = (await self.client.get_latest_blockhash()).value.blockhash
        msg      = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
        tx       = VersionedTransaction(msg, [keypair])
        sig_resp = await self.client.send_transaction(tx, opts=TxOpts(skip_preflight=True, max_retries=0))
        ok       = await self._await_confirm(sig_resp.value)
        return ok, sig_resp.value

    async def _await_confirm(self, sig: str, tries=3, delay=2):
        for _ in range(tries):
            res = await self.client.get_transaction(sig, commitment=Confirmed, max_supported_transaction_version=0)
            if res.value and res.value.transaction.meta.err is None:
                return True
            await asyncio.sleep(delay)
        return False

    async def close(self):
        await self.client.close()
