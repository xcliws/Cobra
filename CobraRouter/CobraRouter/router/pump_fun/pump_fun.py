from decimal import Decimal
from solders.transaction import VersionedTransaction # type: ignore
from solders.keypair import Keypair # type: ignore
from solders.pubkey import Pubkey as Pubkey # type: ignore
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solders.instruction import AccountMeta, Instruction # type: ignore
from spl.token.constants import TOKEN_PROGRAM_ID 
from spl.token.instructions import get_associated_token_address, create_associated_token_account
import base58
from borsh_construct import CStruct, U64
import logging
import asyncio, json
from solders.compute_budget import set_compute_unit_price # type: ignore
from aiohttp import ClientSession
import time, requests
import traceback

try: from .pump_bond import check_has_migrated, get_associated_bonding_curve_address
except: from .pump_bond import check_has_migrated, get_associated_bonding_curve_address
from construct import Struct as cStruct, Byte, Int16ul, Int64ul, Bytes

from solana.rpc.commitment import Processed, Confirmed # type: ignore
from solders.message    import MessageV0 # type: ignore
from spl.token.instructions import close_account, CloseAccountParams, get_associated_token_address

PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GLOBAL_VOLUME_ACCUMULATOR = "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y"
FEE_CONFIG = "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"

BUY_INSTRUCTION_SCHEMA = CStruct(
    "amount" / U64,
    "max_sol_cost" / U64
)

SELL_INSTRUCTION_SCHEMA = CStruct(
    "amount" / U64,
    "min_sol_output" / U64
)

BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])

suppress_logs = [
    "socks",
    "requests",
    "httpx",
    "trio.async_generator_errors",
    "trio",
    "trio.abc.Instrument",
    "trio.abc",
    "trio.serve_listeners",
    "httpcore.http11",
    "httpcore",
    "httpcore.connection",
    "httpcore.proxy",
]

# Set all of them to CRITICAL (no logs)
for log_name in suppress_logs:
    logging.getLogger(log_name).setLevel(logging.CRITICAL)
    logging.getLogger(log_name).handlers.clear()
    logging.getLogger(log_name).propagate = False

def get_solana_price_usd():
    try:
        response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd')
        data = response.json()
        price = data['solana']['usd']
        logging.info(f"Solana price: {price}")
        return str(price)
    except Exception:
        logging.info(f"Failed to get Solana price from Coingecko")
        time.sleep(5)
        return get_solana_price_usd()

NEW_POOL_TYPE = "NEW"
OLD_POOL_TYPE = "OLD"

PumpFunNewPoolState = cStruct(
    "virtual_token_reserves" / Int64ul,
    "virtual_sol_reserves" / Int64ul,
    "real_token_reserves" / Int64ul,
    "real_sol_reserves" / Int64ul,
    "token_total_supply" / Int64ul,
    "complete" / Byte, # boolean
    "creator" / Bytes(32),
)
PumpFunOldPoolState = cStruct(
    "virtual_token_reserves" / Int64ul,
    "virtual_sol_reserves" / Int64ul,
    "real_token_reserves" / Int64ul,
    "real_sol_reserves" / Int64ul,
    "token_total_supply" / Int64ul,
    "complete" / Byte, # boolean
)

def convert_pool_keys(container, pool_type):
    return {
        "virtual_token_reserves": container.virtual_token_reserves,
        "virtual_sol_reserves": container.virtual_sol_reserves,
        "real_token_reserves": container.real_token_reserves,
        "real_sol_reserves": container.real_sol_reserves,
        "token_total_supply": container.token_total_supply,
        "complete": container.complete,
        "creator": container.creator,
    } if pool_type == NEW_POOL_TYPE else {
        "virtual_token_reserves": container.virtual_token_reserves,
        "virtual_sol_reserves": container.virtual_sol_reserves,
        "real_token_reserves": container.real_token_reserves,
        "real_sol_reserves": container.real_sol_reserves,
        "token_total_supply": container.token_total_supply,
        "complete": container.complete,
    }

class PumpFun:
    def __init__(self, session: ClientSession, async_client: AsyncClient):
        self.session = session
        self.async_client = async_client

    def _derive_uva_pda(self, payer: Pubkey):
        user_acc, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(payer)], Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
        )
        return user_acc

    async def _mint_owner(self, mint: Pubkey) -> Pubkey:
        try:
            info = await self.async_client.get_account_info(mint, commitment=Confirmed)
            if info.value is None:
                raise RuntimeError("mint account missing")
            return info.value.owner
        except Exception as e:
            traceback.print_exc()
            logging.info(f"Failed to get token program id: {e}")
            return TOKEN_PROGRAM_ID

    async def fetch_pool_state(self, pool: str):
        """
            Returns:
                dict: Pool data:
                    pool_bump: int
                    index: int
                    creator: str
                    base_mint: str
                    quote_mint: str
                    lp_mint: str
                    pool_base_token_account: str
                    pool_quote_token_account: str
                    lp_supply: int
        """
        pool = Pubkey.from_string(pool)

        resp = await self.async_client.get_account_info_json_parsed(pool, commitment=Processed)
        if not resp or not resp.value or not resp.value.data:
            return "NotOnPumpFun", None

        pool_type = NEW_POOL_TYPE
        raw_data = resp.value.data
        try:
            parsed = PumpFunNewPoolState.parse(raw_data[8:])
        except Exception as e:
            try:
                parsed = PumpFunOldPoolState.parse(raw_data[8:])
                pool_type = OLD_POOL_TYPE
            except Exception as e:
                logging.error(f"Error parsing pool data: {e}")
                return (None, None)
            
        parsed = convert_pool_keys(parsed, pool_type=pool_type)

        return (parsed, pool_type)

    async def lamports_to_tokens(self, lamports: int, price: Decimal) -> Decimal:
        """
        Convert lamports to tokens based on the current price.

        Args:
            lamports (int): The amount in lamports to convert.
            price (Decimal): The price of 1 token in SOL lamports.

        Returns:
            Decimal: The equivalent amount in tokens.
        """
        lams_to_human = Decimal(lamports) / Decimal(1e9)
        tokens = lams_to_human / Decimal(price)
        token_amount = tokens * Decimal(1e6)
        return int(token_amount)

    async def get_price(self, mint: str | Pubkey):
        mint = mint if isinstance(mint, Pubkey) else Pubkey.from_string(mint)
        try:
            bc = get_associated_bonding_curve_address(mint)[0]
            state, _ = await self.fetch_pool_state(str(bc))
            if state == "NotOnPumpFun":
                return "NotOnPumpFun"
            if not state:
                return None
            vtr = state["virtual_token_reserves"] / 1e6
            vsr = state["virtual_sol_reserves"] / 1e9
            if vsr == 0 or vtr == 0:
                return "migrated"
            price = vsr / vtr
            
            return price
        except Exception as e:
            logging.error(f"Error fetching price for mint {mint}: {e}")
            traceback.print_exc()
            return None

    async def build_buy_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        fee_recipient: Pubkey,
        token_amount: int,      # how many tokens to buy
        lamports_budget: int,    # how many lamports to spend
        vault: Pubkey,
        keypair: Keypair,
        token_program_id: Pubkey
    ) -> Instruction:
        instruction_data = BUY_DISCRIMINATOR + BUY_INSTRUCTION_SCHEMA.build({
            "amount": token_amount,
            "max_sol_cost": lamports_budget
        })

        buyer = keypair.pubkey()

        accounts = [
            AccountMeta(pubkey=Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"), is_signer=False, is_writable=False), # global
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True),  # feeRecipient
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),         # mint
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True), # bondingCurve
            AccountMeta(
                pubkey=get_associated_token_address(bonding_curve, mint, token_program_id),
                is_signer=False,
                is_writable=True
            ),                                                                    # associatedBondingCurve
            AccountMeta(
                pubkey=get_associated_token_address(buyer, mint, token_program_id),
                is_signer=False,
                is_writable=True
            ),                                                                    # associatedUser
            AccountMeta(pubkey=buyer, is_signer=True, is_writable=True),         # user
            AccountMeta(pubkey=Pubkey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False), # systemProgram
            AccountMeta(pubkey=Pubkey.from_string(str(token_program_id)), is_signer=False, is_writable=False), # tokenProgram
            AccountMeta(pubkey=Pubkey.from_string(str(vault)), is_signer=False, is_writable=True), # vault
            AccountMeta(pubkey=Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"), is_signer=False, is_writable=False), # eventAuthority
            AccountMeta(pubkey=Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"), is_signer=False, is_writable=False),   # program
            AccountMeta(pubkey=Pubkey.from_string(GLOBAL_VOLUME_ACCUMULATOR), is_signer=False, is_writable=True), # globalVolumeAccumulator
            AccountMeta(pubkey=self._derive_uva_pda(buyer), is_signer=False, is_writable=True), # userVolumeAccumulator
            AccountMeta(pubkey=Pubkey.from_string(FEE_CONFIG), is_signer=False, is_writable=False), # feeConfig
            AccountMeta(pubkey=Pubkey.from_string(FEE_PROGRAM), is_signer=False, is_writable=False), # feeProgram
        ]

        return Instruction(
            program_id=Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"),
            accounts=accounts,
            data=instruction_data
        )

    async def build_sell_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        fee_recipient: Pubkey,
        token_amount: int,       # how many tokens to sell
        lamports_min_output: int, # minimum lamports you want to receive
        vault: Pubkey,
        keypair: Keypair,
        token_program_id: Pubkey
    ) -> Instruction:
        instruction_data = SELL_DISCRIMINATOR + SELL_INSTRUCTION_SCHEMA.build({
            "amount": token_amount,
            "min_sol_output": lamports_min_output
        })

        user = keypair.pubkey()

        # The IDL's account list for sell:
        accounts = [
            AccountMeta(pubkey=Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"), is_signer=False, is_writable=False),  # global
            AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True),  # feeRecipient
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),          # mint
            AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),  # bondingCurve
            AccountMeta(
                pubkey=get_associated_token_address(bonding_curve, mint, token_program_id),
                is_signer=False,
                is_writable=True
            ),                                                                     # associatedBondingCurve
            AccountMeta(
                pubkey=get_associated_token_address(user, mint, token_program_id),
                is_signer=False,
                is_writable=True
            ),                                                                     # associatedUser
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),           # user
            AccountMeta(pubkey=Pubkey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False), # systemProgram
            AccountMeta(pubkey=Pubkey.from_string(str(vault)), is_signer=False, is_writable=True),  # vault
            AccountMeta(pubkey=Pubkey.from_string(str(token_program_id)), is_signer=False, is_writable=False), # tokenProgram
            AccountMeta(pubkey=Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"), is_signer=False, is_writable=False),  # eventAuthority
            AccountMeta(pubkey=Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"), is_signer=False, is_writable=False),    # program
            AccountMeta(pubkey=Pubkey.from_string(FEE_CONFIG), is_signer=False, is_writable=False), # feeConfig
            AccountMeta(pubkey=Pubkey.from_string(FEE_PROGRAM), is_signer=False, is_writable=False), # feeProgram
        ]

        return Instruction(
            program_id=Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"),
            accounts=accounts,
            data=instruction_data
        )

    async def check_ata_exists(self, owner: Pubkey, mint: Pubkey, token_program_id: Pubkey | None = None) -> bool:
        """
        Check if the associated token account (ATA) exists on-chain.
        """
        try:
            if token_program_id is None:
                token_program_id = await self._mint_owner(mint)
            ata_address = get_associated_token_address(owner, mint, token_program_id)

            response = await self.async_client.get_account_info(ata_address)
            if response.value:
                return True
            else:
                return False
        except Exception as e:
            logging.error(f"Error checking ATA existence: {e}")
            return False

    async def make_check_ata(self, keypair: Keypair, instructions: list, mint_address: Pubkey, token_program_id: Pubkey | None = None):
        """
        Check if the Associated Token Account (ATA) exists.
        If it doesn't, add an instruction to create it.
        """
        owner = keypair.pubkey()

        if token_program_id is None:
            token_program_id = await self._mint_owner(mint_address)

        # Check if the ATA exists for the correct token program
        ata_exists = await self.check_ata_exists(owner, mint_address, token_program_id)
        
        if not ata_exists:
            instructions.append(
                create_associated_token_account(
                    payer=owner,
                    owner=owner,
                    mint=mint_address,
                    token_program_id=token_program_id
                )
            )

        return instructions

    def get_creator_vault(self, creator):
        creator_vault_pda, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(Pubkey.from_string(creator))],
            Pubkey.from_string(PUMP_FUN)
        )
        return creator_vault_pda

    async def pump_buy(
            self,
            mint_address: str,
            bonding_curve_pda: str,
            sol_amount: int,
            creator: str,
            keypair: Keypair = None,
            token_amount: int = 0,
            sim: bool = False,
            priority_micro_lamports: int = 0,
            slippage: float = 1.3, # MAX: 1.99
            skip_ata_check: bool = False,
            return_instructions: bool = False,
        ):

        instructions = []

        mint_address = mint_address if isinstance(mint_address, Pubkey) else Pubkey.from_string(mint_address)
        bonding_curve_pda = bonding_curve_pda if isinstance(bonding_curve_pda, Pubkey) else Pubkey.from_string(bonding_curve_pda)

        token_program_id = await self._mint_owner(mint_address)

        has_migrated = await check_has_migrated(
            self.async_client,
            bonding_curve_pda
        )
        if has_migrated:
            return "migrated"

        if priority_micro_lamports > 0:
            instructions.append(
                set_compute_unit_price(
                    priority_micro_lamports
                )
            )

        if not skip_ata_check:
            instructions = await self.make_check_ata(keypair, instructions, mint_address, token_program_id)

        fee_recipient = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
        vault = self.get_creator_vault(creator)
        buy_ix = await self.build_buy_instruction(
            mint_address,
            bonding_curve_pda,
            fee_recipient,
            token_amount,
            # slippage, 1.99x
            int(sol_amount * slippage),
            vault,
            keypair,
            token_program_id
        )
        instructions.append(buy_ix)

        if return_instructions:
            return instructions

        try:
            latest_blockhash = (await self.async_client.get_latest_blockhash(commitment=Processed)).value.blockhash
            msg = MessageV0.try_compile(
                payer = keypair.pubkey(),
                instructions = instructions,
                address_lookup_table_accounts = [],
                recent_blockhash = latest_blockhash
            )
            tx = VersionedTransaction(msg, [keypair])
        except Exception as e:
            logging.error(f"Failed to fetch latest blockhash: {e}")
            raise

        try:
            if sim:
                simulate_resp = await self.async_client.simulate_transaction(tx)
                logging.info(f"Simulation result: {simulate_resp}")
            
            opts = TxOpts(skip_preflight=True, max_retries=0, skip_confirmation=True)
            result = await self.async_client.send_transaction(tx, opts=opts)
            result_json = result.to_json()
            transaction_id = json.loads(result_json).get('result')
            return transaction_id
        except Exception as e:
            logging.error(f"Transaction failed: {e}")
            raise

    async def pump_sell(
            self,
            mint_address: str,
            bonding_curve_pda: str,
            token_amount: int,
            lamports_min_output: int,
            creator: str,
            keypair: Keypair,
            sim: bool = False,
            priority_micro_lamports: int = 0,
            return_instructions: bool = False,
        ):

        instructions = []

        mint_address = mint_address if isinstance(mint_address, Pubkey) else Pubkey.from_string(mint_address)
        bonding_curve_pda = bonding_curve_pda if isinstance(bonding_curve_pda, Pubkey) else Pubkey.from_string(bonding_curve_pda)
        
        has_migrated = await check_has_migrated(
            self.async_client,
            bonding_curve_pda
        )
        if has_migrated:
            return "migrated"

        if priority_micro_lamports > 0:
            instructions.append(
                set_compute_unit_price(
                    priority_micro_lamports
                )
            )

        token_program_id = await self._mint_owner(mint_address)

        fee_recipient = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
        sell_ix = await self.build_sell_instruction(
            mint=mint_address,
            bonding_curve=bonding_curve_pda,
            fee_recipient=fee_recipient,
            token_amount=token_amount,
            lamports_min_output=lamports_min_output,
            vault=self.get_creator_vault(creator),
            keypair=keypair,
            token_program_id=token_program_id
        )
        instructions.append(sell_ix)

        instructions.append(
            close_account(
                CloseAccountParams(
                    program_id=token_program_id,
                    account=get_associated_token_address(keypair.pubkey(), mint_address, token_program_id),
                    dest=keypair.pubkey(),
                    owner=keypair.pubkey(),
                    signers=[],
                )
            )
        )

        if return_instructions:
            return instructions

        try:
            latest_blockhash = (await self.async_client.get_latest_blockhash(commitment=Processed)).value.blockhash
            msg = MessageV0.try_compile(
                payer = keypair.pubkey(),
                instructions = instructions,
                address_lookup_table_accounts = [],
                recent_blockhash = latest_blockhash
            )
            tx = VersionedTransaction(msg, [keypair])
        except Exception as e:
            logging.error(f"Failed to fetch latest blockhash: {e}")
            raise

        try:
            if sim:
                simulate_resp = await self.async_client.simulate_transaction(tx)
                logging.info(f"Simulation result: {simulate_resp}")

            opts = TxOpts(skip_preflight=True, max_retries=0, skip_confirmation=True)
            result = await self.async_client.send_transaction(tx, opts=opts)
            result_json = result.to_json()
            transaction_id = json.loads(result_json).get('result')
            return transaction_id
        except Exception as e:
            logging.error(f"Transaction failed: {e}")
            raise

    async def getTransaction(self, tx_id: str, session: ClientSession):
        start_time = time.time()
        attempt = 1
        try:
            while attempt < 25:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        tx_id,
                        {
                            "commitment": "confirmed",
                            "encoding": "json",
                            "maxSupportedTransactionVersion": 0
                        }
                    ]
                }
                headers = {
                    "Content-Type": "application/json"
                }

                async with session.post(self.rpc_endpoint, json=payload, headers=headers, timeout=10) as response:
                    if response.status != 200:
                        logging.error(f"HTTP Error {response.status}: {await response.text()}")
                        raise Exception(f"HTTP Error {response.status}")

                    data = await response.json()
                    logging.info(f"Attempt {attempt}")

                    if data and data.get('result') is not None:
                        logging.info(f"Elapsed: {time.time() - start_time:.2f}s")
                        result = data['result']
                        return result

                await asyncio.sleep(0.5)
                attempt += 1
        except Exception as e:
            logging.error(f"Error: {e}")
            return None

    async def close(self):
        await self.async_client.close()
        await self.session.close()