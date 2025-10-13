import traceback
from dataclasses import dataclass
from typing import Optional, Tuple
import logging
from solana.rpc.commitment import Processed, Confirmed
from solders.pubkey import Pubkey # type: ignore
from construct import Bytes, Int8ul, Int64ul, Struct as cStruct
from solana.rpc.types import MemcmpOpts, DataSliceOpts
import solana.exceptions
LAUNCHPAD_PROGRAM_ID = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")

AUTH_SEED         = b"vault_auth_seed"
POOL_SEED        = b"pool"
POOL_VAULT_SEED  = b"pool_vault"
EVENT_AUTH_SEED  = b"__event_authority"

LAUNCHPAD_POOL_LAYOUT = cStruct(
    "padding"          / Bytes(8),
    "epoch"            / Int64ul,
    "bump"             / Int8ul,
    "status"           / Int8ul,
    "dec_a"            / Int8ul,
    "dec_b"            / Int8ul,
    "migrate_type"     / Int8ul,

    "supply"           / Int64ul,
    "total_sell_a"     / Int64ul,
    "virtual_a"        / Int64ul,
    "virtual_b"        / Int64ul,
    "real_a"           / Int64ul,
    "real_b"           / Int64ul,
    "total_fund_b"     / Int64ul,
    "protocol_fee"     / Int64ul,
    "platform_fee"     / Int64ul,
    "migrate_fee"      / Int64ul,
    Bytes(5 * 8),  # vesting schedule

    "config_id"        / Bytes(32),
    "platform_id"      / Bytes(32),
    "mint_a"           / Bytes(32),
    "mint_b"           / Bytes(32),
    "vault_a"          / Bytes(32),
    "vault_b"          / Bytes(32),
    "creator"          / Bytes(32),
)

LAUNCHPAD_STATUS_LAYOUT = cStruct(
    "padding"          / Bytes(8),
    "epoch"            / Int64ul,
    "bump"             / Int8ul,
    "status"           / Int8ul,
)

@dataclass
class LaunchpadPoolKeys:
    program_id: Pubkey
    pool_id: Pubkey
    authority: Pubkey
    event_auth: Pubkey

    config_id: Pubkey
    platform_id: Pubkey

    mint_a: Pubkey
    mint_b: Pubkey
    decimals_a: int
    decimals_b: int

    virtual_a: int
    virtual_b: int
    real_a: int
    real_b: int
    vault_a: Pubkey
    vault_b: Pubkey
    creator: Pubkey

class RaydiumLaunchpadCore:
    def __init__(self, client):
        self.client = client

    async def find_launchpad_pool_by_mint(self, mint: str) -> str | None:
        try:
            mint_pk = Pubkey.from_string(mint)

            MINT_A_OFFSET = 205
            MINT_B_OFFSET = 237

            slice_opt = DataSliceOpts(offset=0, length=MINT_B_OFFSET + 32)

            for off in (MINT_A_OFFSET, MINT_B_OFFSET):
                resp = await self.client.get_program_accounts(
                    LAUNCHPAD_PROGRAM_ID,
                    commitment=Confirmed,
                    encoding="base64",
                    data_slice=slice_opt,
                    filters=[MemcmpOpts(offset=off, bytes=str(mint_pk))]
                )
                if resp.value:
                    return str(resp.value[0].pubkey)

            return None
        except solana.exceptions.SolanaRpcException:
            return None
        except Exception as e:
            traceback.print_exc()
            return None

    async def launchpad_check_has_migrated(self, pool_id: str | Pubkey) -> bool:
        pool_pk = pool_id if isinstance(pool_id, Pubkey) else Pubkey.from_string(pool_id)
        try:
            acc = await self.client.get_account_info_json_parsed(pool_pk, commitment=Processed)
            raw = LAUNCHPAD_STATUS_LAYOUT.parse(acc.value.data)
            return raw.status == 2
        except Exception as e:
            traceback.print_exc()
            return False
        
    async def async_fetch_pool_keys(self, pool_id: str | Pubkey) -> Optional[LaunchpadPoolKeys]:
        pool_pk = pool_id if isinstance(pool_id, Pubkey) else Pubkey.from_string(pool_id)
        try:
            acc = await self.client.get_account_info_json_parsed(pool_pk, commitment=Processed)
            raw = LAUNCHPAD_POOL_LAYOUT.parse(acc.value.data)
            auth, _      = Pubkey.find_program_address([AUTH_SEED], LAUNCHPAD_PROGRAM_ID)
            evt_auth, _  = Pubkey.find_program_address([EVENT_AUTH_SEED], LAUNCHPAD_PROGRAM_ID)

            return LaunchpadPoolKeys(
                program_id   = LAUNCHPAD_PROGRAM_ID,
                pool_id      = pool_pk,
                authority    = auth,
                event_auth   = evt_auth,
                config_id    = Pubkey.from_bytes(raw.config_id),
                platform_id  = Pubkey.from_bytes(raw.platform_id),
                mint_a       = Pubkey.from_bytes(raw.mint_a),
                mint_b       = Pubkey.from_bytes(raw.mint_b),
                decimals_a   = raw.dec_a,
                decimals_b   = raw.dec_b,
                virtual_a    = raw.virtual_a,
                virtual_b    = raw.virtual_b,
                vault_a      = Pubkey.from_bytes(raw.vault_a),
                vault_b      = Pubkey.from_bytes(raw.vault_b),
                real_a       = raw.real_a,
                real_b       = raw.real_b,
                creator      = raw.creator,
            )
        except Exception as e:
            traceback.print_exc()
            logging.info(f"[Launchpad] pool decode failed: {e}")
            return None

    async def get_price(self, pool_addr: str | Pubkey):
        pool_addr = pool_addr if isinstance(pool_addr, Pubkey) else Pubkey.from_string(pool_addr)
        keys = await self.async_fetch_pool_keys(pool_addr)
        if keys is None:
            return None
        virtual_a_decimal = keys.virtual_a / (10 ** keys.decimals_a)
        virtual_b_decimal = keys.virtual_b / (10 ** keys.decimals_b)
        real_a_decimal = keys.real_a / (10 ** keys.decimals_a)
        real_b_decimal = keys.real_b / (10 ** keys.decimals_b)
        
        numerator = virtual_b_decimal + real_b_decimal
        denominator = virtual_a_decimal - real_a_decimal
        if denominator <= 0:
            return 0.0
        return (numerator / denominator)   

    async def async_get_pool_reserves(self, keys: LaunchpadPoolKeys) -> Tuple[float, float]:
        """
        Returns: (reserve_a, reserve_b) in decimal
        """
        try:
            infos = await self.client.get_multiple_accounts_json_parsed(
                [keys.vault_a, keys.vault_b], commitment=Processed
            )
            ui_a = infos.value[0].data.parsed["info"]["tokenAmount"]["uiAmount"]
            ui_b = infos.value[1].data.parsed["info"]["tokenAmount"]["uiAmount"]
            return float(ui_a or 0), float(ui_b or 0)
        except Exception as e:
            logging.info(f"[Launchpad] Error fetching vault reserves: {e}")
            return 0.0, 0.0

    @staticmethod
    def calculate_pool_price(keys: LaunchpadPoolKeys, curve_type: int = 0) -> float:
        """
        curve_type: 0 = Constant Product, 1 = Fixed Price, 2 = Linear Price
        """
        virtual_a_decimal = keys.virtual_a / (10 ** keys.decimals_a)
        virtual_b_decimal = keys.virtual_b / (10 ** keys.decimals_b)
        real_a_decimal = keys.real_a / (10 ** keys.decimals_a)
        real_b_decimal = keys.real_b / (10 ** keys.decimals_b)
        
        decimal_adjustment = 10 ** (keys.decimals_a - keys.decimals_b)
        
        if curve_type == 0:
            # Price = (virtualB + realB) / (virtualA - realA) * 10^(decimalA - decimalB)
            numerator = virtual_b_decimal + real_b_decimal
            denominator = virtual_a_decimal - real_a_decimal
            if denominator <= 0:
                return 0.0
            return (numerator / denominator) * decimal_adjustment
            
        elif curve_type == 1:
            # Price = virtualB / virtualA * 10^(decimalA - decimalB)
            if virtual_a_decimal <= 0:
                return 0.0
            return (virtual_b_decimal / virtual_a_decimal) * decimal_adjustment
            
        elif curve_type == 2:
            # Price = (virtualA * realA) / Q64 * 10^(decimalA - decimalB)
            Q64 = 2 ** 64
            return (keys.virtual_a * keys.real_a / Q64) * decimal_adjustment
            
        else:
            raise ValueError(f"Unknown curve type: {curve_type}")

    @staticmethod
    def calculate_constant_product_swap(keys: LaunchpadPoolKeys, sol_amount_decimal: float) -> float:
        """
        Calculate token output for constant product curve
        Uses: (virtualB + realB) and (virtualA - realA) as reserves
        """
        virtual_a_decimal = keys.virtual_a / (10 ** keys.decimals_a)
        virtual_b_decimal = keys.virtual_b / (10 ** keys.decimals_b)
        real_a_decimal = keys.real_a / (10 ** keys.decimals_a)
        real_b_decimal = keys.real_b / (10 ** keys.decimals_b)
        
        input_reserve = virtual_b_decimal + real_b_decimal
        output_reserve = virtual_a_decimal - real_a_decimal
        
        if input_reserve <= 0 or output_reserve <= 0:
            return 0.0
            
        effective_input = sol_amount_decimal * 0.99
        numerator = effective_input * output_reserve
        denominator = input_reserve + effective_input
        
        return numerator / denominator

    @staticmethod
    def calculate_constant_product_sell(keys: LaunchpadPoolKeys, token_amount_decimal: float) -> float:
        """
        Calculate SOL output for selling tokens (constant product curve)
        Uses: (virtualA - realA) and (virtualB + realB) as reserves
        """
        virtual_a_decimal = keys.virtual_a / (10 ** keys.decimals_a)
        virtual_b_decimal = keys.virtual_b / (10 ** keys.decimals_b)
        real_a_decimal = keys.real_a / (10 ** keys.decimals_a)
        real_b_decimal = keys.real_b / (10 ** keys.decimals_b)
        
        input_reserve = virtual_a_decimal - real_a_decimal
        output_reserve = virtual_b_decimal + real_b_decimal
        
        if input_reserve <= 0 or output_reserve <= 0:
            return 0.0
            
        effective_input = token_amount_decimal * 0.99
        numerator = effective_input * output_reserve
        denominator = input_reserve + effective_input
        
        return numerator / denominator
