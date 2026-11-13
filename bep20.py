"""Read-only BSC (BEP20) deposit verification. Never signs or broadcasts transactions.

Also hosts the shared verifier plumbing used by crypto_networks.py:

* ``PendingPayment``  - temporary state (not found yet, unconfirmed, node/API trouble,
  malformed or unexpected responses). Callers must NOT use up a payer's attempt.
* ``PaymentMismatch`` - definite answer that this TXID does not pay the invoice
  (wrong recipient/token/amount, failed tx, wrong chain, not a transfer, bad TXID).
* ``fetch_json``      - one HTTP request with a timeout and a response-size cap that
  converts every transport/HTTP/JSON problem into ``PendingPayment``.
* ``throttle``        - module-level per-network request pacing shared by all verifiers.

Both exception classes subclass ``ValueError`` so older callers keep working.
"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
import json
import os
import re
import time
import weakref

import aiohttp

WALLET = '0x4AB3C61e666BA119deA95e0AD78819B3681DF53b'
TOKEN = '0x55d398326f99059fF775485246999027B3197955'  # Binance-Peg BSC-USD (USDT), 18 decimals
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'  # Transfer(address,address,uint256)
BSC_CHAIN_ID = 56
MIN_CONFIRMATIONS = 20

MAX_BODY = 2 * 1024 * 1024
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, sock_connect=5)

# Customer-facing messages (Russian source; English via translations_en.json).
MSG_NODE_UNAVAILABLE = 'Узел сети временно недоступен. Проверка повторится автоматически.'
MSG_NOT_FOUND = 'Транзакция пока не найдена. Проверка повторится автоматически.'
MSG_WAIT_CONFIRMATIONS = 'Ожидаем подтверждений сети. Проверка повторится автоматически.'
MSG_BLOCK_CHANGED = 'Блок изменился. Повтори проверку после подтверждений.'
MSG_WRONG_CHAIN = 'RPC подключён к другой сети. Нужна BNB Smart Chain mainnet.'
MSG_TX_FAILED = 'Транзакция неуспешна или не соответствует TXID'
MSG_NO_TRANSFER = 'Перевод USDT не совпадает с адресом и точной суммой счёта.'
MSG_BAD_TXID_BSC = 'TXID должен содержать 0x и 64 шестнадцатеричных символа'


class PendingPayment(ValueError):
    """Temporary state: retry later, do not count a failed attempt."""


class PaymentMismatch(ValueError):
    """Definite mismatch: this TXID does not pay this invoice."""


def address(value):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', value):
        raise ValueError('Неверный адрес BSC')
    return value.lower()


def tx_hash(value):
    value = value.strip()
    if not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise PaymentMismatch(MSG_BAD_TXID_BSC)
    return value.lower()


def token_amount(raw):
    value = Decimal(raw) / Decimal(10**18)
    # Previously issued six-decimal invoices must never be silently rounded.
    places = '.4f' if int(raw) % 10**14 == 0 else '.6f'
    return format(value, places)


def address_qr(wallet):
    import io
    import qrcode
    buffer = io.BytesIO()
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(address(wallet))
    qr.make(fit=True)
    qr.make_image(fill_color='black', back_color='white').save(buffer, format='PNG')
    return buffer.getvalue()


@dataclass(frozen=True)
class VerifiedTransfer:
    txid: str
    wallet: str
    token: str
    amount_raw: str
    block_time: int
    block_number: int
    block_hash: str
    network: str = 'bsc'
    transfer_id: str = ''


# ---------------------------------------------------------------- shared HTTP plumbing

_last_request = {}                       # network -> time.monotonic() of last request
_locks = weakref.WeakKeyDictionary()     # event loop -> {network: asyncio.Lock}


async def throttle(network, interval):
    """Space requests to one network at least `interval` seconds apart, process-wide."""
    if interval <= 0:
        return
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, {}).setdefault(network, asyncio.Lock())
    async with lock:
        wait = _last_request.get(network, -1e9) + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request[network] = time.monotonic()


async def fetch_json(session, url, payload=None, params=None, headers=None,
                     not_found=MSG_NOT_FOUND):
    """GET (or POST when `payload` is given) and decode JSON.

    Any failure - connection, timeout, HTTP status, rate limit, oversized or non-JSON
    body - raises PendingPayment. HTTP 404 raises PendingPayment(`not_found`).
    """
    method = 'POST' if payload is not None else 'GET'
    try:
        async with session.request(method, url, json=payload, params=params, headers=headers,
                                   timeout=REQUEST_TIMEOUT) as response:
            if response.status == 404:
                raise PendingPayment(not_found)
            if response.status >= 400:
                raise PendingPayment(MSG_NODE_UNAVAILABLE)
            if int(response.headers.get('Content-Length') or 0) > MAX_BODY:
                raise PendingPayment(MSG_NODE_UNAVAILABLE)
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body += chunk
                if len(body) > MAX_BODY:
                    raise PendingPayment(MSG_NODE_UNAVAILABLE)
            return json.loads(bytes(body))
    except PendingPayment:
        raise
    except Exception:
        raise PendingPayment(MSG_NODE_UNAVAILABLE) from None


async def guarded(coro, timeout):
    """Run a verification coroutine; anything that is not a definite answer is pending."""
    try:
        async with asyncio.timeout(timeout):
            return await coro
    except (PendingPayment, PaymentMismatch):
        raise
    except Exception:
        raise PendingPayment(MSG_NODE_UNAVAILABLE) from None


def hex_int(value):
    """Strict 0x-prefixed hex quantity -> int (ValueError/TypeError on bad input)."""
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value):
        raise ValueError('bad hex quantity')
    return int(value, 16)


# ---------------------------------------------------------------- BSC

class BscVerifier:
    """Verify a USDT BEP20 payment via a JSON-RPC node.

    1. eth_chainId must be 56 (BNB Smart Chain mainnet).
    2. eth_getTransactionReceipt: must exist, match the TXID and have status 1.
    3. At least `confirmations` (>= 20) blocks on top; the block hash must still match.
    4. Pick a non-removed USDT Transfer log to the shop wallet with exactly amount_raw.
       transfer_id = "<txid>:<logIndex>" so one batched tx can pay several invoices.
    """

    def __init__(self, rpc=None, confirmations=None):
        self.rpc = rpc or os.getenv('BSC_RPC_URL', 'https://bsc-dataseed.bnbchain.org')
        if not self.rpc.startswith('https://'):
            raise ValueError('BSC_RPC_URL должен использовать HTTPS')
        configured = int(confirmations or os.getenv('BSC_CONFIRMATIONS', str(MIN_CONFIRMATIONS)))
        self.confirmations = max(MIN_CONFIRMATIONS, configured)

    async def verify(self, txid, wallet, token, amount_raw):
        txid = tx_hash(txid)

        async def run():
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                return await self._verify(session, txid, address(wallet), address(token), int(amount_raw))
        return await guarded(run(), 30)

    async def _rpc(self, session, method, params):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
        result = await fetch_json(session, self.rpc, payload=payload)
        if not isinstance(result, dict) or result.get('error') or 'result' not in result:
            raise PendingPayment(MSG_NODE_UNAVAILABLE)
        return result['result']

    async def _verify(self, session, txid, wallet, token, expected):
        if hex_int(await self._rpc(session, 'eth_chainId', [])) != BSC_CHAIN_ID:
            raise PaymentMismatch(MSG_WRONG_CHAIN)

        receipt = await self._rpc(session, 'eth_getTransactionReceipt', [txid])
        if not receipt:
            raise PendingPayment(MSG_NOT_FOUND)
        if str(receipt['transactionHash']).lower() != txid:
            raise PendingPayment(MSG_NODE_UNAVAILABLE)   # node answered about another tx
        if hex_int(receipt['status']) != 1:
            raise PaymentMismatch(MSG_TX_FAILED)

        block_number = hex_int(receipt['blockNumber'])
        head = hex_int(await self._rpc(session, 'eth_blockNumber', []))
        if head - block_number + 1 < self.confirmations:
            raise PendingPayment(MSG_WAIT_CONFIRMATIONS)

        block = await self._rpc(session, 'eth_getBlockByNumber', [receipt['blockNumber'], False])
        if not block or str(block['hash']).lower() != str(receipt['blockHash']).lower():
            raise PendingPayment(MSG_BLOCK_CHANGED)

        recipient_topic = '0x' + '0' * 24 + wallet[2:]
        matches = []
        for log in receipt['logs']:
            topics = [str(topic).lower() for topic in log.get('topics') or []]
            if (log.get('removed') or str(log.get('address', '')).lower() != token
                    or len(topics) != 3 or topics[0] != TRANSFER or topics[2] != recipient_topic):
                continue
            if hex_int(log['data']) == expected:
                matches.append(hex_int(log['logIndex']))
        if not matches:
            raise PaymentMismatch(MSG_NO_TRANSFER)

        return VerifiedTransfer(
            txid, wallet, token, str(expected), hex_int(block['timestamp']), block_number,
            str(block['hash']).lower(), 'bsc', f'{txid}:{min(matches)}')
