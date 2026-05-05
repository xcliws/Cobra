> [!NOTE]
> This project is outdated, and will soon become archived. </br>
> Use [Mamba](https://github.com/FLOCK4H/Mamba) instead. </br>
> Some markets will still work until their on-chain program changes.

</br>

<img src="https://github.com/FLOCK4H/Cobra/blob/main/docs/imgs/cobra_banner.png" />

> [!IMPORTANT]
> This software is open-source & free of any charge, there are no fees, nor ads included in the repo.
> </br>
> Consider feeding the Cobra if the project comes useful.
> </br>
> Wallet: `FL4CKfetEWBMXFs15ZAz4peyGbCuzVaoszRKcoVt3WfC`

# Cobra

### Read the **[Cobra Documentation](https://flock4h.github.io/Cobra)**

**Supported Solana Markets:**

1. [Raydium CLMM](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/raydiumswap/clmm)
2. [Meteora DLMM](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/meteora_dlmm)
3. [PumpFun](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/pump_fun)
4. [PumpSwapAMM](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/PumpSwapAMM)
5. [MeteoraDAMM v1](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/meteora_damm_v1)
6. [MeteoraDAMM v2](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/meteora_damm_v2)
7. [Meteora Dynamic Bonding Curve](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/meteoraDBC)
8. [Raydium CPMM](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/raydiumswap/cpmm)
9. [Raydium AMM V4](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/raydiumswap/amm_v4)
10. [Raydium Launchlab](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter/router/raydiumswap/launchlab)

Cobra composes of 3 parts:
- CobraRouter
- CobraNET (Optional)
- CobraWallets (Optional)

**and a CLI, a Command Line Interface wrapper around [CobraRouter](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter) and [CobraWallets](https://github.com/FLOCK4H/Cobra/tree/main/CobraWallets/):**

<img width="1101" height="526" alt="image" src="https://github.com/user-attachments/assets/5eef96af-6640-47a0-8541-9ba0066a093f" />

Check out **[How to run CLI](https://flock4h.github.io/Cobra/first/installation/#4-optional-cli-configuration)**

After setting up Cobra properly, the features are:
- Generating Wallets, Grinding Custom Wallets
- Buying, selling on supported markets (& in Pump.fun AMM - creating new pools)
- Detecting mint's market and pool address
- Fetching pool's states/ keys (for developers)
- Interfacing with supported markets using available methods (for developers)

# Setup

## 1. Download the repository and install the required modules

```
$ git clone https://github.com/FLOCK4H/Cobra
$ pip install -r req.txt
```

<details>
    <summary><b>🖱️ I am a developer</b></summary>

## 2. Install [CobraRouter](https://github.com/FLOCK4H/Cobra/tree/main/CobraRouter/CobraRouter) module:
    
```bash
$ cd CobraRouter
$ pip install .
```

## 3. Usage:

```python
from CobraRouter.detect import CobraDetector
from CobraRouter.router import Router
import asyncio
from solana.rpc.async_api import AsyncClient
import aiohttp

async def main():
    client = AsyncClient("https://api.apewise.org/rpc?api-key=")
    session = aiohttp.ClientSession()
    router = Router(client, session)
    detector = CobraDetector(router, "https://api.apewise.org/rpc?api-key=")
    detect = await detector._detect("9R1pCPM7GRr9F4gk978LqBQiPKfYStbZKc5iKV4imoon")
    print(detect)
    await client.close()
    await session.close()
    await router.close()

if __name__ == "__main__":
    asyncio.run(main())
```

**Example output:**

```bash
PS C:\Users\swear\Desktop> python .\test.py
BMBcZ9GWMCi9HaCE7BagrLxakzffy6fAGdEpihLRfVPw
[CobraRouter] Route winner (?): 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 -> BMBcZ9GWMCi9HaCE7BagrLxakzffy6fAGdEpihLRfVPw
('675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8', 'BMBcZ9GWMCi9HaCE7BagrLxakzffy6fAGdEpihLRfVPw')
```

Learn how to interact with the library: **[Cobra Documentation](https://flock4h.github.io/Cobra)**

</details>

<details>

<summary><b>🖱️ I am a trader</b></summary>

## 2. Configure the `secrets.env` file:

> Required variables are: `RUN_AS_CLI`, `HTTP_RPC`, and `PRIVATE_KEY`.</br> 
> Helius can be free tier.</br>
> **Current fastest HTTP RPC Provider:** [Apewise](https://apewise.org)

**Create or edit `secrets.env` file; Here you can control `SLIPPAGE`, which is 1..100 range, and `PRIORITY_FEE_LEVEL` which is a String and options are: `low`, `medium`, `high`, `turbo`.**

```
RUN_AS_CLI=True
HTTP_RPC="https://api.apewise.org/rpc?api-key=" # apewise.org -> fastest right now

# CLI CONFIG SECTION
PRIVATE_KEY=2wY3abcde5Pj4xxxxxxxxxxxxxxxxxxxxxxxxxxxx
SLIPPAGE=30
PRIORITY_FEE_LEVEL="high" # "low", "medium", "high", "turbo"
```

## 3. Run the CLI

`$ python main.py`


<img width="913" height="181" alt="image" src="https://github.com/user-attachments/assets/ef98c6ab-86a5-4548-bba2-8d3ad9bdc89a" />


</details>

# CobraNET

CobraNET is an optional Telegram wrapper around the CobraRouter and CobraWallets, and allows you to host your own Dex Router via Telegram Bot.

## Setup

Create `secrets.env` where you run the application, make sure to set:

`secrets.env`
```
RUN_AS_CLI=False
BOT_TOKEN=TELEGRAM_BOT_TOKEN_FROM_BOTFATHER
HTTP_RPC="https://api.apewise.org/rpc?api-key=" # apewise.org -> fastest right now
HELIUS_API_KEY="your-helius-free-or-not-api-key" 
```

For database operations you will need PostgreSQL installed: [PostgreSQL](https://www.postgresql.org/download)

Initialize the database:

```
$ cd Cobra
$ python database.py
```

Run the Cobra with Telegram support:

`$ python main.py`

Test your bot by entering `/start` inside the conversation.

<img width="528" height="757" alt="image" src="https://github.com/user-attachments/assets/8af7dd6b-cc97-4ee1-9ed3-6abd5a13c163" />

Learn more about CobraNET here: [Cobra Documentation](https://flock4h.github.io/Cobra/markets/cobranet)

## Contact & Support

**Discord: [FLOCK4H.CAVE](https://discord.gg/thREUECv2a)**, **Telegram: [FLOCK4H.CAVE](https://t.me/flock4hcave)**

**Telegram private handle: @dubskii420**

<img src="https://github.com/user-attachments/assets/d655c153-0056-47fc-8314-6f919f18ed6d" width="256" />

# LICENSE

Copyright 2025 FLOCK4H

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
